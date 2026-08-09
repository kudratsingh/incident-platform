"""
Unit tests for BaseKafkaConsumer's offset-commit discipline (E1-01).

First-ever unit tests for the base consumer — the absence of this file is
why E1-01 (argument-less commit() committing every assigned partition's
fetch position) survived. The contract under test:

  - every commit names an explicit ``{TopicPartition: offset + 1}`` for
    exactly the processed message's partition — never the argument-less
    ``commit()`` that snapshots all-partition fetch positions
  - a handler failure commits nothing, seeks back to the failed offset, and
    abandons the REST of that partition's batch; other partitions continue
  - poison pills (schema-invalid) are committed past, per-partition
  - a failed seek-back (partition reassigned during a rebalance) is
    swallowed with a warning — the consume loop survives the race
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiokafka import ConsumerRecord, TopicPartition
from aiokafka.errors import CommitFailedError, IllegalStateError
from app.workers.kafka_consumer import BaseKafkaConsumer
from app.workers.schema_registry import SchemaValidationError


def _mk_record(
    topic: str = "test.topic", partition: int = 0, offset: int = 0
) -> ConsumerRecord:
    """Build a ConsumerRecord the way aiokafka's fetcher does (dataclass)."""
    return ConsumerRecord(
        topic=topic,
        partition=partition,
        offset=offset,
        timestamp=0,
        timestamp_type=0,
        key=None,
        value={"n": offset},
        checksum=None,
        serialized_key_size=-1,
        serialized_value_size=2,
        headers=(),
    )


class RecordingConsumer(BaseKafkaConsumer):
    """Records every handle_message attempt; fails scripted (partition, offset)s."""

    def __init__(self, fail_offsets: set[tuple[int, int]] | None = None) -> None:
        super().__init__(topics=["test.topic"], group_id="test-group")
        self.handled: list[tuple[int, int]] = []
        self.fail_offsets = fail_offsets or set()

    async def handle_message(
        self,
        topic: str,
        key: str | None,
        value: dict[str, Any],
        *,
        partition: int = 0,
        offset: int = 0,
    ) -> None:
        self.handled.append((partition, offset))
        if (partition, offset) in self.fail_offsets:
            raise RuntimeError(f"scripted handler failure at p{partition} o{offset}")


def _consumer_with_mock(
    fail_offsets: set[tuple[int, int]] | None = None,
) -> RecordingConsumer:
    consumer = RecordingConsumer(fail_offsets)
    consumer._consumer = AsyncMock()
    # seek() is SYNC in aiokafka — as an AsyncMock attribute it would hand
    # back an unawaited coroutine mock and silently "pass".
    consumer._consumer.seek = MagicMock()
    return consumer


def _committed_offsets(mock: AsyncMock) -> list[dict[TopicPartition, int]]:
    """The offsets dict of every commit call, in call order."""
    return [
        c.args[0] if c.args else c.kwargs["offsets"] for c in mock.commit.await_args_list
    ]


async def test_redelivery_contract_commits_only_past_last_success() -> None:
    """THE E1-01 contract: for a batch [m0 ok, m1 fail, m2] the consumer
    commits exactly {tp: m0.offset + 1}, seeks back to m1.offset, and
    abandons m2 — nothing at or past the failed offset is ever committed."""
    consumer = _consumer_with_mock(fail_offsets={(0, 1)})
    mock = consumer._consumer
    assert isinstance(mock, AsyncMock)
    tp = TopicPartition("test.topic", 0)
    batch = {tp: [_mk_record(offset=0), _mk_record(offset=1), _mk_record(offset=2)]}

    had_failure = await consumer._process_batch(batch)

    assert had_failure is True
    # Every commit must carry an explicit offsets dict — the argument-less
    # form commits the fetch position of EVERY assigned partition (E1-01).
    assert mock.commit.await_args_list, "expected at least one commit"
    assert all(c.args or "offsets" in c.kwargs for c in mock.commit.await_args_list)
    # Only m0's offset is committed; nothing at or past the failed m1.
    assert _committed_offsets(mock) == [{tp: 1}]
    # Seek back to the failed offset exactly once so the next poll redelivers.
    mock.seek.assert_called_once_with(tp, 1)
    # m1 was attempted, m2 abandoned (it is refetched after the seek).
    assert consumer.handled == [(0, 0), (0, 1)]


async def test_partition_isolation_failure_does_not_block_other_partition() -> None:
    """{tpA: [a0 fail], tpB: [b0 ok]} — b0 is still handled and committed;
    seek touches only tpA. Guards against 'abandon the whole batch'."""
    consumer = _consumer_with_mock(fail_offsets={(0, 5)})
    mock = consumer._consumer
    assert isinstance(mock, AsyncMock)
    tp_a = TopicPartition("test.topic", 0)
    tp_b = TopicPartition("test.topic", 1)
    batch = {
        tp_a: [_mk_record(partition=0, offset=5)],
        tp_b: [_mk_record(partition=1, offset=7)],
    }

    had_failure = await consumer._process_batch(batch)

    assert had_failure is True
    assert _committed_offsets(mock) == [{tp_b: 8}]
    mock.seek.assert_called_once_with(tp_a, 5)
    assert consumer.handled == [(0, 5), (1, 7)]


async def test_poison_pill_commits_past_per_partition_and_does_not_stall() -> None:
    """A schema-invalid message is committed past for its own partition,
    the handler never sees it, and the next message still processes."""
    consumer = _consumer_with_mock()
    mock = consumer._consumer
    assert isinstance(mock, AsyncMock)
    tp = TopicPartition("test.topic", 0)
    m0, m1 = _mk_record(offset=3), _mk_record(offset=4)

    def _reject_m0(topic: str, payload: dict[str, Any]) -> None:
        if payload == m0.value:
            raise SchemaValidationError("scripted: m0 is poison")

    with patch("app.workers.kafka_consumer.validate_schema", side_effect=_reject_m0):
        had_failure = await consumer._process_batch({tp: [m0, m1]})

    assert had_failure is False
    assert _committed_offsets(mock) == [{tp: 4}, {tp: 5}]
    assert consumer.handled == [(0, 4)]  # the handler never saw the poison pill
    mock.seek.assert_not_called()


async def test_seek_back_failure_during_rebalance_is_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """seek() raising IllegalStateError (partition reassigned mid-batch) is
    swallowed with a warning; the consume loop survives the rebalance race."""
    consumer = _consumer_with_mock(fail_offsets={(0, 2)})
    mock = consumer._consumer
    assert isinstance(mock, AsyncMock)
    mock.seek = MagicMock(side_effect=IllegalStateError("partition is not assigned"))
    tp = TopicPartition("test.topic", 0)

    with caplog.at_level("WARNING"):
        had_failure = await consumer._process_batch({tp: [_mk_record(offset=2)]})

    assert had_failure is True
    mock.seek.assert_called_once_with(tp, 2)
    assert mock.commit.await_count == 0
    assert any("seek-back failed" in r.getMessage() for r in caplog.records)


async def test_process_one_success_commits_explicit_per_partition_offset() -> None:
    """At HEAD this fails: commit() is awaited argument-less, which commits
    the fetch position of every assigned partition (the E1-01 mechanism)."""
    consumer = _consumer_with_mock()
    mock = consumer._consumer
    assert isinstance(mock, AsyncMock)

    ok = await consumer._process_one(_mk_record(partition=3, offset=41))

    mock.commit.assert_awaited_once_with({TopicPartition("test.topic", 3): 42})
    assert ok is True


async def test_process_one_handler_failure_commits_nothing_and_returns_false() -> None:
    consumer = _consumer_with_mock(fail_offsets={(0, 9)})
    mock = consumer._consumer
    assert isinstance(mock, AsyncMock)

    ok = await consumer._process_one(_mk_record(offset=9))

    assert ok is False
    mock.commit.assert_not_awaited()


async def test_commit_failure_reports_failure_so_caller_seeks_back() -> None:
    """CommitFailedError (e.g. group rebalanced) lands in the same except
    path as a handler failure: _process_one returns False so the caller
    seeks back; reprocessing is safe because handlers are idempotent."""
    consumer = _consumer_with_mock()
    mock = consumer._consumer
    assert isinstance(mock, AsyncMock)
    mock.commit = AsyncMock(side_effect=CommitFailedError("group rebalanced"))

    ok = await consumer._process_one(_mk_record(offset=0))

    assert ok is False
    assert consumer.handled == [(0, 0)]  # handler DID run; only the commit failed
