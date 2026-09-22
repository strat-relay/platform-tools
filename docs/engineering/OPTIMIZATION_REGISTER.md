# Optimization register

The canonical register for deliberately deferred optimization, hardening, and technical debt
across StratRelay. Created by architecture/p4-2-runtime; entries predate that branch where noted.

**Rule:** a `DEFERRED` item does not block shipping unless new evidence shows that it affects
correctness, data integrity, security, or safe broker execution. Shipping past a `DEFERRED` item
is not a decision to abandon it - it is a decision that it can wait.

States: `DEFERRED` (acknowledged, not scheduled) · `NEXT` (intended for the next relevant piece
of work) · `BLOCKING` (must be resolved before a named next step) · `DONE` (resolved; kept for
history, not deleted).

| # | Item | State | Notes |
|---|---|---|---|
| 1 | DB-first signal hot path -> frozen NATS-first prototype later | DEFERRED | Prototype exists (`architecture/nats-first-data-plane`), not integrated. Re-evaluate once DB-first production has run long enough to have real latency data. |
| 2 | Real signal T0-T6 latency measurement/tuning | DEFERRED | The NATS-first prototype's benchmark is a local/fake-substrate proxy only, not production numbers (see that branch's own `04_BENCHMARK_RESULTS.md`). |
| 3 | PostgreSQL backup/restore and node-loss resilience | DEFERRED | Not addressed by any P2/P4 branch to date. |
| 4 | JetStream durability/capacity/retention tuning | DEFERRED | `TRADING_OBSERVATION`'s 7-day retention (#29) is one instance of this broader item. |
| 5 | OD-06 broker-held authenticated expiring fencing | BLOCKING | Blocks any V2/execution work, not P4.2 shadow shipping. Explicitly out of scope for every P4 branch to date (`docs/p4_2_managed_trade/05_V2_EXECUTION_COMPATIBILITY.md`, nats-first prototype). |
| 6 | Execution NATS-first path | DEFERRED | Depends on #1 and #5. |
| 7 | Bridge execution idempotency | DEFERRED | Pre-existing, documented in `docs/runtime_boundaries` (B1). |
| 8 | Execution reconciliation/recovery | DEFERRED | Not addressed by P4.2. |
| 9 | Canonical `/safety` migration | DEFERRED | Pre-existing platform item, unrelated to P4.2's own scope. |
| 10 | Canonical `/strategies` migration | DEFERRED | Pre-existing platform item. |
| 11 | `/events` and `/audit` taxonomy/migration | DEFERRED | Pre-existing platform item. |
| 12 | Platform API / Bridge API ownership split completion | DEFERRED | Concurrent Codex work (`platform_api/`); tracked here for visibility, not owned by this branch. |
| 13 | Legacy Control API retirement | DEFERRED | Pre-existing platform item. |
| 14 | `signals.jsonl` retirement | DEFERRED | P2-A1/P4.2 already do not depend on it for any live path; full retirement (deleting the file/tailer) is separate. |
| 15 | Compact strategy-state filesystem seam retirement | DEFERRED | Pre-existing platform item. |
| 16 | Runtime-PVC code overlay removal | DEFERRED | Pre-existing platform item. |
| 17 | Orchestrator-shadow workload rename | DEFERRED | Pre-existing platform item. |
| 18 | Namespace capacity planning | NEXT | This branch adds the minimum quota headroom for one P4 pod (`deploy/trade_management/resource-quota.yaml`: pods 10->11, limits.cpu 3600m->3750m, limits.memory 4Gi->4160Mi). A real capacity plan across all current and near-term pods (P4, Platform API, P2 shadow, a future execution consumer) has not been done. |
| 19 | DB/NATS credential rotation | BLOCKING (for P4.2 activation specifically) | See "Credentials" below - the activation agent must either rotate before P4.2 activation or receive explicit authorization to defer it (mission section 9). Not rotated by this branch. |
| 20 | Full P4 eligibility taxonomy (`GAP_RECOVERY`, `PRE_ORCHESTRATOR_REFERENCE`) | DEFERRED | `trade_management/managed_trade.py::compute_eligibility` only implements `ELIGIBILITY_UNEVALUATED`/`ELIGIBLE`/`FORWARD_INELIGIBLE(LATE_CREATION)`. No visibility into the upstream replay-guard classification signal within P4.2's scope. |
| 21 | ManagedTrade state history | DEFERRED | No `managed_trade_state_history` table; P4.2 only ever creates `state='OPEN'` trades (no transition exercised yet). |
| 22 | Anti-join reconciliation (EntrySignals without a ManagedTrade) | DEFERRED | No batch reconciliation report exists; correctness today rests on deterministic ids + unique constraints + quarantine, not a periodic audit. |
| 23 | Challenger TM tracks | DEFERRED | No `managed_trade_evaluation_track` table; only the BOUND series exists. |
| 24 | Runtime TM code-manifest verification | DEFERRED | `TmVersionManifest.code_manifest` carries a placeholder (`"computed-at-registration"`) for `tm_none.py`; the evaluator does not verify its own code hash against the bound manifest at startup (A6 07 section 4 "fail closed if code cannot be reproduced" is not implemented). |
| 25 | P4 real-Postgres tests into normal CI | DEFERRED | `TradeManagementRealPostgresTests` (in `test_postgres_integration.py`) run today via the same `python -m unittest test_postgres_integration` invocation the pre-existing tests use, but `compose.yaml`'s `postgres-tests` service command was not changed to add a distinct target, and no CI pipeline was wired to run it automatically. |
| 26 | ManagementSignal distribution | DEFERRED | No `ManagementSignal`, no `signal.management.published.v1`, no distribution/Console consumer. TM-NONE's publication gate is always `WITHHELD` by construction (it never produces an actionable decision), so this has no correctness impact yet. |
| 27 | ManagedTrade/TM Platform API + Console views | DEFERRED | Explicitly out of scope for architecture/p4-2-runtime (mission section 11). |
| 28 | Frozen NATS-first branch integration/evaluation | DEFERRED | `architecture/nats-first-data-plane` remains an unintegrated prototype/comparison; see #1. |
| 29 | `TRADING_OBSERVATION` retention review | NEXT | 7 days is an explicit SHIP-FIRST value for the first production deployment (mission section "RETENTION"), not a final decision. Revisit once real observation volume/replay needs are known. |
| 30 | P4 observation interval tuning | NEXT | `P4_OBSERVATION_INTERVAL_SECONDS` defaults to 30s (`trade_management/runtime/config.py`), configuration-driven and chosen as a reasonable ship-first starting point, not measured against real load. |
| 31 | Post-activation ManagedTrade reconciliation/backfill | DEFERRED | The 9 pre-boundary canonical EntrySignals are deliberately never replayed into ManagedTrade by this branch (mission section "NO BACKFILL"). A reviewed, explicit backfill mechanism for them (or a decision not to backfill) is separate future work. |
| 32 | Single shared PostgreSQL/NATS connection per P4 runtime process | DEFERRED | `trade_management/runtime/service.py` uses one Postgres connection and one NATS connection for the whole process (matching `scripts/p2_shadow/worker.py`'s and `scripts/signal_outbox_relay.py`'s existing convention), not a connection pool. Fine at this vertical slice's expected volume; revisit if throughput requires it. |

## Credentials (mission section 9)

**Not rotated by this branch.** `architecture/p4-2-runtime` is implementation/preparation only
and never connects to any live credential. The P4 runtime workload
(`deploy/trade_management/workload.yaml`) references the existing `trading-runtime-endpoints`
Kubernetes Secret via `envFrom` - the same secret `signal-outbox-relay` and
`platform-signals-api` already use - and embeds no credential of any kind in any manifest or
code path (verified: `git grep` for secret-shaped strings across this branch's changes finds
none; `RuntimeConfig.from_env()` reads only environment variable names, never a literal value).

Per mission section 9, before P4.2 is actually activated (a separate, later, explicitly
authorized step - not performed by this branch), the activation agent must either:

- **A.** Rotate the credentials in `trading-runtime-endpoints` before activation, or
- **B.** Receive explicit authorization from whoever owns that decision to defer rotation.

This register entry (#19) exists so that decision is never made by default/omission.
