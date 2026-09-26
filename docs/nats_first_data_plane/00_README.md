# NATS/JetStream-first real-time data plane (prototype)

Branch: `architecture/nats-first-data-plane` · Worktree: `trading-platform-nats-first` ·
Base commit: `e900823`

## What this is

A prototype, additive, not-wired-in architecture for StratRelay's real-time signal path, built
and evaluated **in parallel** with the PostgreSQL-first ("DB-first") path that is proceeding to
production independently on `main`. This is **not** a claim that DB-first is wrong. It is an
answer to: "if signal acceptance onto the real-time backbone did not require a PostgreSQL commit
first, what would that look like, and is it worth adopting later?"

Nothing here is activated. `SIGNAL_DATA_PLANE_MODE` defaults to `DB_FIRST` ([dataplane/mode.py](../../dataplane/mode.py))
and no production code path calls into `dataplane/`.

## Current (DB-first) vs proposed (NATS-first)

```
CURRENT:  Orchestrator -> PostgreSQL transaction -> transactional outbox -> JetStream -> consumers
PROPOSED: Orchestrator -> JetStream (PUBACK) -> [signal delivery, Trade Manager, V2 execution]
                                              -> PostgreSQL projector -> PostgreSQL
```

## Documents in this set

1. `00_README.md` - this file: index, T0-T6 latency mapping, module map.
2. [`01_AUTHORITY_MODEL.md`](01_AUTHORITY_MODEL.md) - ADR: which system is authoritative for which class of information.
3. [`02_ORDERING.md`](02_ORDERING.md) - what ordering guarantees are actually required, and why the current design does not impose global ordering.
4. [`03_MIGRATION_PATH.md`](03_MIGRATION_PATH.md) - `DB_FIRST` -> `NATS_FIRST` transition without a flag-day rewrite or dual authority.
5. [`04_BENCHMARK_RESULTS.md`](04_BENCHMARK_RESULTS.md) - local, in-process benchmark methodology and results (explicitly not production numbers).
6. [`05_V2_EXECUTION_COMPATIBILITY.md`](05_V2_EXECUTION_COMPATIBILITY.md) - why this architecture doesn't change, weaken, or solve OD-06/broker fencing.
7. [`06_MERGE_CONFLICTS.md`](06_MERGE_CONFLICTS.md) - likely conflicts against `main`.

## What was reused, not rebuilt

This is deliberately **ordering and authority semantics**, not a rewrite:

- `migration.signal.canonical_signal()` - pure canonicalization, used unmodified by both the
  new publisher (canonicalize only) and the new projector (canonicalize + write).
- `migration.signal.ingest_signal()` - the idempotent PostgreSQL writer, used unmodified by the
  projector; it is the same function the DB-first path uses today.
- `migration.signal.load_entry_mechanisms()`, the `strategy.entry_signals` /
  `strategy.entry_signal_mechanisms` / `strategy.candidates` / `strategy.signals` relational
  schema, deterministic `signal_id`, `entry_signal_hash` identity, reason codes, provenance,
  cutoff/source-reference concepts - all reused as-is.
- `infrastructure.messaging.contracts.EventEnvelope`, `JetStreamPublisher`, `validate_subject()`,
  the inbox/idempotency pattern (`claim_inbox`/`mark_inbox_processed`) - reused as-is.
- The one shared-file change is additive: a new subject
  (`realtime.signal.entry.accepted.v1`) and a new stream (`SIGNAL_REALTIME`) registered in
  `infrastructure/messaging/contracts.py`, deliberately outside the `strategy.`/`signal.` prefix
  family so it never entangles with `TRADING_CORE`'s existing subject/retention/consumer set.

## New modules (`dataplane/`)

| Module | Purpose |
|---|---|
| `mode.py` | `SignalDataPlaneMode` (`DB_FIRST`/`NATS_FIRST`) + `SignalDataPlaneFlags`, fail-closed. |
| `realtime_publisher.py` | Orchestrator-side: canonicalize -> deterministic `signal_id` -> JetStream publish -> require PUBACK. No PostgreSQL import. |
| `signal_projector.py` | `SignalPersistenceProjector`: JetStream consumer -> `canonical_signal()` + `ingest_signal()` -> PostgreSQL. Three-layer idempotency (see file docstring). |
| `distribution.py` | `RealtimeConsumer`: the same PostgreSQL-independent consumer shape used for both signal distribution and Trade Manager intake compatibility. Neither is activated. |
| `wire.py` | `decode_envelope()`: the wire-format inverse of `EventEnvelope.canonical_bytes()`. |
| `latency.py` | `LatencyTimestamps`/`LatencyRecorder`: T0-T6 instrumentation shared by both paths. |
| `fakes.py` | In-process `FakeConnection`/`FakeCursor` (matches the exact SQL surface `ingest_signal` issues) and `FailingJetStream`, used by tests and the benchmark. No live PostgreSQL/NATS is reachable in this sandbox. |
| `benchmark.py` | Reproducible local latency comparison; see `04_BENCHMARK_RESULTS.md`. |

## T0-T6 latency instrumentation

```
T0  strategy decision complete           (decision_time / signal_emitted_at instant)
T1  orchestrator accepts signal          (canonicalization begins)
T2  bus publish initiated                (JetStream publish call, or DB tx begin for DB-first)
T3  bus PUBACK / durable acceptance      (JetStream ack; for DB-first, the relay's own later
                                           JetStream PUBACK after its DB commit)
T4  distribution consumer receives       (independent of PostgreSQL)
T5  PostgreSQL projector begins          (NATS-first only; DB-first has no separate "begins" -
                                           T1/T2 already is it)
T6  PostgreSQL projection committed
```

Named metrics: `strategy_to_bus_ms`, `orchestrator_to_bus_ms`, `bus_publish_ack_ms`,
`bus_to_distribution_ms`, `bus_to_db_projection_ms`, `projector_processing_ms`,
`end_to_end_acceptance_ms`, `end_to_end_projection_ms`. See `dataplane/latency.py`.

One asymmetry worth calling out explicitly: for **DB-first**, T6 (PostgreSQL commit) happens
*before* T3 (bus PUBACK) - the write is the primary transaction, and the bus publish is a
downstream relay step. For **NATS-first**, T3 happens *before* T5/T6 - the bus accepts the
signal first, and PostgreSQL projection is asynchronous afterward. `bus_to_db_projection_ms` is
therefore negative for DB-first samples in the benchmark output; that is correct, not a bug, and
is exactly the ordering inversion this whole prototype is about.

## Safety

`BROKER_WRITES=0`. No Kubernetes, no live PostgreSQL, no live NATS, no MT5 configuration, no
Trade Manager activation, no execution activation touched by this branch. See
`05_V2_EXECUTION_COMPATIBILITY.md` for why P5 broker-held authenticated fencing (OD-06) remains
mandatory and unaddressed here.
