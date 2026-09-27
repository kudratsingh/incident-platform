# ADR 0040 — The lag clock survives a restart

**Status:** Accepted · **Date:** 2026-09-27 · **Owner:** Platform · WO-R3-356 · Builds on [ADR 0009](0009-consumer-lifecycle-and-supervision.md), [ADR 0037](0037-a-run-record-carries-the-run.md) and [ADR 0039](0039-the-platform-pages-on-its-own-metric.md)

## Context

The demo's eighth take stopped on a platform stall, not an agent mistake. The api-container log has the whole story:

| UTC | What happened |
|---|---|
| 10:15:32 | last lag sample before the fault's fix |
| 10:15:36.6 | the agent's `restart_consumer_group`; the supervisor's `_restart_consumer` calls `stop()` then `start()` |
| 10:15:37.35 | `kafka consumer stopped` |
| 10:15:37.36 | the new consumer has rejoined and is consuming; the backlog drains within seconds |
| 10:16:17.36 | `consumer_lag query failed: KafkaTimeoutError … 40000 ms` |
| 10:16:22 | the next lag sample, and the platform's own rule resolves the alert |

The metrics pass that started just before the restart was still inside `JobDispatcherConsumer.consumer_lag()` when the supervisor stopped the client it was querying. `end_offsets` / `committed` on a client being torn down did not fail; it waited for the client's 40-s request timeout. The metrics loop is one task, so nothing measured for 50 s, the value key (three passes, 15 s at the demo's 5-s clock) expired, and `get_consumer_lag` read `lag: null, lag_known: false` for about 45 s. The agent's verify gate refused to call the incident resolved without a reading taken after its action — correctly — and the take ran out of patience before the platform produced one. Take 7 did not show this only because its tick fell just before the restart.

## Decision

The metrics pass never waits on a consumer that is restarting, and never waits long on one that is not. Both mechanisms, because each covers a case the other does not:

1. **A `restarting` flag.** `BaseKafkaConsumer.restarting` is `True` for the whole of the supervisor's `_restart_consumer` call — `stop()`, `start()` and any backoff between failed attempts — and is cleared in a `finally`, so a cancelled restart cannot leave it set. While it is set, `consumer_lag()` returns `None` at once without touching Kafka. This covers every pass that *starts* during a restart, including one that would otherwise query a freshly built client whose `start()` has not finished. `restart_consumer_group` needs no change: it only deletes the kill and latency keys, and the restart it causes is this same supervisor call.

2. **A bounded query.** The offset queries (`end_offsets` and every `committed`) run as one coroutine under `asyncio.wait_for(…, timeout=lag_query_timeout_seconds())`, which is **half the metrics interval, capped at 2 s** (`LAG_QUERY_TIMEOUT_CAP_SECONDS`): 2 s at the 60-s default and at the demo's 5 s, 0.5 s at the 1-s floor. This covers the pass that was *already in flight* when the restart began — the flag cannot reach back into it. A timeout returns `None` and logs `consumer_lag query timed out`. Half an interval means a timed-out pass still leaves the next one on schedule.

The first pass whose query succeeds after the restart writes the value key and a ring sample, as every successful pass already did. At the demo's clock that is within two ticks (≤ 10 s) of `kafka consumer started`, and the value key, whose TTL is three passes, is never absent.

`None` still means unknown and is never written as 0: the flag and the timeout both return `None`, and `_metrics_loop` still skips the gauge, the value key and the sample on `None`.

## Rejected

- **The flag alone.** It does not help the pass already waiting on the old client, which is exactly the pass that stalled take 8.
- **The timeout alone.** A pass that starts mid-restart would still spend up to 2 s querying a client with no connection, and on a consumer whose `start()` keeps failing it would spend it every pass. The flag makes that case free and explicit.
- **Lowering the client's `request_timeout_ms`.** It would shorten every Kafka request the consumer makes, including the polls and commits that have nothing to do with the metrics pass, to fix one caller.
- **Querying lag from a separate admin client.** It would survive the restart, but it would report the group's committed offsets whether or not this process's consumer is in the group — a different number from the one `check_backpressure` has always gated on — and it adds a second connection per process for one reading.

## Consequences

- **No contract delta.** No tool name, description, schema, scope or `is_idempotent` flag moves; `tools/list` hashes the same before and after (40 tools). `get_consumer_lag`'s description already says the interval is deployment-configured and that `lag` is null when unknown, which is what a restarting consumer now reports for at most one pass.
- **One new setting-derived number, no new setting.** `lag_query_timeout_seconds` lives in `app/core/consumer_lag.py` beside the interval and the value key's TTL it is derived with ([ADR 0039](0039-the-platform-pages-on-its-own-metric.md)).
- **A slow Kafka now reads as unknown sooner.** A healthy broker that takes longer than 2 s to answer both offset queries produces no sample for that pass. That is the honest reading of a measurement that did not arrive, and at three passes of TTL the value key survives two of them in a row.
- **Proved on a real broker.** `backend/tests/integration/test_lag_clock_survives_restart.py` runs the shipped consumer, supervisor and metrics loop on Redpanda and Redis at the 5-s clock, kills the group, holds one offset query on the old client open forever (take 8's shape), restarts through `restart_consumer_group`, and asserts a sample within 10 s of the new consumer's start and no value-key gap longer than one tick.
