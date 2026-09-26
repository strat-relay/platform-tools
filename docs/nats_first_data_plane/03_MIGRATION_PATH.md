# Migration path: `DB_FIRST` -> `NATS_FIRST` without a flag-day rewrite

## Principle

One `SignalDataPlaneMode` is active in a given deployment at a given time, and it is the sole
*producer* for the real-time hot path for every signal accepted while it is active. The two
modes are never simultaneously producing for the same signal - that would create dual
authoritative acceptance events for one logical signal, which this design explicitly avoids (the
event IDs are even deliberately distinct strings - `:entry.created` vs `:entry.accepted` - so a
bug that accidentally ran both paths for the same signal would be immediately visible as two
different outbox/stream entries rather than silently merging).

This is a **producer-side** switch, not a data-migration. Nothing about existing PostgreSQL rows
needs to be rewritten, backfilled, or reinterpreted when the mode changes; `ingest_signal()` is
called by both a `DB_FIRST` transaction and a `NATS_FIRST` projector, producing the identical
relational shape either way.

## Stages

**Stage 0 - today.** `SIGNAL_DATA_PLANE_MODE=DB_FIRST` (the default; `dataplane/mode.py`).
Unchanged production behavior. `dataplane/` exists but nothing calls into it.

**Stage 1 - shadow evaluation (this branch's scope).** `NATS_FIRST` is exercised only in
isolated tests and the local benchmark (`dataplane/benchmark.py`), never against a real
orchestrator or real signals. This is where this branch stops. `TESTS_RUN`/benchmark results
inform whether Stage 2 is worth pursuing at all.

**Stage 2 - shadow-parallel in a non-production environment (future, not built here).** A
`SIGNAL_DATA_PLANE_MODE=NATS_FIRST` orchestrator instance runs in a non-production environment
against real NATS/PostgreSQL, with its own isolated `strategy_instance_id`/environment, so its
signals are never confused with `DB_FIRST` production signals. This validates the `NATS_FIRST`
path against real infrastructure (real PUBACK latency, real projector catch-up behavior) without
touching the `DB_FIRST` production path at all. No production traffic runs through `NATS_FIRST`
at this stage.

**Stage 3 - explicit per-deployment cutover, not per-signal.** When a specific deployment
(a specific orchestrator instance / environment) is ready, its `SIGNAL_DATA_PLANE_MODE` is
changed from `DB_FIRST` to `NATS_FIRST` as an explicit configuration change - the same class of
change as `SignalAuthorityMode`'s existing `LEGACY_FILE` -> `DB_SHADOW` -> `DB_PRIMARY`
progression, which this design deliberately mirrors in spirit (fail-closed, explicit
double-confirmation via `SIGNAL_DATA_PLANE_NATS_FIRST_ENABLED=true`, no silent fallback). Before
the flip: `SignalDataPlaneFlags.validate(nats_available=True)` requires the operator to have
independently confirmed NATS availability - this module will not let an operator select
`NATS_FIRST` "hopefully."

Downstream consumers (distribution, Trade Manager intake, future V2 execution triggers) are
themselves written to consume from JetStream, not to branch on the mode flag - so a consumer
that's already been pointed at `realtime.signal.entry.accepted.v1` needs no change at cutover;
what changes is *which upstream path publishes to that subject*. This bounds the blast radius of
the Stage 3 flip to the orchestrator/publisher boundary.

**Stage 4 - retire `DB_FIRST` for that deployment (future).** Once a deployment has run
`NATS_FIRST` successfully for a defined soak period, the `OutboxRelay`/transactional-outbox code
path for that deployment becomes dead code and can eventually be removed - but only after Stage
3 has been validated in production for that deployment specifically. This document does not
set a timeline for Stage 4; it is explicitly out of scope for this branch to decide.

## Why this avoids dual authority

- At any instant, exactly one mode is the producer for a given deployment's signals - determined
  by that deployment's own `SIGNAL_DATA_PLANE_MODE`, read once at startup
  (`SignalDataPlaneFlags.from_env()`), not re-evaluated per signal and not mixed within one
  deployment.
- `ingest_signal()`'s own idempotency (deterministic `signal_id`, `entry_signal_hash` conflict
  detection) means that *even if* a bug caused both paths to attempt to write the same
  `signal_id` (e.g. during a Stage 2 misconfiguration), the second writer would either no-op
  (identical content) or raise `CanonicalSignalIdentityConflict`/`SignalProjectionConflict` - it
  would never silently create two different logical records for one signal_id. That is a safety
  backstop, not the primary mechanism; the primary mechanism is that only one mode is configured
  as the producer per deployment.
- A `NATS_FIRST` deployment reading `DB_FIRST`-produced history (e.g. a dashboard querying
  PostgreSQL across a migration boundary) sees no discontinuity: the relational schema and
  the meaning of its rows are identical in both modes (`ENTRY_SIGNAL_RELATIONAL_SCHEMA_REUSED`).

## What this branch does NOT do

Configure, flip, or exercise the mode flag against any real deployment, staging, or production
configuration. `SIGNAL_DATA_PLANE_MODE` is not set anywhere outside test code in this branch.
