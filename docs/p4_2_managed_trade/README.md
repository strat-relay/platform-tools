# P4.2: ManagedTrade, Trade Observation Service, TM-NONE

Branch: `architecture/p4-2-managed-trade` · Worktree: `trading-platform-p4-2` ·
Base commit: `d3edcbb519c66ac3643d87d4cdf1e4936663d793`

Implementation-only. Nothing here is activated in production. `SIGNAL_AUTHORITY_MODE`,
`EXECUTION_AUTHORITY_MODE`, `ORCHESTRATOR_MODE` are all unaffected. `BROKER_WRITES=0`.

This document describes the P4.2 implementation as originally authored. For its integration
onto the current production lineage (alongside Codex's Platform Signal API work), the real-
PostgreSQL proof, the two bugs that proof caught and fixed, and the `TM-NONE-1` production seed
(migration `014`), see [`02_INTEGRATION.md`](02_INTEGRATION.md).

## What this is

```
canonical EntrySignal (P2-A1, strategy.entry_signals, signal.entry.created.v1)
        -> ManagedTrade (broker-independent reference trade)
        -> Trade Observation Service (MarketDataProvider only)
        -> TM-NONE (every eligible observation -> audited HOLD)
        -> Publication gate boundary (WITHHELD; no customer distribution built)
```

Authoritative design documents (read, not reproduced here): A6
`docs/management_architecture/{02,04,06,07,08,11,12}*.md` and A7
`docs/p2_p4_reconciliation/{03,04,07,09,10}*.md` (in the `claude-a6`/`claude-a7` worktrees).
This implementation follows their identity schemes, event contracts, and idempotency rules
directly. Where this implementation's scope is narrower than the full A6/A7 design, that is
called out explicitly below - it is a deliberate scoping decision for this mission, not a
disagreement with the design.

## New code

| Module | Purpose |
|---|---|
| `trade_management/ids.py` | `stable_id` + deterministic derivations: `managed_trade_id`, `market_snapshot_id`, `observation_id`, `decision_id`, `tm_version_id`, `binding_id`. |
| `trade_management/versions.py` | `TmVersionManifest` (canonicalised, hashed identity) and the frozen `TM-NONE-1` manifest with a golden hash vector. |
| `trade_management/market_data.py` | The read-only `MarketDataProvider` Protocol (`MarketQuote`, `BarWindow`) and `FakeMarketDataProvider` for tests. No adapter to a real feed is built here. |
| `trade_management/binding.py` | `StreamBindingResolver` protocol, `LegacyStaticResolver`, `DefaultTmNoneResolver` (fail-closed if TM-NONE-1 isn't registered), `ChainedResolver`. |
| `trade_management/managed_trade.py` | `create_managed_trade`: the single creation transaction (inbox claim -> load EntrySignal -> verify hash -> eligibility -> resolve binding once -> insert -> outbox `trade.opened.v1`). |
| `trade_management/open_consumer.py` | `ManagedTradeOpenConsumer`: durable-consumer shape for `signal.entry.created.v1`. Not activated. |
| `trade_management/observation.py` | `record_observation` + `TradeObservationService`: gapless per-trade sequencing under a row lock, content-addressed dedup, outbox `trade.observation.recorded.v1`. |
| `trade_management/tm_none.py` | `TmNoneEvaluator` + `record_decision`: persists an immutable `HOLD` decision per observation, outbox `trade.decision.made.v1`. |
| `trade_management/publication_gate.py` | `evaluate_publication_gate`: the `TradeManagerDecision != ManagementSignal` boundary. Always `WITHHELD` for `HOLD`; no `ManagementSignal`/distribution built. |
| `trade_management/fakes.py` | In-process `FakeConnection`/`FakeCursor` test substrate (no live PostgreSQL reachable in this sandbox). |
| `postgres/migrations/013_trade_management_foundation.sql` | The relational schema (schema `trade_management`, additive; leaves migration 003's legacy `observations`/`decisions`/`checkpoints` stub tables in place, unused). `DATABASE_SCHEMA_VERSION` bumped to `"013"`. |

### Shared files touched (both additive/mechanical)

- `infrastructure/messaging/contracts.py`: added subjects `trade.opened.v1`,
  `trade.observation.recorded.v1`, `trade.decision.made.v1`; explicitly extended
  `TRADING_CORE`'s subject list with the first and third (never a `trade.>` wildcard); added
  the new `TRADING_OBSERVATION` stream for the second. `main` has not touched this file since
  the base commit.
- `tests/test_postgres_nats_foundation.py`: updated two assertions that hard-coded the stream
  set (`{"TRADING_CORE", "EXECUTION"}`) to include the new `TRADING_OBSERVATION` stream - this
  was a genuine regression caught by running the pre-existing suite, not a design choice.

## Reused unmodified

`strategy.entry_signals` (P2-A1, read-only from this package), `postgres.db.transaction`,
`postgres.foundation.claim_inbox`/`mark_inbox_processed`, `platform.outbox_events`/
`platform.inbox_events`, `core.strategies.evaluation.canonical_bytes`/`canonical_hash`,
`infrastructure.messaging.contracts.EventEnvelope`/`JetStreamPublisher` (which already sets
`Nats-Msg-Id`). No line of P2's canonical EntrySignal contract was changed.

## Scope reductions vs the full A6/A7 design (explicit, not accidental)

This mission's own test list (section 10) is satisfied in full. The following A6/A7 items were
deliberately **not** built, to keep this branch bounded and reviewable; each is a natural
follow-up, not a blocker to reviewing what's here:

- **No `managed_trade_evaluation_track` (CHALLENGER tracks).** Only the BOUND series exists.
  Shadow/challenger TM-version comparison (A6 06 section 3) is future work.
- **No `managed_trade_state_history` audit table.** P4.2 only ever creates `state='OPEN'`
  trades (both adapters emit EntrySignal only after a reference fill, per A6 06 section 4) - no
  transition is exercised yet, so no history table was added ahead of need. `managed_trade.state`
  and the `PENDING_ENTRY`/`CANCELLED`/`CLOSED` values exist in the schema for when it is.
- **No anti-join reconciliation report** (`trade_management/reconcile.py` in A7's plan). The
  correctness backstop that exists today is: deterministic ids, the `entry_signal_id` unique
  constraint, and the quarantine table - not a batch "signals without a ManagedTrade" report.
- **Simplified eligibility.** `compute_eligibility` classifies only `ELIGIBILITY_UNEVALUATED`
  (default, no threshold configured) vs `ELIGIBLE`/`FORWARD_INELIGIBLE(LATE_CREATION)` from a
  configurable creation-lag threshold. The full replay-guard taxonomy (`GAP_RECOVERY`,
  `PRE_ORCHESTRATOR_REFERENCE`) is not wired in - this implementation has no visibility into
  that upstream classification signal within this mission's scope.
- **No `STREAM` binding-resolution type.** Only `LEGACY_STATIC` and `DEFAULT_TM_NONE` are
  implemented (`binding.py`); a full `StreamBinding` registry (subject/consumer routing by
  stream identity) does not exist yet upstream.
- **TM-NONE's staleness/gap handling is simplified.** `TmNoneEvaluator` distinguishes only
  `NO_MANAGEMENT_POLICY` vs `TRADE_CLOSED`; `STALE_MARKET_DATA`/`DATA_UNAVAILABLE`/
  `LATE_OBSERVATION` classification (A6 08 section 4) is not implemented - every observation
  the service actually records is assumed timely. `TmVersionManifest.observation_spec.max_market_age_ms`
  exists in the manifest as a placeholder for when this is added; it is not yet enforced.
- **No production seed migration.** `TM-NONE-1`, `TM-LEGACY-0`, and per-strategy
  `legacy_stream_binding` rows are seeded by tests only (`FakeConnection.seed_tm_version`/
  `seed_legacy_binding`); a reviewed data migration to register `TM-NONE-1` in a real database
  is a separate, later step (this branch performs no live database writes at all).
- **`managed_trade_skip`** exists in the schema (for a future "signal classified ineligible,
  never even attempted" case) but nothing currently writes to it - every EntrySignal consumed by
  this implementation either creates a trade, is absorbed as a duplicate, or is quarantined.

## Failure semantics implemented (A7 10 table)

| Situation | Behaviour here |
|---|---|
| EntrySignal record missing | `EntrySignalRecordMissing` raised inside the transaction; inbox claim rolls back too - safe to retry once the record exists. |
| Binding table empty/no row | `LegacyStaticResolver` returns `None`; `ChainedResolver` falls through to `DefaultTmNoneResolver`. |
| `TM-NONE-1` not registered | `TmVersionUnavailable` raised; whole transaction (including the inbox claim) rolls back - fail closed, retryable. |
| PostgreSQL unavailable | Not directly testable without a live PostgreSQL in this sandbox; the transaction boundary (`postgres.db.transaction`) means no partial commit is possible either way. |
| Hash mismatch on duplicate signal_id | Quarantined (`managed_trade_quarantine`), original row never touched. |
| Duplicate observation delivery | Content-addressed `observation_id` dedup; no new sequence number consumed. |
| Duplicate decision delivery | Both inbox-level (`trade-manager-shadow` consumer) and domain-level (`decision_id` uniqueness) absorb it. |

## Ordering

Per-trade: `managed_trade_id` + `observation_seq`, gapless by construction (the producer assigns
the next sequence under a row lock on `managed_trade`, inside its own transaction - A6 08
section 5). Across different trades: no ordering is imposed or relied upon - each is an
independent transaction. No global ordering key was introduced.

## Tests

49 new tests across 4 files (`tests/test_trade_management_*.py`): manifest hash determinism and
golden vectors; deterministic id derivations; ManagedTrade creation, duplicate/redelivery,
hash-mismatch quarantine, frozen strategy/version/binding identity, fail-closed TM-NONE-1
unavailability, binding-immutability-in-practice; the open consumer's duplicate handling;
observation recording, gapless per-trade sequencing, cross-trade independence, content-addressed
dedup, restart/redelivery safety, correct subject/stream; TM-NONE consuming an observation and
producing `HOLD`, decision durability/auditability, duplicate-decision absorption, the
publication gate always withholding `HOLD`; a static isolation audit (AST-based forbidden-import
check, no `open()` calls, no broker-tool vocabulary, no `signals.jsonl`/Phase 7/runtime-directory
reference outside explanatory docstrings) and a DDL property audit (additive-only, no forbidden
column names, the only cross-schema FK is `strategy.entry_signals`, the documented immutability
triggers exist).

Full regression run: this branch's 219 tests (170 pre-existing + 49 new) produce
`failures=3, errors=5, skipped=1` - verified byte-identical, via a detached worktree at the base
commit, to the 170 pre-existing tests' own `failures=3, errors=5, skipped=1` baseline. Those 8
failures/errors pre-exist on `main` (`test_control_api.py`, `test_platform_boundary.py` -
environment/live-service-dependent) and are unrelated to this branch. Zero regressions
introduced.

`FROZEN_STRATEGY_FILES_CHANGED=false`: `git diff --stat d3edcbb -- core/strategies strategies
strategy_report_format.py signal_orchestrator.py trade_manager/` is empty on this branch.

## DDL immutability triggers: what is and isn't verified here

The migration defines real PostgreSQL triggers (`trade_manager_version_immutable`,
`managed_trade_binding_immutable`, `trade_manager_decision_immutable`,
`legacy_stream_binding_append_only`). No live PostgreSQL is reachable in this sandbox (verified:
no `psycopg` installed, no reachable `localhost:5432`), so these triggers are verified by (a) a
static test that the `CREATE TRIGGER`/function definitions exist in the migration file with the
right names, and (b) `FakeCursor`'s own `UPDATE` dispatch only implements the specific mutable
columns this package's code actually issues (`last_observation_seq`, inbox `status`) - any
attempt by this package's own code to update an immutable column would raise
`AssertionError: FakeCursor cannot handle statement`, which is a real (if indirect) guard against
this package's *own* code ever attempting such a mutation. The trigger's enforcement against an
arbitrary SQL client (e.g. a future ad hoc operator UPDATE) can only be verified against a real
PostgreSQL instance - a natural first check for Stage 2 (below).

## Integration / merge-conflict report

As of this branch's base commit (`d3edcbb519c66ac3643d87d4cdf1e4936663d793`), `main` has not
advanced (`git log d3edcbb..main` is empty), so there is no actual commit history to diff against
yet - this section is a file-level risk assessment for when Codex's concurrent Platform API /
canonical Console signal path work lands, not a diff.

**Files this branch touches:**
- New: `trade_management/` (10 files), `postgres/migrations/013_trade_management_foundation.sql`,
  `tests/test_trade_management_*.py` (4 files), `docs/p4_2_managed_trade/README.md`.
- Modified (additive/mechanical): `infrastructure/messaging/contracts.py`,
  `postgres/foundation.py` (`DATABASE_SCHEMA_VERSION` only), `tests/test_postgres_nats_foundation.py`.

**Realistic collision points**, in descending likelihood:

1. **`infrastructure/messaging/contracts.py`.** If Codex's Platform API/Console work also
   registers new subjects (plausible for a Console-facing signal delivery path), the textual
   collision would land in the same `SUBJECTS` frozenset/`STREAMS` dict literals this branch
   edits. Both changes would be additive entries; resolution is keeping both sets, not
   reconciling contradictory logic - this branch's addition is kept as its own clearly-commented
   block for exactly this reason.
2. **`postgres/foundation.py`'s `DATABASE_SCHEMA_VERSION` and migration numbering.** This branch
   claims migration number `013` and bumps the version string to `"013"`. If Codex's work also
   adds a migration, whichever branch merges second will need to renumber its migration file
   (a mechanical rename, not a logical conflict, since `009`-style additive migrations in this
   repo have no ordering dependency on each other beyond file-name sort order) and reconcile the
   version string.
3. **`control_api/app.py`.** Not touched by this branch at all. If Codex's Console work adds new
   routes there, there is no collision - but if a *future* integration step wants to expose
   ManagedTrade/observation/decision data through the Console (out of scope for this branch),
   that would be new code added there later, not a conflict from this branch's own changes.
4. **`tests/test_postgres_nats_foundation.py`.** Low risk: this branch's edit is narrowly scoped
   to the stream-count assertions; a concurrent edit to the same test file is unlikely unless
   Codex's work also touches `STREAMS`.

**No collision expected** in `trade_management/` itself (a wholly new path), the new migration
file's *content* (only its number is a soft collision risk), or any of P2's existing files/tables
(this branch never writes to `strategy.entry_signals` or any other P2-owned relation).
