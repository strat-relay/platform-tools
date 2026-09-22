# Canonical signal authority path

## Current creation and acceptance boundary

The active Context path is:

1. `context_structure_retrace_forward.py::_fill` records a completed/fill
   observation in the runner's state/event output. Decision functions and
   their frozen fingerprint are unchanged.
2. `ContextStructureRetraceAdapter.discover_new_signals` in
   `orchestration/adapters/context_structure_retrace.py` reads compact runner
   state, applies the existing prospective/identity/replay checks, and returns
   `StrategySignal` objects. Liquidity uses its corresponding adapter.
3. `signal_orchestrator.poll_once` receives those already-accepted signals.
   Historically its first durable acceptance action was
   `OrchestrationStore.append("signals", ...)`; this is the precise boundary
   immediately before routing and downstream orchestration dispositions.

In `DB_PRIMARY`, this boundary calls
`CanonicalSignalPublisher.publish(StrategySignal)` instead. It maps the
accepted object through the existing canonical EntrySignal contract and calls
`migration.signal.ingest_signal` in one PostgreSQL transaction. The
transaction persists the evaluation/trace, candidate and EntrySignal, ordered
mechanism children, generic signal projection, and both outbox events. A DB
failure escapes before the signal is treated as processed or routed. The
publisher has no NATS client and performs no network publication.

The accepted `StrategySignal` represents P2's accepted EntrySignal, not a
customer-published signal. Existing route decisions and the internal
distribution-queue disposition remain downstream behavior; they do not
constitute customer distribution. The broader domain distinction remains
`StrategyOpportunity → EntrySignal → PublicationDecision → PublishedSignal`.
P2 currently creates an EntrySignal from its accepted StrategySignal and
records internal route/distribution dispositions; it does not implement
customer `PublishedSignal` delivery.

## Modes

`SIGNAL_AUTHORITY_MODE` is independent of execution mode:

| Mode | Signal authority | Signal file behavior |
|---|---|---|
| `LEGACY_FILE` (default when both primary flags are false) | Existing orchestrator signal JSONL | Existing write/read behavior |
| `DB_SHADOW` | Legacy file remains authority; the separate migration tailer may observe it | Existing file behavior remains |
| `DB_PRIMARY` | PostgreSQL transaction is authority; outbox is the sole NATS source | No canonical `signals.jsonl` read or write |

`DB_PRIMARY` requires both `SIGNAL_DB_PRIMARY_ENABLED=true` and
`SIGNAL_JETSTREAM_PRIMARY_ENABLED=true`; inconsistent combinations fail
closed. It requires schema migration 012, an explicit `SIGNAL_CUTOFF_ID` and
timezone-aware `SIGNAL_CUTOFF_UTC`, `NATS_URL`, and a working PostgreSQL
connection. `SIGNAL_SOURCE_ID` is optional and defaults to
`signal-orchestrator`. The cutoff ID/time are configuration from the separately
persisted T0 record. The publisher never samples a legacy file EOF. It rejects
signals whose source `signal_timestamp` predates T0.

The orchestrator needs its existing `orchestration/config/platform.json`
strategy registry and the corresponding adapter inputs. Today the adapters
identify strategy/version in `StrategySignal`; the contract stores version as
text and does not require a StrategyVersion FK or ParameterSet row. The
producer has no ParameterSet identity, so its existing
`LEGACY_IMPLICIT_IN_STRATEGY_ID` status is retained. No runtime history or
strategy data is seeded from prior JSONL rows.

The standalone relay runs as `python -m scripts.signal_outbox_relay` under a
process supervisor with the same PostgreSQL configuration plus `NATS_URL` and
optional `P2_NATS_USER` / `P2_NATS_PASSWORD`. It verifies migration 012 and
the `TRADING_CORE` stream. It selects pending/failed outbox rows, publishes
canonical envelopes with `Nats-Msg-Id=event_id`, and marks success only after
JetStream acknowledges. Failures remain retryable; restart resumes from the
outbox. Transport is at-least-once, with inbox/idempotency preventing duplicate
domain effects; exactly-once network delivery is not claimed. NATS unavailability
does not undo a committed PostgreSQL signal.

`LegacySignalTailer` / `AppendOnlyTailer` are explicitly migration tooling
(`PRODUCTION_PRIMARY_COMPONENT = False`, `MIGRATION_TOOLING = True`). They are
not used by the DB_PRIMARY publisher. The tailer can be stopped without
affecting the canonical accepted-signal write.

## File IPC boundary

`DB_PRIMARY` has zero dependency on the legacy `signals.jsonl`: it neither
reads nor writes that stream. Existing `LEGACY_FILE` and `DB_SHADOW` behavior
is preserved. The frozen Context runner and its current adapter still exchange
compact strategy state through their pre-existing runtime files; replacing
that upstream strategy-host seam is separate from this accepted-signal
authority change. No frozen strategy decision code was modified.

## PostgreSQL target evidence

The checked-in deployment configuration distinguishes two things:

* `deploy/p2_shadow/resources.yaml` provisions an explicitly isolated
  `p2-signal-shadow-postgres` StatefulSet/PVC and database `p2_signal_shadow`.
  It is migration tooling, not identified as canonical production state.
* `compose.yaml` defines the platform PostgreSQL service, default database
  `trading_platform`; `docs/POSTGRES_SYSTEM_OF_RECORD.md` and
  `docs/DOCKER_DEPLOYMENT.md` describe Compose as the platform's normal
  deployment model. This is the repository's logical canonical target. No
  production host/DSN or production Compose deployment is checked in.

Therefore the target classification is **B at the logical configuration
level**: use the platform PostgreSQL service/database (`postgres` /
`trading_platform`) through deployment-supplied `TRADING_POSTGRES_DSN` or
`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, and `PGPASSWORD`. The live
`p2_signal_shadow` instance must not be renamed or copied into; deployment
configuration needs to point the orchestrator and independent relay at the
canonical platform DB. No runtime data transfer is part of that transition.
Authoritative writers reject implicit libpq defaults: use a DSN or provide all
five `PG*` values explicitly.

## Delivery and publication boundary

P2's accepted StrategySignal maps to EntrySignal creation in PostgreSQL. The
existing `route_signal` remains a later orchestration decision and its internal
queue is not a public signal. Customer publication and its
PublicationDecision/PublishedSignal records remain future domain work; this
change does not add distribution.

## Change scope

This is preparation only. It does not apply migration 012, establish T0, alter
Kubernetes, flip authority flags, start Trade Manager/execution, or change MT5.
`DB_PRIMARY` requires an already-existing T0 record's ID and UTC supplied by
deployment. The previous shadow cutoff utility remains useful to document the
end of legacy authority but is not an input cursor for the new publisher.
