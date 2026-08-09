"""Unit tests for JobService — repositories are mocked, no DB needed."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.exceptions import AuthorizationError, JobError, NotFoundError
from app.models.enums import JobStatus, JobType, UserRole
from app.models.job import Job
from app.services.job import JobService


def _make_job(**kwargs: object) -> Job:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "tenant_id": uuid.uuid4(),
        "type": JobType.CSV_UPLOAD,
        "status": JobStatus.PENDING,
        "idempotency_key": None,
        "retry_count": 0,
        "max_retries": 3,
        "priority": 0,
        "trace_id": None,
        "payload": None,
        "result": None,
        "error_message": None,
    }
    defaults.update(kwargs)
    job = MagicMock(spec=Job)
    for k, v in defaults.items():
        setattr(job, k, v)
    return job  # type: ignore[return-value]


def _make_service() -> tuple[JobService, AsyncMock, AsyncMock, AsyncMock]:
    job_repo = AsyncMock()
    # The idempotency-race guard wraps job_repo.create in
    # `async with session.begin_nested()`. Give the mock a working
    # savepoint context so the wrapping doesn't break every other test.
    savepoint_ctx = MagicMock()
    savepoint_ctx.__aenter__ = AsyncMock(return_value=None)
    savepoint_ctx.__aexit__ = AsyncMock(return_value=False)
    job_repo.session = MagicMock()
    job_repo.session.begin_nested = MagicMock(return_value=savepoint_ctx)

    audit_repo = AsyncMock()
    outbox_repo = AsyncMock()
    redis = AsyncMock()
    svc = JobService(job_repo, audit_repo, outbox_repo, redis)
    return svc, job_repo, audit_repo, outbox_repo


# ---------------------------------------------------------------------------
# create_job
# ---------------------------------------------------------------------------


async def test_create_job_success() -> None:
    svc, job_repo, audit_repo, _ = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    new_job = _make_job()
    job_repo.create.return_value = new_job

    result = await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
    )

    job_repo.create.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    assert result is new_job


async def test_create_job_idempotency_returns_existing() -> None:
    svc, job_repo, _, _ = _make_service()
    existing = _make_job(idempotency_key="key-123")
    job_repo.get_by_idempotency_key.return_value = existing

    result = await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        job_type=JobType.CSV_UPLOAD,
        idempotency_key="key-123",
    )

    job_repo.create.assert_not_awaited()
    assert result is existing


async def test_create_job_idempotency_race_returns_winner() -> None:
    """The check-then-insert race: two concurrent requests with the same key
    both pass the pre-check (get_by_idempotency_key returns None), both
    reach the create, and the DB rejects the loser with IntegrityError.

    The loser must catch it, re-fetch by key, and return the winner's row
    — never surface a 500 to the caller.
    """
    from sqlalchemy.exc import IntegrityError

    svc, job_repo, audit_repo, outbox_repo = _make_service()
    tenant_id = uuid.uuid4()
    winner = _make_job(idempotency_key="key-123", tenant_id=tenant_id)

    # Pre-check misses (this is the racer that lost the check-then-insert race).
    # Post-collision re-fetch finds the winner.
    job_repo.get_by_idempotency_key.side_effect = [None, winner]
    job_repo.create.side_effect = IntegrityError(
        "duplicate key value violates unique constraint",
        params={},
        orig=Exception("uq"),
    )

    result = await svc.create_job(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        job_type=JobType.CSV_UPLOAD,
        idempotency_key="key-123",
    )

    # Loser returned the winner's row. No audit / outbox side-effects
    # happened for the loser — they belong to the winner's request.
    assert result is winner
    audit_repo.log.assert_not_awaited()
    outbox_repo.add.assert_not_awaited()


async def test_create_job_reraises_integrity_when_no_idempotency_key() -> None:
    """If we hit IntegrityError but the caller didn't use an idempotency
    key, it's not the race — surface the error rather than silently
    returning nothing."""
    from sqlalchemy.exc import IntegrityError

    svc, job_repo, _, _ = _make_service()
    job_repo.get_by_idempotency_key.return_value = None
    job_repo.create.side_effect = IntegrityError(
        "some other unique violation", params={}, orig=Exception("x")
    )

    with pytest.raises(IntegrityError):
        await svc.create_job(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            job_type=JobType.CSV_UPLOAD,
            # No idempotency_key
        )


async def test_create_job_reraises_integrity_when_refetch_finds_nothing() -> None:
    """If we hit IntegrityError with an idempotency key BUT the re-fetch
    still returns None, the constraint violation is something else (e.g.
    an FK we didn't validate). Surface it — silently swallowing would
    lose the diagnostic."""
    from sqlalchemy.exc import IntegrityError

    svc, job_repo, _, _ = _make_service()
    # Pre-check misses, and re-fetch also misses.
    job_repo.get_by_idempotency_key.side_effect = [None, None]
    job_repo.create.side_effect = IntegrityError(
        "FK violation on user_id", params={}, orig=Exception("fk")
    )

    with pytest.raises(IntegrityError):
        await svc.create_job(
            user_id=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            job_type=JobType.CSV_UPLOAD,
            idempotency_key="key-race",
        )


# ---------------------------------------------------------------------------
# get_job
# ---------------------------------------------------------------------------


async def test_get_job_owner_can_access() -> None:
    svc, job_repo, _, _ = _make_service()
    owner_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    job = _make_job(user_id=owner_id, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = job

    result = await svc.get_job(job.id, owner_id, UserRole.USER, tenant_id)
    assert result is job


async def test_get_job_non_owner_raises() -> None:
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    job = _make_job(user_id=uuid.uuid4(), tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = job

    with pytest.raises(AuthorizationError):
        await svc.get_job(job.id, uuid.uuid4(), UserRole.USER, tenant_id)


async def test_get_job_admin_can_access_any() -> None:
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    job = _make_job(user_id=uuid.uuid4(), tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = job

    result = await svc.get_job(job.id, uuid.uuid4(), UserRole.ADMIN, tenant_id)
    assert result is job


async def test_get_job_not_found_raises() -> None:
    svc, job_repo, _, _ = _make_service()
    job_repo.get_for_tenant.return_value = None

    with pytest.raises(NotFoundError):
        await svc.get_job(uuid.uuid4(), uuid.uuid4(), UserRole.ADMIN, uuid.uuid4())


# ---------------------------------------------------------------------------
# replay_job
# ---------------------------------------------------------------------------


async def test_replay_failed_job() -> None:
    svc, job_repo, audit_repo, _ = _make_service()
    tenant_id = uuid.uuid4()
    failed_job = _make_job(status=JobStatus.FAILED, tenant_id=tenant_id)
    replayed_job = _make_job(
        status=JobStatus.PENDING, id=failed_job.id, tenant_id=tenant_id
    )
    job_repo.get_for_tenant.return_value = failed_job
    job_repo.update_status.return_value = replayed_job

    result = await svc.replay_job(
        failed_job.id, tenant_id, requesting_user_id=uuid.uuid4()
    )

    job_repo.update_status.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    assert result is replayed_job


async def test_replay_resets_retry_count_and_error() -> None:
    """A DLQ'd job at max retries must get a clean retry budget on replay,
    or the next failure dead-letters it again immediately."""
    svc, job_repo, audit_repo, _ = _make_service()
    tenant_id = uuid.uuid4()
    dead_job = _make_job(
        status=JobStatus.DEAD_LETTER,
        retry_count=3,
        max_retries=3,
        error_message="last attempt boom",
        tenant_id=tenant_id,
    )
    job_repo.get_for_tenant.return_value = dead_job
    job_repo.update_status.return_value = dead_job

    await svc.replay_job(dead_job.id, tenant_id, requesting_user_id=uuid.uuid4())

    extra = job_repo.update_status.await_args.kwargs["extra"]
    assert extra["retry_count"] == 0
    assert extra["error_message"] is None
    assert extra["result"] is None

    audit_extra = audit_repo.log.await_args.kwargs["extra_data"]
    assert audit_extra["previous_retry_count"] == 3
    assert audit_extra["previous_status"] == JobStatus.DEAD_LETTER


async def test_replay_non_failed_job_raises() -> None:
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    running_job = _make_job(status=JobStatus.RUNNING, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = running_job

    with pytest.raises(JobError, match="replayed"):
        await svc.replay_job(
            running_job.id, tenant_id, requesting_user_id=uuid.uuid4()
        )


async def test_replay_from_service_account_writes_audit_with_null_user_id() -> None:
    """Regression: the SA path used to pass the service_account.id as
    requesting_user_id, which then went into audit_logs.user_id and
    violated the users FK. Fixed by routing the SA id to principal_id
    and NULL'ing user_id when principal_type != 'user'. The MCP tools
    (replay_dlq_by_ids, replay_dlq_by_category, replay_dlq_messages)
    plus the DLQ-replay promote loop all hit this path."""
    svc, job_repo, audit_repo, _ = _make_service()
    tenant_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    dead_job = _make_job(status=JobStatus.DEAD_LETTER, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = dead_job
    job_repo.update_status.return_value = dead_job

    await svc.replay_job(
        dead_job.id,
        tenant_id,
        principal_type="service_account",
        principal_id=sa_id,
    )

    kwargs = audit_repo.log.await_args.kwargs
    assert kwargs["user_id"] is None, (
        "SA replay must NULL user_id — else the users FK trips"
    )
    assert kwargs["principal_type"] == "service_account"
    assert kwargs["principal_id"] == sa_id


async def test_replay_invalidates_job_cache() -> None:
    """E2-02: invalidation must live in the SERVICE layer, not only in the
    REST admin wrapper — otherwise the three MCP replay tools and the
    scheduled DLQ-replay loop leave `GET /jobs/{id}` serving stale status
    for a whole TTL."""
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    dead_job = _make_job(status=JobStatus.DEAD_LETTER, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = dead_job
    job_repo.update_status.return_value = dead_job

    await svc.replay_job(dead_job.id, tenant_id, requesting_user_id=uuid.uuid4())

    svc.redis.delete.assert_awaited_once_with(  # type: ignore[attr-defined]
        f"cache:job:{tenant_id}:{dead_job.id}"
    )


# ---------------------------------------------------------------------------
# resolve_incident
# ---------------------------------------------------------------------------


async def test_resolve_incident_invalidates_job_cache() -> None:
    """Same invariant as replay: the service layer owns invalidation."""
    svc, job_repo, _, _ = _make_service()
    tenant_id = uuid.uuid4()
    job = _make_job(status=JobStatus.FAILED, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = job
    job_repo.update_status.return_value = job

    await svc.resolve_incident(job.id, uuid.uuid4(), tenant_id)

    svc.redis.delete.assert_awaited_once_with(  # type: ignore[attr-defined]
        f"cache:job:{tenant_id}:{job.id}"
    )


# ---------------------------------------------------------------------------
# DAG pause enforcement on the replay / create dispatch paths (E1-08)
# ---------------------------------------------------------------------------


async def test_replay_job_refused_while_the_dag_is_paused() -> None:
    """E1-08: `replay_job` is the choke point for the admin endpoint, all
    three MCP replay tools and the scheduled DLQ-replay loop — and none of
    them probed the pause. A replay is a NEW dispatch, so `pause_dag`'s
    "work in flight is not recalled" carve-out does not cover it.

    Refusal has to happen before *any* mutation: a job that flipped to
    PENDING with an audit row and an outbox event has already been
    dispatched, whatever the caller does with the exception.
    """
    svc, job_repo, audit_repo, outbox_repo = _make_service()
    svc.dep_repo = AsyncMock()
    tenant_id = uuid.uuid4()
    dead_job = _make_job(status=JobStatus.DEAD_LETTER, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = dead_job
    paused_by = uuid.uuid4()

    with patch(
        "app.services.job.find_blocking_pause",
        new=AsyncMock(return_value=paused_by),
    ):
        with pytest.raises(JobError) as exc_info:
            await svc.replay_job(
                dead_job.id, tenant_id, requesting_user_id=uuid.uuid4()
            )

    assert "paused" in str(exc_info.value)
    assert str(paused_by) in str(exc_info.value)
    job_repo.update_status.assert_not_awaited()
    audit_repo.log.assert_not_awaited()
    outbox_repo.add.assert_not_awaited()


async def test_replay_job_proceeds_when_no_pause_holds() -> None:
    """The guard is a probe, not a new precondition: an unpaused DAG
    replays exactly as before."""
    svc, job_repo, audit_repo, outbox_repo = _make_service()
    svc.dep_repo = AsyncMock()
    tenant_id = uuid.uuid4()
    dead_job = _make_job(status=JobStatus.DEAD_LETTER, tenant_id=tenant_id)
    job_repo.get_for_tenant.return_value = dead_job
    job_repo.update_status.return_value = dead_job

    with patch(
        "app.services.job.find_blocking_pause", new=AsyncMock(return_value=None)
    ):
        await svc.replay_job(
            dead_job.id, tenant_id, requesting_user_id=uuid.uuid4()
        )

    job_repo.update_status.assert_awaited_once()
    audit_repo.log.assert_awaited_once()
    outbox_repo.add.assert_awaited_once()


async def test_create_job_onto_a_paused_chain_is_held_waiting() -> None:
    """The bypass the audit's sketch missed: a job whose parents are all
    COMPLETED is created straight to PENDING and published immediately —
    even when those parents sit in a paused chain. It must be created
    WAITING instead, which `_resume_unblocked_waiting_loop` promotes once
    the pause lifts (no new resume machinery needed).
    """
    svc, job_repo, _, outbox_repo = _make_service()
    svc.dep_repo = AsyncMock()
    tenant_id = uuid.uuid4()
    parent = _make_job(status=JobStatus.COMPLETED, tenant_id=tenant_id)
    job_repo.get_by_idempotency_key.return_value = None
    job_repo.get_by_id.return_value = parent
    job_repo.create.return_value = _make_job(
        status=JobStatus.WAITING, tenant_id=tenant_id
    )

    with patch(
        "app.services.job.find_blocking_pause",
        new=AsyncMock(return_value=uuid.uuid4()),
    ):
        await svc.create_job(
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            job_type=JobType.CSV_UPLOAD,
            dependencies=[parent.id],
        )

    assert job_repo.create.await_args.kwargs["status"] == JobStatus.WAITING
    outbox_repo.add.assert_not_awaited()


async def test_create_job_with_met_deps_still_promotes_when_unpaused() -> None:
    """Unpaused chain: all-COMPLETED parents still mean PENDING + a
    `job.submitted` row, exactly as before the pause probe."""
    svc, job_repo, _, outbox_repo = _make_service()
    svc.dep_repo = AsyncMock()
    tenant_id = uuid.uuid4()
    parent = _make_job(status=JobStatus.COMPLETED, tenant_id=tenant_id)
    job_repo.get_by_idempotency_key.return_value = None
    job_repo.get_by_id.return_value = parent
    job_repo.create.return_value = _make_job(
        status=JobStatus.PENDING, tenant_id=tenant_id
    )

    with patch(
        "app.services.job.find_blocking_pause", new=AsyncMock(return_value=None)
    ):
        await svc.create_job(
            user_id=uuid.uuid4(),
            tenant_id=tenant_id,
            job_type=JobType.CSV_UPLOAD,
            dependencies=[parent.id],
        )

    assert job_repo.create.await_args.kwargs["status"] == JobStatus.PENDING
    outbox_repo.add.assert_awaited_once()
