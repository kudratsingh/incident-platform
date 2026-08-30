"""SSE stream auth: the stream-token mint endpoint and the stream route.

The browser cannot send an Authorization header from a native EventSource, so
the stream authenticates with a short-lived, single-purpose stream token:

  POST /jobs/{id}/stream-token   — normal header auth; authorizes the job
                                   (tenant scope + ownership) and mints the token
  GET  /jobs/{id}/stream?token=… — validates the stream token from the query
                                   string; identity comes only from the token

Covered here:
  - minting requires authentication (401 with no header)
  - minting enforces tenant scope (404 cross-tenant — cannot confirm existence)
  - minting enforces ownership (403 for a non-owner plain user; admins allowed)
  - the minted token is bound to the job (sub claim == job id)
  - the stream route rejects: missing token, the primary access JWT, an
    expired stream token, and a stream token minted for a different job
  - the stream route accepts a freshly minted stream token (no more 401 loop)
  - a job that already finished short-circuits off the jobs row into one
    synthetic terminal event instead of hanging on a silent channel (E1-10)

The SSE generator itself is not drained — conftest's Redis is an AsyncMock, so
the accept test patches app.api.streaming.subscribe with an empty async
generator and asserts on the auth decision only.  The short-circuit tests are
the exception: they never reach subscribe(), so they can read the real body.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest_asyncio
from app.config import get_settings
from app.core.security import create_access_token, create_stream_token, decode_token, hash_password
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.models.tenant import Tenant
from app.models.user import User
from app.workers import progress_broker
from app.workers.progress import ProgressEvent
from app.workers.progress_broker import ProgressBroker
from httpx import AsyncClient
from jose import jwt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def job(db_session, test_user: User) -> Job:  # type: ignore[no-untyped-def]
    """A running job owned by the conftest test_user (default tenant)."""
    job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD,
        status=JobStatus.RUNNING,
        retry_count=0,
        max_retries=3,
        priority=0,
    )
    db_session.add(job)
    await db_session.flush()
    return job


@pytest_asyncio.fixture
async def other_tenant_headers(db_session) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Auth headers for a user in a completely separate tenant."""
    tenant = Tenant(
        id=uuid.uuid4(), slug="stream-other-tenant", name="Other Co.", is_active=True
    )
    db_session.add(tenant)
    await db_session.flush()
    user = User(
        tenant_id=tenant.id,
        email="stream-outsider@other.example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(
        {"sub": str(user.id), "tenant_id": str(user.tenant_id), "role": user.role}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def second_user_headers(db_session, default_tenant) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Auth headers for a second plain (non-admin) user in the default tenant."""
    user = User(
        tenant_id=default_tenant.id,
        email="stream-second@example.com",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    token = create_access_token(
        {"sub": str(user.id), "tenant_id": str(user.tenant_id), "role": user.role}
    )
    return {"Authorization": f"Bearer {token}"}


def _expired_stream_token(job_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    """A structurally valid stream token whose 60s lifetime has already passed.

    Built by back-dating the mint-time clock (the sanctioned way to test
    expiry — the real TTL is never extended for test convenience).
    """
    settings = get_settings()
    minted_at = datetime.now(UTC) - timedelta(seconds=120)
    payload = {
        "sub": str(job_id),
        "tenant_id": str(tenant_id),
        "exp": minted_at + timedelta(seconds=60),
        "iat": minted_at,
        "type": "stream",
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


async def _empty_subscribe(
    *args: object, **kwargs: object
) -> AsyncGenerator[ProgressEvent, None]:
    """Stand-in for progress.subscribe — yields nothing and ends the stream."""
    return
    yield  # pragma: no cover — unreachable; makes this an async generator


async def _never_ending_subscribe(
    *args: object, **kwargs: object
) -> AsyncGenerator[ProgressEvent, None]:
    """Stand-in for a live-but-silent channel: yields nothing, never returns.

    This is what the endpoint used to do to a late subscriber. The E1-10 tests
    patch it in so that "the endpoint fell through to subscribe()" shows up as
    a timeout rather than a happy empty body.
    """
    await asyncio.Event().wait()
    yield  # pragma: no cover — unreachable; makes this an async generator


def _sse_events(body: str) -> list[dict[str, object]]:
    """Parse an SSE body into the JSON payload of each `data:` line."""
    return [
        json.loads(line.removeprefix("data:").strip())
        for line in body.splitlines()
        if line.startswith("data:")
    ]


async def _fetch_stream(client: AsyncClient, job_id: uuid.UUID, tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """GET the stream with a valid token, failing fast if it does not close."""
    token = create_stream_token(job_id, tenant_id)
    with mock.patch("app.api.streaming.subscribe", _never_ending_subscribe):
        return await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job_id}/stream", params={"token": token}),
            timeout=5,
        )


# ---------------------------------------------------------------------------
# POST /jobs/{id}/stream-token — authn + authz (the missing F1-03 checks)
# ---------------------------------------------------------------------------


async def test_stream_token_requires_auth(client: AsyncClient, job: Job) -> None:
    """Minting a stream token with no Authorization header → 401."""
    resp = await client.post(f"/api/v1/jobs/{job.id}/stream-token")
    assert resp.status_code == 401


async def test_stream_token_cross_tenant_404(
    client: AsyncClient, job: Job, other_tenant_headers: dict[str, str]
) -> None:
    """Tenant-B user asks for a stream token on a tenant-A job → 404.

    404, not 403 — the caller cannot even confirm the job exists.
    """
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=other_tenant_headers
    )
    assert resp.status_code == 404
    assert resp.json().get("error_code") == "not_found"


async def test_stream_token_non_owner_403(
    client: AsyncClient, job: Job, second_user_headers: dict[str, str]
) -> None:
    """A plain user in the same tenant, not the job owner → 403."""
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=second_user_headers
    )
    assert resp.status_code == 403
    assert resp.json().get("error_code") == "forbidden"


async def test_stream_token_owner_gets_job_bound_token(
    client: AsyncClient, job: Job, auth_headers: dict[str, str]
) -> None:
    """The owner gets a token whose subject is the JOB id (not the user id)."""
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=auth_headers
    )
    assert resp.status_code == 200
    token = resp.json()["token"]
    payload = decode_token(token, expected_type="stream")
    assert payload["sub"] == str(job.id)
    assert payload["tenant_id"] == str(job.tenant_id)


async def test_stream_token_admin_can_mint_for_another_users_job(
    client: AsyncClient, job: Job, admin_headers: dict[str, str]
) -> None:
    """Admins keep the documented privilege of streaming any same-tenant job."""
    resp = await client.post(
        f"/api/v1/jobs/{job.id}/stream-token", headers=admin_headers
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /jobs/{id}/stream — token validation (the F2-01/F2-03 transport)
# ---------------------------------------------------------------------------


async def test_stream_route_rejects_missing_token(
    client: AsyncClient, job: Job
) -> None:
    """No ?token → 401 from OUR token check, not from the header-only scheme.

    The error_code assertion is what proves the route stopped depending on
    the Authorization header: at HEAD the 401 is FastAPI's bare
    {"detail": "Not authenticated"} with no error_code envelope.
    """
    resp = await client.get(f"/api/v1/jobs/{job.id}/stream")
    assert resp.status_code == 401
    assert resp.json().get("error_code") == "authentication_failed"


async def test_stream_route_rejects_primary_access_token(
    client: AsyncClient, job: Job, user_token: str
) -> None:
    """The primary access JWT in the query string must NOT open a stream.

    Single-purpose enforcement (F2-03): only type=stream tokens are accepted,
    so nobody can 'fix' browser auth by pasting the real JWT into the URL.
    """
    resp = await client.get(
        f"/api/v1/jobs/{job.id}/stream", params={"token": user_token}
    )
    assert resp.status_code == 401
    assert resp.json().get("error_code") == "authentication_failed"


async def test_stream_route_rejects_expired_token(
    client: AsyncClient, job: Job
) -> None:
    """A stream token minted 2 minutes ago (60s TTL) → 401."""
    expired = _expired_stream_token(job.id, job.tenant_id)
    resp = await client.get(
        f"/api/v1/jobs/{job.id}/stream", params={"token": expired}
    )
    assert resp.status_code == 401
    assert resp.json().get("error_code") == "authentication_failed"


async def test_stream_route_rejects_token_for_other_job(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    job: Job,
    test_user: User,
    auth_headers: dict[str, str],
) -> None:
    """A token minted for job A replayed against job B → 403.

    Both jobs belong to the same owner, so the ONLY thing failing here is the
    job binding — without it, any token the caller could mint would open any
    job's stream and reopen F1-03 through the back door.
    """
    other_job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.REPORT_GEN,
        status=JobStatus.RUNNING,
        retry_count=0,
        max_retries=3,
        priority=0,
    )
    db_session.add(other_job)
    await db_session.flush()

    mint = await client.post(f"/api/v1/jobs/{job.id}/stream-token", headers=auth_headers)
    assert mint.status_code == 200
    token_for_job_a = mint.json()["token"]

    resp = await client.get(
        f"/api/v1/jobs/{other_job.id}/stream", params={"token": token_for_job_a}
    )
    assert resp.status_code == 403
    assert resp.json().get("error_code") == "forbidden"


async def test_stream_route_accepts_valid_stream_token(
    client: AsyncClient, job: Job, auth_headers: dict[str, str]
) -> None:
    """Mint → stream with ?token= → NOT 401 (the F2-01 browser regression).

    At HEAD the header-only dependency 401'd every EventSource request, which
    is why the browser reconnect-looped forever. subscribe() is patched to an
    empty async generator because conftest's Redis is an AsyncMock — the
    assertion is the auth decision, not event delivery.
    """
    mint = await client.post(f"/api/v1/jobs/{job.id}/stream-token", headers=auth_headers)
    assert mint.status_code == 200
    stream_token = mint.json()["token"]

    with mock.patch("app.api.streaming.subscribe", _empty_subscribe):
        resp = await client.get(
            f"/api/v1/jobs/{job.id}/stream", params={"token": stream_token}
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")


# ---------------------------------------------------------------------------
# GET /jobs/{id}/stream — the late-subscriber short-circuit (E1-10)
# ---------------------------------------------------------------------------


async def _finished_job(  # type: ignore[no-untyped-def]
    db_session, test_user: User, status: JobStatus, **fields: object
) -> Job:
    fields.setdefault("retry_count", 0)
    job = Job(
        tenant_id=test_user.tenant_id,
        user_id=test_user.id,
        type=JobType.CSV_UPLOAD,
        status=status,
        max_retries=3,
        priority=0,
        **fields,
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def test_stream_of_completed_job_closes_immediately(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
) -> None:
    """THE E1-10 assertion at the HTTP layer.

    The job finished before this client connected and Redis retained nothing
    (conftest's redis.get returns None, i.e. the key was evicted or predates
    the snapshot).  Pre-fix the endpoint subscribed to a channel that will
    never speak again and the response never ended — here subscribe() is
    patched to a generator that never returns, so falling through to it shows
    up as the timeout it really is.
    """
    job = await _finished_job(db_session, test_user, JobStatus.COMPLETED)

    resp = await _fetch_stream(client, job.id, job.tenant_id)

    assert resp.status_code == 200
    events = _sse_events(resp.text)
    assert len(events) == 1
    assert events[0]["status"] == "completed"
    assert events[0]["progress"] == 100
    assert events[0]["job_id"] == str(job.id)


async def test_stream_of_cancelled_job_closes_immediately(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
) -> None:
    """Saga-cancelled jobs are terminal too — the case the audit sketch missed."""
    job = await _finished_job(db_session, test_user, JobStatus.CANCELLED)

    resp = await _fetch_stream(client, job.id, job.tenant_id)

    events = _sse_events(resp.text)
    assert [e["status"] for e in events] == ["cancelled"]
    assert events[0]["progress"] == 0


async def test_stream_of_dead_lettered_job_reports_the_error(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
) -> None:
    """The synthetic event carries the row's error message and retry count."""
    job = await _finished_job(
        db_session,
        test_user,
        JobStatus.DEAD_LETTER,
        error_message="upstream 500",
        retry_count=3,
    )

    resp = await _fetch_stream(client, job.id, job.tenant_id)

    events = _sse_events(resp.text)
    assert events[0]["status"] == "dead_letter"
    assert events[0]["message"] == "upstream 500"
    assert events[0]["retry_count"] == 3


async def test_stream_of_running_job_still_subscribes(
    client: AsyncClient, job: Job
) -> None:
    """A live job must NOT be short-circuited — the row says nothing final."""
    token = create_stream_token(job.id, job.tenant_id)

    with mock.patch("app.api.streaming.subscribe", _empty_subscribe):
        resp = await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
            timeout=5,
        )

    assert resp.status_code == 200
    assert _sse_events(resp.text) == []


async def test_retained_snapshot_takes_precedence_over_the_row(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
) -> None:
    """When Redis still holds a snapshot, subscribe() owns the stream.

    The DB short-circuit is the fallback for an evicted/absent key only; it
    must never pre-empt events the pub/sub path is about to deliver.
    """
    job = await _finished_job(db_session, test_user, JobStatus.COMPLETED)
    token = create_stream_token(job.id, job.tenant_id)
    retained = ProgressEvent(
        job_id=str(job.id), status="completed", progress=100, message="from redis"
    )

    async def _snapshot(*args: object) -> ProgressEvent:
        return retained

    with (
        mock.patch("app.api.streaming.read_last_event", _snapshot),
        mock.patch("app.api.streaming.subscribe", _empty_subscribe),
    ):
        resp = await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
            timeout=5,
        )

    # subscribe() (stubbed empty here) was used — no synthetic event was emitted.
    assert _sse_events(resp.text) == []


# ---------------------------------------------------------------------------
# ...and the case where the snapshot and the row DISAGREE (WO-R2-57)
#
# The test above only covers a snapshot that agrees with the row — both
# terminal-completed — so it cannot see the failure the reconciliation exists
# to prevent: a terminal snapshot retained in front of a row that is not
# terminal. A DLQ replay produces exactly that (the job reaches dead_letter,
# the snapshot records it, an operator replays the job, and the replay's
# `running` events are deliberately refused by the snapshot's ordering guard),
# and handing that snapshot to a viewer closes the stream on its first event
# for a job that is running right now.
#
# The tie-break is recency, not "the row always wins": the same disagreement
# is produced by an ordinary race, where a job finishes microseconds after
# this request read its row, and there the snapshot is the truthful half.
# ---------------------------------------------------------------------------


def _subscribe_recorder(calls: list[dict[str, object]]):  # type: ignore[no-untyped-def]
    async def _subscribe(
        job_id: str, **kwargs: object
    ) -> AsyncGenerator[ProgressEvent, None]:
        calls.append(kwargs)
        return
        yield  # pragma: no cover — unreachable; makes this an async generator

    return _subscribe


def _snapshot_returning(event: ProgressEvent):  # type: ignore[no-untyped-def]
    async def _read(*args: object) -> ProgressEvent:
        return event

    return _read


async def test_stale_terminal_snapshot_does_not_close_a_running_job_stream(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
) -> None:
    """Row says running and was written after the snapshot said dead_letter.

    Before WO-R2-57 the snapshot was passed straight through, so the viewer of
    a replayed job got one `dead_letter` event and a closed stream.
    """
    job = await _finished_job(
        db_session,
        test_user,
        JobStatus.RUNNING,
        updated_at=datetime.now(UTC),
    )
    token = create_stream_token(job.id, job.tenant_id)
    stale = ProgressEvent(
        job_id=str(job.id),
        status="dead_letter",
        progress=0,
        message="from a lifecycle this job has left",
        timestamp=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    )
    calls: list[dict[str, object]] = []

    with (
        mock.patch("app.api.streaming.read_last_event", _snapshot_returning(stale)),
        mock.patch("app.api.streaming.subscribe", _subscribe_recorder(calls)),
    ):
        resp = await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
            timeout=5,
        )

    assert resp.status_code == 200
    # The stale snapshot is not delivered: the stream stays open for the live
    # events of the run that is actually happening.
    assert calls == [{"use_snapshot": False}]
    assert _sse_events(resp.text) == []


async def test_snapshot_newer_than_the_row_is_still_believed(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
) -> None:
    """The ordinary race: the job finished just after this request read its row.

    Distrusting every terminal-snapshot-on-a-running-row would turn this into
    a stream that waits out the broker's idle timeout for events that have
    already been published.
    """
    job = await _finished_job(
        db_session,
        test_user,
        JobStatus.RUNNING,
        updated_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    token = create_stream_token(job.id, job.tenant_id)
    fresh = ProgressEvent(
        job_id=str(job.id),
        status="completed",
        progress=100,
        message="finished a moment ago",
        timestamp=datetime.now(UTC).isoformat(),
    )
    calls: list[dict[str, object]] = []

    with (
        mock.patch("app.api.streaming.read_last_event", _snapshot_returning(fresh)),
        mock.patch("app.api.streaming.subscribe", _subscribe_recorder(calls)),
    ):
        resp = await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
            timeout=5,
        )

    assert resp.status_code == 200
    assert calls == [{"use_snapshot": True}]


async def test_non_terminal_snapshot_on_a_running_row_is_untouched(
    client: AsyncClient,
    db_session,  # type: ignore[no-untyped-def]
    test_user: User,
) -> None:
    """Only a *terminal* snapshot can close a stream, so only that case is
    reconciled — a `running` snapshot older than the row is just old news."""
    job = await _finished_job(
        db_session,
        test_user,
        JobStatus.RUNNING,
        updated_at=datetime.now(UTC),
    )
    token = create_stream_token(job.id, job.tenant_id)
    older = ProgressEvent(
        job_id=str(job.id),
        status="running",
        progress=40,
        message="halfway",
        timestamp=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
    )
    calls: list[dict[str, object]] = []

    with (
        mock.patch("app.api.streaming.read_last_event", _snapshot_returning(older)),
        mock.patch("app.api.streaming.subscribe", _subscribe_recorder(calls)),
    ):
        resp = await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
            timeout=5,
        )

    assert resp.status_code == 200
    assert calls == [{"use_snapshot": True}]


# ---------------------------------------------------------------------------
# Pool isolation and the per-process stream cap (WO-R2-11)
#
# Each open stream used to hold one connection out of the single 20-connection
# process-wide pool for its whole life, and `worker_loop` runs in the same
# process on that same pool — so ~20 parked dashboards starved the rate
# limiter, `check_backpressure` and the admin stats loops. Two structural
# answers, asserted here at the HTTP layer:
#
#   * streaming draws from its own bounded pool, so it cannot starve anyone,
#   * a per-process cap refuses the extra viewer with 503 + Retry-After
#     rather than letting it queue against a finite resource.
#
# The fan-out itself (N viewers → one connection) is asserted in
# tests/unit/test_progress_broker.py, where the Pub/Sub calls are visible.
# ---------------------------------------------------------------------------


class _DeadRedis:
    """A Redis that refuses everything — stands in for an outage."""

    def pubsub(self) -> object:
        raise ConnectionError("redis down")

    async def get(self, key: str) -> str | None:
        raise ConnectionError("redis down")


def _test_broker(redis: object, **overrides: object) -> ProgressBroker:
    kwargs: dict[str, object] = {
        "max_streams": 1,
        "idle_timeout_seconds": 1,
        "max_duration_seconds": 5,
        "retry_after_seconds": 5,
    }
    kwargs.update(overrides)
    return ProgressBroker(redis, **kwargs)  # type: ignore[arg-type]


def test_streaming_draws_from_its_own_pool_not_the_shared_one() -> None:
    """The SSE pool is a separate object, sized independently of the default.

    This is the whole point of the finding: whatever streaming does to its
    own pool, the rate limiter, backpressure check and worker loops that hold
    `get_redis_pool()` keep their 20 slots.
    """
    from app.core.redis import get_redis_pool, get_sse_redis_pool

    settings = get_settings()
    assert get_sse_redis_pool() is not get_redis_pool()
    assert get_sse_redis_pool().max_connections == settings.sse_redis_max_connections
    assert get_redis_pool().max_connections == 20


async def test_stream_beyond_the_cap_is_refused_while_post_jobs_still_works(
    client: AsyncClient, job: Job, auth_headers: dict[str, str]
) -> None:
    """Cap+1 concurrent viewers: the extra one is refused, the API stays up.

    Pre-fix there was no cap at all — the (cap+1)th viewer opened happily and
    took another connection out of the shared pool, and it was `POST /jobs`
    (rate limiter, backpressure, quota — all Redis) that paid for it. Now the
    refusal is explicit, addressed to the viewer, and carries Retry-After.
    """
    broker = _test_broker(_DeadRedis(), max_streams=1)
    broker.acquire()  # the one permitted stream is already open
    token = create_stream_token(job.id, job.tenant_id)

    with mock.patch.object(progress_broker, "_broker", broker):
        resp = await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
            timeout=5,
        )
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "5"
        assert resp.json()["error_code"] == "stream_capacity"

        # The process is NOT out of Redis — job submission still works.
        created = await client.post(
            "/api/v1/jobs", json={"type": "csv_upload"}, headers=auth_headers
        )
        assert created.status_code == 201


async def test_a_finished_stream_hands_its_slot_back(
    client: AsyncClient, job: Job
) -> None:
    """A closed stream frees capacity for the next viewer.

    With a cap of one and a broker whose Redis is dead, the first stream
    opens, degrades to an empty body and closes — and the second viewer then
    gets in rather than meeting a permanently exhausted cap.
    """
    broker = _test_broker(_DeadRedis(), max_streams=1)
    token = create_stream_token(job.id, job.tenant_id)

    with mock.patch.object(progress_broker, "_broker", broker):
        for _ in range(2):
            resp = await asyncio.wait_for(
                client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
                timeout=5,
            )
            assert resp.status_code == 200
    assert broker.active_streams == 0


async def test_redis_outage_degrades_the_stream_and_never_500s(
    client: AsyncClient, job: Job
) -> None:
    """Fail-open, unchanged: Redis down closes the stream, it does not error.

    The browser's EventSource reconnects on its own. What must never happen
    is the Redis failure escaping the generator as a 500 out of the API.
    """
    broker = _test_broker(_DeadRedis(), max_streams=0)
    token = create_stream_token(job.id, job.tenant_id)

    with mock.patch.object(progress_broker, "_broker", broker):
        resp = await asyncio.wait_for(
            client.get(f"/api/v1/jobs/{job.id}/stream", params={"token": token}),
            timeout=5,
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert _sse_events(resp.text) == []
