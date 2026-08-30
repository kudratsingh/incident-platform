"""Ordering that does not depend on `created_at` alone (WO-R2-58).

`TimestampMixin.created_at` defaults to `func.now()`, which Postgres
resolves to `transaction_timestamp()` — one value for the whole
transaction. Every row a request writes therefore shares it, and any
`ORDER BY created_at` over those rows is a *total tie* that the server
breaks however the storage layer feels like.

Three places cared:

  * `SagaRepository.completed_steps` derived the compensation rollback
    order from it, so "undo the most recent success first" was a
    coin flip;
  * `SagaRepository.jobs` is what the API returns as a saga's `steps`, so
    the tie decided what the detail view rendered first;
  * `JobRepository.list_jobs` paginates OFFSET/LIMIT over it, so a row
    could appear on two pages or on none.

The first two need *declaration order* and the third only needs
*determinism*, and conflating them is its own bug: appending a uuid
tiebreaker to a tie buys determinism, which is the whole fix for
pagination but actively wrong for a rendered step list — it yields a
stable order that is stably incorrect. Steps sort by `_STEP_ORDER`,
pages sort by `(clock, id)`.

SQLite happens to return insertion order for a tie, which is exactly why
these tests do NOT insert in the order they assert: a test that inserts
in declaration order passes on SQLite before the fix and proves nothing.
Rows are written in a deliberately scrambled order here, standing in for
the arbitrary heap order a real server is free to return.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.core.security import hash_password
from app.models.enums import JobStatus, SagaStatus, UserRole
from app.models.job import Job
from app.models.saga import Saga
from app.models.user import User
from app.repositories.job import JobRepository
from app.repositories.saga import SagaRepository
from app.services.saga import SagaService, SagaStep
from sqlalchemy.ext.asyncio import AsyncSession

# One timestamp shared by every row a transaction writes — the condition
# `transaction_timestamp()` creates for real, forced here so the tie is
# certain rather than merely likely.
_TIED = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


async def _user(db_session: AsyncSession, tenant_id: uuid.UUID) -> User:
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email=f"{uuid.uuid4()}@test.example",
        hashed_password=hash_password("password123"),
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def test_completed_steps_are_ordered_by_declaration_not_storage_order(
    db_session: AsyncSession, default_tenant
) -> None:
    """Compensation order is a correctness question, not a cosmetic one.

    The three steps are written back-to-front so that "whatever order the
    storage layer returns" is provably not declaration order. Before the
    fix `completed_steps` ordered by the tied `created_at` alone and
    handed the coordinator step_c, step_a, step_b — so `reversed()` rolled
    back step_b *before* step_c, undoing work out of order.
    """
    user = await _user(db_session, default_tenant.id)
    saga = Saga(
        id=uuid.uuid4(),
        tenant_id=default_tenant.id,
        name="ordered",
        status=SagaStatus.RUNNING,
        created_at=_TIED,
    )
    db_session.add(saga)
    await db_session.flush()

    declared = ["step_a", "step_b", "step_c"]
    for index, step_type in [(2, "step_c"), (0, "step_a"), (1, "step_b")]:
        db_session.add(
            Job(
                id=uuid.uuid4(),
                tenant_id=default_tenant.id,
                user_id=user.id,
                type=step_type,
                status=JobStatus.COMPLETED,
                priority=0,
                payload={},
                retry_count=0,
                max_retries=3,
                saga_id=saga.id,
                saga_step_index=index,
                created_at=_TIED,
                updated_at=_TIED,
            )
        )
    await db_session.flush()

    steps = await SagaRepository(db_session).completed_steps(saga.id)
    assert [s.type for s in steps] == declared
    # Which is what makes the coordinator's `reversed()` mean "undo the most
    # recent success first".
    assert [s.type for s in reversed(steps)] == ["step_c", "step_b", "step_a"]


async def test_saga_jobs_returns_declaration_order_with_compensations_last(
    db_session: AsyncSession, default_tenant
) -> None:
    """`jobs()` is what the API returns as a saga's `steps`, so its order is
    part of the contract — step 1 renders first.

    The regression this pins is a *stable random* order, which is subtler
    than the arbitrary one it replaced: giving a tied `created_at` a uuid
    tiebreaker makes repeated reads agree with each other while agreeing
    with nothing else. The ids below are assigned in reverse declaration
    order for exactly that reason — under `(created_at, id)` this saga comes
    back backwards, every time, deterministically wrong.

    The `.compensate` row carries no index by design and must land last,
    below the steps it undoes.
    """
    user = await _user(db_session, default_tenant.id)
    saga = Saga(
        id=uuid.uuid4(),
        tenant_id=default_tenant.id,
        name="rendered",
        status=SagaStatus.RUNNING,
        created_at=_TIED,
    )
    db_session.add(saga)
    await db_session.flush()

    # id descends as the step index ascends.
    steps = [
        ("step_a", 0, "aaaaaaaa-0000-0000-0000-00000000000c"),
        ("step_b", 1, "aaaaaaaa-0000-0000-0000-00000000000b"),
        ("step_c", 2, "aaaaaaaa-0000-0000-0000-00000000000a"),
    ]
    for step_type, index, job_id in steps:
        db_session.add(
            Job(
                id=uuid.UUID(job_id),
                tenant_id=default_tenant.id,
                user_id=user.id,
                type=step_type,
                status=JobStatus.COMPLETED,
                priority=0,
                payload={},
                retry_count=0,
                max_retries=3,
                saga_id=saga.id,
                saga_step_index=index,
                created_at=_TIED,
                updated_at=_TIED,
            )
        )
    # A compensation row: no index, minted later than the steps.
    db_session.add(
        Job(
            id=uuid.UUID("aaaaaaaa-0000-0000-0000-0000000000ff"),
            tenant_id=default_tenant.id,
            user_id=user.id,
            type="step_c.compensate",
            status=JobStatus.PENDING,
            priority=0,
            payload={},
            retry_count=0,
            max_retries=3,
            saga_id=saga.id,
            saga_step_index=None,
            created_at=_TIED + timedelta(minutes=5),
            updated_at=_TIED + timedelta(minutes=5),
        )
    )
    await db_session.flush()

    rows = await SagaRepository(db_session).jobs(saga.id)
    assert [j.type for j in rows] == [
        "step_a",
        "step_b",
        "step_c",
        "step_c.compensate",
    ]


async def test_saga_service_stamps_step_index_in_declaration_order() -> None:
    """The sequence has to be written at creation — it is the only record of
    declaration order once the rows all share a timestamp."""
    job_service = AsyncMock()
    job_service.create_job.side_effect = lambda **_: MagicMock(id=uuid.uuid4())
    saga_repo = AsyncMock()
    saga_repo.create.return_value = MagicMock(id=uuid.uuid4())

    svc = SagaService(saga_repo, job_service, AsyncMock())
    await svc.create_saga(
        user_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="three-step",
        steps=[SagaStep(type=t) for t in ("step_a", "step_b", "step_c")],
    )

    calls = job_service.create_job.await_args_list
    assert [c.kwargs["job_type"] for c in calls] == ["step_a", "step_b", "step_c"]
    assert [c.kwargs["saga_step_index"] for c in calls] == [0, 1, 2]


@pytest.mark.parametrize("page_size", [2, 3])
async def test_list_jobs_pagination_is_stable_over_tied_timestamps(
    db_session: AsyncSession, default_tenant, page_size: int
) -> None:
    """Every row exactly once across pages, over a set that is entirely tied.

    The ids are sequential and the rows are inserted ascending, so the
    ordering the fix promises (`created_at DESC, id DESC`) is the exact
    reverse of the insertion order SQLite returns for a bare tie — the
    assertion cannot pass by accident on the untied query.
    """
    user = await _user(db_session, default_tenant.id)
    # Ascending ids, and deliberately not all-digit: SQLite's NUMERIC affinity
    # turns a 32-hex-digit string that happens to be all digits into an
    # integer, and the UUID result processor then chokes on it.
    ids = [uuid.UUID(f"aaaaaaaa-0000-0000-0000-00000000000{n}") for n in range(1, 7)]
    for job_id in ids:
        db_session.add(
            Job(
                id=job_id,
                tenant_id=default_tenant.id,
                user_id=user.id,
                type="csv_upload",
                status=JobStatus.PENDING,
                priority=0,
                payload={},
                retry_count=0,
                max_retries=3,
                created_at=_TIED,
                updated_at=_TIED,
            )
        )
    await db_session.flush()

    repo = JobRepository(db_session)
    seen: list[uuid.UUID] = []
    for offset in range(0, len(ids), page_size):
        page, total = await repo.list_jobs(
            tenant_id=default_tenant.id,
            offset=offset,
            limit=page_size,
            user_id=user.id,
        )
        assert total == len(ids)
        seen.extend(j.id for j in page)

    assert seen == sorted(ids, reverse=True)
    assert len(set(seen)) == len(ids)
