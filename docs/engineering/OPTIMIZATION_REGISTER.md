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
| 5 | OD-06 broker-held authenticated expiring fencing | IMPLEMENTED, NOT DEPLOYED | `execution_v2/fence.py` (platform-side signing) + `mt5_bridge_fence/` (real, durable, independently-verifying bridge-side validation) implement and isolated-proof the full design (`tests/test_mt5_bridge_fence.py`, `tests/test_execution_v2_real_bridge.py`) as of `architecture/v2-execution-audit-remediation`. Still blocks any actual broker write until `mt5_bridge_fence/` is deployed to the real `mt5-native-bridge` process (see item #35 and `docs/v2_execution/README.md` §7). |
| 6 | Execution NATS-first path | DEFERRED | Depends on #1; #5 is no longer a full blocker (see above), but this remains unperformed regardless. |
| 7 | Bridge execution idempotency | IMPLEMENTED, NOT DEPLOYED | `mt5_bridge_fence/store.py`'s durable SQLite idempotency ledger (survives a real process restart) addresses the pre-existing gap documented in `docs/runtime_boundaries` (B1) for the V2 execution path specifically - not yet deployed to the real bridge; see item #35. The original bridge process's own lack of idempotency-key de-duplication (B1) is otherwise unchanged. |
| 8 | Execution reconciliation/recovery | IMPLEMENTED, NOT DEPLOYED | `execution_v2/reconcile.py` + `mt5_bridge_fence`'s durable ledger provide the minimum reconciliation this mission's scope requires (`STILL_UNKNOWN`/`CONFIRMED_EXECUTED`/`CONFIRMED_NOT_EXECUTED`), proven in `tests/test_execution_v2_real_bridge.py::test_response_loss_one_effect_reconciled_safely`. Not a general reconciliation platform; not yet deployed. |
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
| 33 | Public Context ENTRY_ONLY outcome filesystem authority | DONE | Canonical outcomes are projected from the frozen runner's post-checkpoint state into `strategy.entry_signal_outcomes`; the Platform API signal and Context report routes read PostgreSQL. The initial cutover is limited to the verified 11 post-T0 EntrySignals in the configured cutoff. This does not retire the runner's internal compact-state file. |
| 34 | PID-1 stale-lock handling across Context container restarts | DEFERRED | A prior container-start log showed the runner refusing a stale PID-1 lock. The current runner is healthy and has no lock file; preserve this as a separate runtime lifecycle hardening item. Graceful rollout must allow the runner's SIGTERM handler to checkpoint and release its lock. |
| 35 | Real MT5 bridge-side deployment of `mt5_bridge_fence/` | BLOCKING (for v2-execution activation specifically) | `mt5_bridge_fence/` is real, durable, independently-verifying production code, proven end-to-end over a real HTTP transport (`tests/test_execution_v2_real_bridge.py`) - but has never been ported to or deployed inside the actual `mt5-native-bridge` process/repository. No broker order can safely be sent until that deployment exists. See `docs/v2_execution/README.md` §7. |
| 36 | Multi-attempt retry after a proven-safe terminal attempt state | DEFERRED | `execution_v2.execution_attempt.execution_intent_id` is `UNIQUE` (one attempt per intent for this slice); OD-06 invariant I5's "new attempt requires proof every earlier attempt is NOT_SENT/FENCED/CANCELLED" is not implemented. |
| 37 | Multi-account execution routing | DEFERRED | `execution_v2.execution_intent.entry_signal_id` is globally `UNIQUE` (not per-account) - correct for a single personal-execution account; explicitly out of scope for this branch. |
| 38 | Asymmetric fence signatures | DEFERRED | `execution_v2/fence.py` and `mt5_bridge_fence/` both use HMAC-SHA256 shared-secret signing (V1), matching the OD-06 ADR's own stated hardening path. |
| 39 | Fence expiry clock-skew grace window | DEFERRED | `advance_fence`/`submit` use a strict `now > expiry` comparison with no grace window, relying on both processes sharing NTP-synchronized infrastructure clocks once co-deployed. Revisit once the real bridge's actual clock-sync guarantees are known (see #35). |
| 40 | Bridge HTTP transport-layer authentication | DEFERRED | `mt5_bridge_fence/http_server.py` has no mTLS/API-key/network-level authentication of its own - the payload-level HMAC signature is the authorization decision, but the HTTP connection itself is unauthenticated beyond that. Binding the bridge to a private network segment (and/or adding mTLS) is separate hardening work once the real bridge service (#35) is actually deployed and reachable from somewhere other than localhost. |
| 41 | NATS-first execution optimization | DEFERRED | The existing outbox-relay path is reused unmodified for `execution.intent.created.v1`/`execution.result.recorded.v1`; a lower-latency direct-JetStream path is compatible future work. |
| 42 | Control API canonical `/executions` wiring | DEFERRED | `control_api/execution_v2_source.py` is prepared and unit-tested but not called from `control_api/app.py`'s live `/executions` route (do not deploy this API change); see that module's own docstring for the exact integration point. |
| 43 | execution_v2 resource-quota headroom | BLOCKING (before scaling `deploy/execution_v2/workload.yaml` above `replicas: 0`) | No mission to date has performed a live-cluster read (DO NOT MODIFY LIVE KUBERNETES); see `deploy/execution_v2/resource-quota-patch.README.md` for the exact arithmetic an operator must apply against the then-current live `ResourceQuota` before scaling up. |
| 44 | `execution-v2-runtime` NetworkPolicy egress rule to the deployed bridge service | BLOCKING (alongside #35) | `deploy/execution_v2/network-policy.yaml` deliberately grants no egress to any bridge fence endpoint today, since none is deployed. Adding the rule is required alongside, and only alongside, deploying #35 - never added speculatively ahead of it. |
| 45 | V2 per-account risk policy overrides | DEFERRED | See "V2 per-account risk policy overrides" below. Audited (`CLAUDE-V2-RISK-CONFIG-SCOPE-AUDIT`) at platform `7ecabba` / console `f74a245`: the DB-authority cutover implements a single canonical GLOBAL policy row only - no account-scoped table, resolver, or Console scope selector exists. Not a small completion of already-present support; correctly out of scope for the DB-authority cutover itself. |
| 46 | V2 execution hot-path latency | DEFERRED | **PRIORITY: HIGHEST.** The current pre-broker path performs multiple synchronous PostgreSQL transactions and synchronous bridge reads. Observed bridge latency: account median ~5.4s, positions ~4.0s, quote ~4.0s, maximum ~27.9s. Future work may evaluate NATS-first validation, safe asynchronous audit/projection, and continuously refreshed broker snapshots only after execution correctness is proven. |

## V2 per-account risk policy overrides

**STATUS:** DEFERRED

**CURRENT:** V2 risk configuration uses the canonical global PostgreSQL policy
(`execution_v2.risk_policy`, singleton row `policy_id='current'`, plus its
`risk_policy_allowed_{account,strategy,symbol}` child tables and `risk_policy_change` history -
`execution_v2/risk_policy_store.py`). There is exactly one policy for the whole platform; nothing
in the schema, resolver, API, or Console is keyed by trading account.

**TARGET:**

```
Global Risk Policy
        v defaults
Trading Account Overrides
        v precedence
Effective Account Policy
        v
V2 Risk Evaluator
```

**NORMAL PRECEDENCE:** explicit account override > global default.

**INHERITANCE:** missing account override -> global value. Absence must be tracked via explicit
presence/nullability (a row exists vs. does not), never via a falsy sentinel - `false`/`0`/`0.0`
are legitimate explicit override values, not "inherit" signals.

**GLOBAL SAFETY:** global disable cannot be bypassed by an account override. Effective activation
must behave as `global_enabled AND account_effective_enabled`, never simple "account wins"
precedence in the unsafe direction.

Future audit should classify every field as one of: `GLOBAL_ONLY`, `ACCOUNT_OVERRIDABLE`,
`ACCOUNT_ONLY`, `DERIVED`, `HARD_SAFETY_CEILING`.

Expected account-overridable candidates (confirmed present as global-only fields today in
`execution_v2.risk_policy` / `RiskPolicy`, per `execution_v2/risk.py`'s `parse_policy_dict`):
`risk_per_trade`, `max_volume`, `max_daily_loss`, `max_signal_age_seconds`,
`max_concurrent_positions`, `max_concurrent_orders`, `allowed_strategies`,
`allowed_symbols`/instruments. Exact field semantics (which of these are ordinary defaults vs.
absolute platform safety ceilings that an account override must never be able to weaken) must be
confirmed during implementation, not assumed.

**REQUIRED FUTURE BEHAVIOR:**

- Account overrides store only explicit differences - no full policy duplication per account.
- Removing an override restores global inheritance.
- Global changes propagate immediately to inheriting accounts; explicit account overrides remain
  unchanged.
- Global kill switch always wins (see GLOBAL SAFETY above).
- Global hard restrictions cannot be widened by an account override - likely intersection
  semantics for `allowed_strategies`/`allowed_symbols` (`effective = GLOBAL allowed INTERSECT
  ACCOUNT allowed override`, when an account override is present), not simple replacement.
- Limits (`max_daily_loss`, `max_concurrent_positions`, `max_concurrent_orders`) must be measured
  and enforced at the same account scope as they are configured - a per-account limit whose
  runtime counter is accidentally computed globally across every account would be a real
  correctness bug, not a cosmetic one.
- Canonical internal trading-account identity used as the relational key, never a display name,
  masked account number, or broker symbol.
- One backend effective-policy resolver (`resolve_effective_policy(account_id)`); the Console,
  Platform API, and evaluator all consume its output rather than each re-implementing merge logic.
- Optimistic concurrency (global and account-override each carry their own revision; the effective
  fingerprint incorporates both).
- Effective-policy provenance exposed at minimum as: global policy revision, account override
  revision, effective policy fingerprint, and field-level source where useful for the
  configuration UI.
- Fail closed: missing/invalid global policy, DB unavailable, or an unknown account -> fail
  closed entirely; a malformed account override -> fail closed for that account only, never a
  silent fallback to global for that account, and never any fallback to the legacy JSON file.

**CONSOLE TARGET:** a scope selector (`Global Defaults` / `Trading Account: <masked account>`).
The account view shows, per field: Global Default, Account Override, Effective Value, and a
`Use Global` / `Override` toggle - never requiring the operator to manually copy global values
into every account.

**DEPENDENCIES:** canonical PostgreSQL risk-policy authority must exist first (it now does, as of
platform `7ecabba`).

**NON-BLOCKING FOR CURRENT DB AUTHORITY CUTOVER:** YES.

**Schema note:** today's `execution_v2.risk_policy` singleton-row design does not itself block
adding an `execution_v2.account_policy_override` table later (a straightforward additive table,
foreign-keyed to canonical account identity, following the same relational-child-table pattern
`risk_policy_allowed_account` already uses) - but that table is deliberately not created now.

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

## Fence signing key (mission section 4, `architecture/v2-execution-audit-remediation`)

`V2_FENCE_SIGNING_KEY` is a **new** credential, not a rotation of an existing one - see
`docs/v2_execution/README.md` §3a "Fence key / authentication design" for the full
algorithm/rotation/failure-behavior specification. It has never been set to any value by any
V2-execution mission - `FenceAuthority.from_env()` and `RuntimeConfig.from_env()` both fail
closed without it, and no default/example key exists anywhere in the codebase
(`tests/test_execution_v2_isolation.py::test_fence_authority_never_hardcodes_a_signing_key`).
Before `deploy/execution_v2/workload.yaml` is scaled above `replicas: 0`, an operator must
provision a real key via a dedicated `execution-v2-fence-signing-key` Kubernetes Secret and, once
the real bridge (item #35) is deployed, provision the *same* key material to it out-of-band -
this key is deliberately never transmitted over the platform's own request path in the real
design (`docs/runtime_boundaries/04_RECOMMENDED_FENCING_DESIGN.md`).
