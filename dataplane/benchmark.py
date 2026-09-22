"""Reproducible, in-process, local benchmark: CURRENT DB-first path vs PROPOSED NATS-first path.

    (A) CURRENT:  Orchestrator -> PostgreSQL transaction (ingest_signal) -> outbox row ->
                  OutboxRelay-equivalent -> JetStream publish -> PUBACK
    (B) PROPOSED: Orchestrator -> JetStream publish -> PUBACK -> (async) SignalPersistenceProjector
                  -> PostgreSQL

Both paths run against the SAME in-process fakes (dataplane/fakes.py: FakeConnection,
FailingJetStream wrapping infrastructure.messaging.testing.InMemoryJetStream) with injectable
per-statement/per-publish latency, so neither path gets an unfair advantage from real network or
disk variance that isn't present for the other. Every timing figure produced by this module is
an ARCHITECTURAL COMPARISON PROXY ONLY: no production PostgreSQL, no production NATS, no real
network hop, no real disk fsync is involved anywhere in this process. Do not quote these numbers
as production latency; quote the *relative shape* of the comparison (e.g. "DB-first bus
acceptance is coupled to relay catch-up time; NATS-first bus acceptance is not") instead.

For the DB-first path, this module deliberately does NOT re-drive
infrastructure/messaging/outbox_relay.py's `publish_batch()` SQL (its `... FOR UPDATE SKIP
LOCKED ... ORDER BY ...` statement is not something dataplane/fakes.py's FakeCursor emulates,
and SKIP LOCKED/lease-ownership correctness under concurrent workers is already covered by
tests/test_migration_substrate.py). Instead this module reads `conn.outbox_rows()` directly and
calls the SAME `JetStreamPublisher.publish()` and issues the SAME
`UPDATE platform.outbox_events SET publish_status='PUBLISHED' ...` statement the relay issues,
which is the essential publish+mark-published behavior being compared here.

Run: `python3 -m dataplane.benchmark` (see tests/test_dataplane_benchmark.py for a fast smoke
run). Results are also importable via `run_all_scenarios()` for programmatic use.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from dataplane.distribution import RealtimeConsumer
from dataplane.fakes import FailingJetStream, FakeConnection
from dataplane.latency import LatencyRecorder, LatencyTimestamps
from dataplane.realtime_publisher import REALTIME_SIGNAL_SUBJECT, RealtimePublicationFailed, RealtimeSignalPublisher
from dataplane.signal_projector import SignalPersistenceProjector
from dataplane.wire import decode_envelope
from infrastructure.messaging.contracts import EventEnvelope
from infrastructure.messaging.jetstream import JetStreamPublisher
from migration.signal import canonical_signal, ingest_signal


def _raw(signal_id: str) -> dict[str, Any]:
    return {
        "signal_id": signal_id, "strategy_id": "STRAT-BENCH", "strategy_version": "V1",
        "strategy_instance_id": "inst-bench", "source_event_id": f"{signal_id}:src",
        "symbol": "XAUUSDm", "canonical_symbol": "XAUUSD", "direction": "LONG",
        "signal_timestamp": "2026-09-21T00:00:00Z", "created_at": "2026-09-21T00:00:01Z",
        "signal_emitted_at": "2026-09-21T00:00:01Z", "entry_mechanisms": ["DEPTH_ONLY"],
        "entry_price": 100, "stop_price": 99, "target_price": 102,
        "provenance": {"classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL"},
    }


@dataclass(frozen=True)
class ScenarioConfig:
    name: str
    description: str
    iterations: int = 60
    pg_stmt_latency_ms: float = 0.15
    pg_spike_probability: float = 0.0
    pg_spike_ms: float = 0.0
    js_publish_latency_ms: float = 0.2
    jetstream_outage_for_first_fraction: float = 0.0  # 0..1 of iterations
    backlog: bool = False  # defer relay/projector to the end, then drain as one batch


SCENARIOS: tuple[ScenarioConfig, ...] = (
    ScenarioConfig("warm_steady_state", "Nominal case: low, steady per-statement/publish latency.",
                    pg_stmt_latency_ms=0.1, js_publish_latency_ms=0.15),
    ScenarioConfig("postgres_contention", "Occasional lock-wait spikes on PostgreSQL statements "
                    "(simulated FOR UPDATE contention), JetStream unaffected.",
                    pg_stmt_latency_ms=0.15, pg_spike_probability=0.2, pg_spike_ms=8.0, js_publish_latency_ms=0.15),
    ScenarioConfig("postgres_slowdown", "Sustained elevated PostgreSQL statement latency "
                    "(e.g. degraded storage/replica lag), JetStream unaffected.",
                    pg_stmt_latency_ms=6.0, js_publish_latency_ms=0.15),
    ScenarioConfig("projector_backlog", "Relay/projector deferred and drained as one batch after "
                    "all signals are accepted, modeling a consumer that has fallen behind.",
                    pg_stmt_latency_ms=0.1, js_publish_latency_ms=0.15, backlog=True),
    ScenarioConfig("nats_restart_recovery", "JetStream unavailable for the first half of the run, "
                    "then recovers; PostgreSQL is healthy throughout.",
                    pg_stmt_latency_ms=0.1, js_publish_latency_ms=0.15,
                    jetstream_outage_for_first_fraction=0.5),
)


def _latency_fn(base_ms: float, spike_probability: float, spike_ms: float) -> Callable[[], None]:
    def _sleep() -> None:
        delay = base_ms + (spike_ms if spike_probability and random.random() < spike_probability else 0.0)
        if delay:
            time.sleep(delay / 1000.0)
    return _sleep


def _find_outbox_row(conn: FakeConnection, event_id: str) -> dict[str, Any]:
    row = next((r for r in conn.outbox_rows() if r["event_id"] == event_id), None)
    if row is None:
        raise AssertionError(f"expected outbox row {event_id!r} was not written by ingest_signal")
    return row


def _envelope_from_outbox_row(row: dict[str, Any], occurred_at: str) -> EventEnvelope:
    # Real PostgreSQL/psycopg auto-decodes a jsonb column back into a dict/list; the fake
    # stores exactly what was bound (a JSON string, per ingest_signal's json.dumps(...)::jsonb),
    # so this mirrors that same decode step rather than special-casing the fake in callers.
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return EventEnvelope(row["event_id"], row["event_type"], row["aggregate_type"], row["aggregate_id"],
                          1, occurred_at, payload)


def _mark_outbox_published(conn: FakeConnection, event_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE platform.outbox_events SET publish_status='PUBLISHED' WHERE event_id=%s", (event_id,))
    conn.commit()


# --------------------------------------------------------------------------------------
# DB-first path
# --------------------------------------------------------------------------------------

async def _db_first_accept(conn: FakeConnection, signal_id: str) -> tuple[LatencyTimestamps, Any]:
    """Orchestrator -> PostgreSQL transaction. This IS the DB-first path's acceptance and
    projection step at once: there is no separate projector, so T5/T6 are stamped here too."""
    ts = LatencyTimestamps(path="DB_FIRST")
    ts.t0_decision = ts.t1_orchestrator_accept = time.perf_counter()
    canonical = canonical_signal(_raw(signal_id), runtime_version="signal-orchestrator.v1",
                                  evaluator_version="db-first-benchmark.v1",
                                  stage_id="orchestrator_acceptance", primitive_id="orchestrator.strategy_signal")
    ts.t2_publish_initiated = time.perf_counter()
    ingest_signal(conn, canonical)
    conn.commit()
    ts.t5_projector_begin = ts.t6_projector_commit = time.perf_counter()
    return ts, canonical


async def _db_first_relay(recorder: LatencyRecorder, conn: FakeConnection, js: Any,
                          ts: LatencyTimestamps, canonical: Any) -> None:
    """OutboxRelay-equivalent step: publish the outbox row to JetStream and mark it published.
    T3 (bus PUBACK / durable real-time acceptance) and T4 (distribution can now see it) both
    land here - after, and dependent on, the earlier PostgreSQL commit."""
    event_id = f"{canonical.signal_id}:entry.created"
    row = _find_outbox_row(conn, event_id)
    envelope = _envelope_from_outbox_row(row, canonical.fields["signal_emitted_at"])
    await JetStreamPublisher(js).publish(envelope)
    _mark_outbox_published(conn, event_id)
    ts.t3_puback = ts.t4_distribution_receive = time.perf_counter()
    recorder.record(ts)


async def run_db_first_iteration(recorder: LatencyRecorder, conn: FakeConnection, js: Any,
                                 signal_id: str, *, defer_relay: bool) -> tuple[LatencyTimestamps, Any, bool]:
    """Returns (ts, canonical, relay_succeeded). PostgreSQL commit (T2/T5/T6) always happens
    here regardless of JetStream health - that IS the point of scenario A/D for this path: the
    relay hop can fail independently of, and after, durable PostgreSQL acceptance. A failed
    relay attempt is never recorded as a completed sample (no T3/T4 exist yet); the caller
    retries `_db_first_relay` later, which records it once it actually succeeds."""
    ts, canonical = await _db_first_accept(conn, signal_id)
    if defer_relay:
        return ts, canonical, True  # caller drains the relay backlog later
    try:
        await _db_first_relay(recorder, conn, js, ts, canonical)
        return ts, canonical, True
    except ConnectionError:
        return ts, canonical, False


# --------------------------------------------------------------------------------------
# NATS-first path
# --------------------------------------------------------------------------------------

async def run_nats_first_iteration(recorder: LatencyRecorder, publisher: RealtimeSignalPublisher,
                                   projector: SignalPersistenceProjector, distribution: RealtimeConsumer,
                                   js: Any, signal_id: str, *, defer_projection: bool) -> tuple[LatencyTimestamps, bytes]:
    ts = LatencyTimestamps(path="NATS_FIRST")
    ts.t0_decision = ts.t1_orchestrator_accept = time.perf_counter()
    canonical = publisher.canonicalize(_raw(signal_id))

    def _mark_t2() -> None:
        ts.t2_publish_initiated = time.perf_counter()

    await publisher.publish(_raw(signal_id), canonical=canonical, on_publish_initiated=_mark_t2)
    ts.t3_puback = time.perf_counter()
    payload = js.messages[REALTIME_SIGNAL_SUBJECT][-1].payload  # the message just appended
    await distribution.handle(payload)
    ts.t4_distribution_receive = time.perf_counter()
    if defer_projection:
        recorder.record(ts)  # strategy_to_bus/ack/distribution metrics recorded now; projection later
        return ts, payload
    envelope = decode_envelope(payload)
    ts.t5_projector_begin = time.perf_counter()
    await projector.handle(envelope)
    ts.t6_projector_commit = time.perf_counter()
    recorder.record(ts)
    return ts, payload


async def _drain_nats_first_projector_backlog(recorder: LatencyRecorder, projector: SignalPersistenceProjector,
                                              pending: list[tuple[LatencyTimestamps, bytes]]) -> None:
    for ts, payload in pending:
        envelope = decode_envelope(payload)
        ts.t5_projector_begin = time.perf_counter()
        await projector.handle(envelope)
        ts.t6_projector_commit = time.perf_counter()
        recorder.record(ts)  # re-record: now carries end_to_end_projection_ms too


async def _drain_db_first_relay_backlog(recorder: LatencyRecorder, conn: FakeConnection, js: Any,
                                        pending: list[tuple[LatencyTimestamps, Any]]) -> None:
    for ts, canonical in pending:
        await _db_first_relay(recorder, conn, js, ts, canonical)


# --------------------------------------------------------------------------------------
# Scenario runner
# --------------------------------------------------------------------------------------

async def run_scenario(cfg: ScenarioConfig) -> dict[str, Any]:
    recorder = LatencyRecorder()
    outage_cutoff = int(cfg.iterations * cfg.jetstream_outage_for_first_fraction)

    # --- DB-first ---
    db_conn = FakeConnection(latency_fn=_latency_fn(cfg.pg_stmt_latency_ms, cfg.pg_spike_probability, cfg.pg_spike_ms))
    db_js = FailingJetStream(latency_fn=_latency_fn(cfg.js_publish_latency_ms, 0.0, 0.0))
    db_pending: list[tuple[LatencyTimestamps, Any]] = []
    db_retry: list[tuple[LatencyTimestamps, Any]] = []
    db_failures = 0
    for i in range(cfg.iterations):
        db_js.available = not (i < outage_cutoff)
        signal_id = f"{cfg.name}-db-{i}"
        if cfg.backlog:
            ts, canonical, _ = await run_db_first_iteration(recorder, db_conn, db_js, signal_id, defer_relay=True)
            db_pending.append((ts, canonical))
            continue
        ts, canonical, relayed = await run_db_first_iteration(recorder, db_conn, db_js, signal_id, defer_relay=False)
        if not relayed:
            # PostgreSQL already committed the signal durably; only the relay hop failed.
            # It remains queued in the outbox for the next relay attempt (at-least-once).
            db_failures += 1
            db_retry.append((ts, canonical))
    db_js.available = True
    if cfg.backlog:
        await _drain_db_first_relay_backlog(recorder, db_conn, db_js, db_pending)
    elif db_retry:
        # Outage recovered: retry the relay step for whatever never got published. This reuses
        # the same _db_first_relay() call, so the retried sample is recorded exactly like any
        # other successful one (its T3/T4 simply land later, after the outage window).
        await _drain_db_first_relay_backlog(recorder, db_conn, db_js, db_retry)

    # --- NATS-first ---
    nats_js = FailingJetStream(latency_fn=_latency_fn(cfg.js_publish_latency_ms, 0.0, 0.0))
    nats_conn = FakeConnection(latency_fn=_latency_fn(cfg.pg_stmt_latency_ms, cfg.pg_spike_probability, cfg.pg_spike_ms))
    publisher = RealtimeSignalPublisher(JetStreamPublisher(nats_js))
    projector = SignalPersistenceProjector(lambda: nats_conn)
    distribution = RealtimeConsumer(consumer_name="distribution-service")
    nats_pending: list[tuple[LatencyTimestamps, bytes]] = []
    nats_rejections = 0
    for i in range(cfg.iterations):
        nats_js.available = not (i < outage_cutoff)
        signal_id = f"{cfg.name}-nats-{i}"
        try:
            if cfg.backlog:
                ts, payload = await run_nats_first_iteration(recorder, publisher, projector, distribution, nats_js,
                                                              signal_id, defer_projection=True)
                nats_pending.append((ts, payload))
            else:
                await run_nats_first_iteration(recorder, publisher, projector, distribution, nats_js,
                                                signal_id, defer_projection=False)
        except RealtimePublicationFailed:
            # Never silently accepted: no PostgreSQL row, no distribution delivery, no PUBACK.
            # The orchestrator retries the identical StrategySignal once JetStream recovers,
            # per scenario D/F - deterministic signal_id makes the retry safe.
            nats_rejections += 1
    nats_js.available = True
    if nats_rejections:
        for i in range(outage_cutoff):
            signal_id = f"{cfg.name}-nats-{i}"
            try:
                if cfg.backlog:
                    ts, payload = await run_nats_first_iteration(recorder, publisher, projector, distribution, nats_js,
                                                                  signal_id, defer_projection=True)
                    nats_pending.append((ts, payload))
                else:
                    await run_nats_first_iteration(recorder, publisher, projector, distribution, nats_js,
                                                    signal_id, defer_projection=False)
            except RealtimePublicationFailed:
                pass  # already retried once; steady state assumed recovered for the benchmark
    if cfg.backlog:
        await _drain_nats_first_projector_backlog(recorder, projector, nats_pending)

    return {
        "scenario": cfg.name,
        "description": cfg.description,
        "iterations": cfg.iterations,
        "db_first_relay_failures_during_outage": db_failures,
        "nats_first_rejections_during_outage": nats_rejections,
        "distributions": recorder.report(),
    }


async def run_all_scenarios(scenarios: tuple[ScenarioConfig, ...] = SCENARIOS) -> list[dict[str, Any]]:
    return [await run_scenario(cfg) for cfg in scenarios]


def _format_report(results: list[dict[str, Any]]) -> str:
    lines = ["NATS-first vs DB-first signal data-plane: LOCAL, IN-PROCESS BENCHMARK",
             "(architectural comparison proxy only - not production latency)", ""]
    for entry in results:
        lines.append(f"## {entry['scenario']} ({entry['iterations']} iterations/path)")
        lines.append(entry["description"])
        if entry["db_first_relay_failures_during_outage"]:
            lines.append(f"  DB-first: {entry['db_first_relay_failures_during_outage']} relay publish(es) "
                          f"failed during the simulated outage (PostgreSQL commit still succeeded each time).")
        if entry["nats_first_rejections_during_outage"]:
            lines.append(f"  NATS-first: {entry['nats_first_rejections_during_outage']} publish(es) rejected "
                          f"outright during the simulated outage (no PostgreSQL row, no distribution, no PUBACK).")
        for key in sorted(entry["distributions"]):
            d = entry["distributions"][key]
            lines.append(f"  {key:45s} n={d['n']:4d}  p50={d['p50']:7.3f}ms  p95={d['p95']:7.3f}ms  "
                         f"p99={d['p99']:7.3f}ms  max={d['max']:7.3f}ms")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    results = asyncio.run(run_all_scenarios())
    print(_format_report(results))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
