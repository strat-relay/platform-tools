# Runtime boundaries and fencing (A5) - OD-05, OD-06, and A4 OD-01..OD-04

Agent CLAUDE-A5-RUNTIME-BOUNDARIES-AND-FENCING. Baseline: trading-platform `88528e685539fa68b2aed6579354e2d75b57baf6` (Codex V1.2.1) in an isolated worktree (`claude-a5/runtime-boundaries`). Inputs: A4 `89ba8b2`, Codex V1.2 `39d6216`, A3 `26d4a40`, A1 `eda827c`. The bridge was read from repository `mt5-native-bridge` **commit `5d4b018` via git objects** (its working tree holds live runtime files and was not read). Codex's uncommitted V1.3 work, production runtime, brokers, MT5, PostgreSQL, NATS and Kubernetes were not touched. **Documentation and read-only tooling only; nothing is approved and nothing is implemented.**

> PostgreSQL tells us what is true. JetStream tells services what happened. The trading platform decides what should happen. The MT5 bridge tells MetaTrader what to do and reports what MT5 says.

## Documents

| # | Document | Deliverable |
|---|---|---|
| 00 | this file | summary, decisions, corrections |
| 01 | [`01_EXECUTION_PATH_TRACE.md`](01_EXECUTION_PATH_TRACE.md) | 1 execution-path trace |
| 02 | [`02_FENCING_ALTERNATIVES.md`](02_FENCING_ALTERNATIVES.md) | 2 fencing alternatives |
| 03 | [`03_FENCE_RACE_ANALYSIS.md`](03_FENCE_RACE_ANALYSIS.md) | 3 race analysis |
| 04 | [`04_RECOMMENDED_FENCING_DESIGN.md`](04_RECOMMENDED_FENCING_DESIGN.md) | 4 recommended design |
| 05 | [`05_EXECUTION_ATTEMPT_STATE_MACHINE.md`](05_EXECUTION_ATTEMPT_STATE_MACHINE.md) | 5 attempt state machine |
| 06 | [`06_UNCERTAIN_RECONCILIATION.md`](06_UNCERTAIN_RECONCILIATION.md) | 6 UNCERTAIN reconciliation + broker idempotency model |
| 07 | [`07_WRITE_AUTHORIZATION_MODEL.md`](07_WRITE_AUTHORIZATION_MODEL.md) | 7 write authorization |
| 08 | [`08_OD05_FROZEN_RUNNER_ANALYSIS.md`](08_OD05_FROZEN_RUNNER_ANALYSIS.md) | 8 OD-05 analysis |
| 09 | [`09_OD05_MIGRATION_RECOMMENDATION.md`](09_OD05_MIGRATION_RECOMMENDATION.md) | 9 OD-05 options + recommendation (proposed) |
| 10 | [`10_OD01_OD04_EVIDENCE.md`](10_OD01_OD04_EVIDENCE.md) | 10 OD-01..OD-04 evidence |
| 11 | [`11_P2_P5_DEPENDENCY_ASSESSMENT.md`](11_P2_P5_DEPENDENCY_ASSESSMENT.md) | 11 P2-P5 assessment |
| 12 | [`12_PLATFORM_BRIDGE_BOUNDARY.md`](12_PLATFORM_BRIDGE_BOUNDARY.md) | 12 platform/bridge boundary |
| 13 | [`13_SECURITY_MODEL.md`](13_SECURITY_MODEL.md) | 13 security model |
| 14 | [`14_EXECUTION_FAILURE_MATRIX.md`](14_EXECUTION_FAILURE_MATRIX.md) | 14 failure matrix |
| 15 | [`15_ADR_OD06_BROKER_WRITE_FENCING.md`](15_ADR_OD06_BROKER_WRITE_FENCING.md) | 15 ADR-0003 (**RECOMMENDED, not approved**) |
| 16 | [`16_CODEX_P0_P1_REVIEW_CHECKLIST.md`](16_CODEX_P0_P1_REVIEW_CHECKLIST.md) | 16 Codex V1.3 checklist |
| - | `tools/verify_evidence.py`, `data/evidence_check.json` | 37 re-runnable source checks |

## Headline results

**OD-06 (broker fencing).** The race cannot be closed on the sender's side, nor by PostgreSQL: the last cancellable instant is `RequestLifecycle.dispatch()` in the bridge, and the EA has no cancel protocol. Recommended design **F**: a bridge-held, authenticated, *expiring* fence advanced by the new owner **before its first write**, checked at enqueue **and inside `dispatch()`**, atomic with cancelling queued lower-generation work and enumerating already-dispatched work, plus signed request-bound write authorizations, a bridge write ledger keyed by per-attempt id, and a durable platform attempt machine that commits `SENDING` before the call and keeps `UNCERTAIN` first-class. Race closed at: **the acknowledged fence advance (queued/late requests) and `dispatch()` (the point of no return)**; residual (<= 5 s, accounted for) between PostgreSQL acquire and advance ack.

**OD-05 (frozen runners).** "Frozen" is enforced on **decision code** only (Context: five functions; Liquidity: the strategy module). The Context decision fingerprint is *verified identical* across the initial commit and four later edits to the runner file (`P26`). Persistence is reachable only through seams outside the fingerprint (`P23`, `P24`). Recommended: S0 tailer (time-boxed migration state) then **Option C: a StrategyHost that imports the unmodified runner modules and rebinds the byte-sink seams to PostgreSQL** - source hashes and decision fingerprints preserved, file IPC reaches zero, **no permanent runner exception** in the final `PRODUCTION_FILE_IPC=0`. Proposed, not locked.

**P2-P5.** P2, P3 (shadow), P4 (in part) are **not blocked by OD-06**; P5 is. But A4's ordering needs two corrections: **ownership ledger and broker-state have the execution consumer as their only writer**, so their authority moves with P5, not P3/P4 (`11`).

## New findings that change earlier conclusions

1. **The bridge does not honour the idempotency key** and the EA never receives it (`B1`, `B2`); A4's "bridge honours the idempotency key" is false at baseline. The bridge has **no authentication** and REAL close/trailing are **refused** by it (`B3`, `B4`).
2. **The production path does not use `Mt5ExecutionClient`** (`P1`); management writes use a separate raw path (`P6`).
3. **Uncertainty is recorded as rejection**: any exception during submit, including timeouts and post-send bookkeeping failures, becomes `DRY_RUN_REJECTED` with `broker_write_blocked=true` (`P5`); the bridge's own 5 s waiter timeout arrives as an `isError` text, not as the client timeout branch (`01` 13).
4. **The EA's read tools return no `comment`/`magic`/ids** (`B8`): the send tag cannot be read back, so an `UNCERTAIN` send cannot be reconciled exactly without a bridge ledger (or an EA change).
5. **V1.2.1 lacks write-path fencing**: `acquire_ownership` is compare-and-increment only; no `assert_generation`, `expires_at` unused, `execution.intents.status` unconstrained, two overlapping intent tables (`P13`-`P15`).
6. **OD-01 is live at deployment-definition level**: the stack registry starts the orchestrator with `real-start` against a REAL-mode account, so the `tradeability_decisions` `KeyError` is reached on every routed signal and swallowed into a `SKIPPED` sizing row. REAL execution is **unaffected** (its sizing ignores `sizing_decisions`), so this is a silent **policy bypass**, not an outage (`10`).
7. **The Trade Manager's only upstream is the optional Phase 7 observer** (`P27`), which reads a file the runner declares immutable; with the bridge refusing REAL close/trail the REAL management chain is inert at this baseline on two counts.
8. **Two independent "generation" counters exist** (`real_execution_resume_generation` in the resume file; `live_execution_resume_generation` in orchestrator state, read but never written by source) and neither is attached to any intent or send.

## OD-01..OD-04 in one table

| Id | Status | Evidence |
|---|---|---|
| OD-01 | **`CONFIRMED_LIVE_DEFECT`** (deployment-definition level; loaded-code version `UNKNOWN_RUNTIME_DEPENDENT`) | `10`, `P9`, `P21`, `P22` |
| OD-02 | **`UNKNOWN`** existence, **no source contract** (`ORPHANED` if present) | `P10`, `P12` |
| OD-03 | **optional, not in the intended stack, yet sole TM upstream**; live status `UNKNOWN_RUNTIME_DEPENDENT` | `P19`, `P27` |
| OD-04 | code-supported **YES**; deployment **not in intended definition, actual UNKNOWN** | `10` |

## Open decisions (owner)

| Id | Decision | Recommendation | Blocks |
|---|---|---|---|
| OD-A5-1 | Approve Option C (StrategyHost) as the frozen-runner end state; S0 as a time-boxed migration state | approve | S1 |
| OD-A5-2 | Owner/location of the golden replay corpus | derive from runner histories; versioned fixtures | S1 |
| OD-A5-3 | One StrategyHost per stream instance vs shared | per instance | S1 |
| OD-A5-4 | Halt an account while any attempt is `UNCERTAIN`; replace "retry management failures every loop" with reconcile-then-retry | adopt (execution-policy sign-off) | P5 |
| **OD-A5-5** | **Approve or reject ADR-0003 (fence design F)** | approve | **P5** |
| OD-A5-6 | Bridge scope (M1-M8) and who implements; HMAC vs Ed25519 | M1-M6 + M7; HMAC in V1, Ed25519 path defined | P5 |
| OD-A5-7 | REAL reduce-only capability and break-glass | adopt | P4-execution, P5 |
| OD-A5-8 | Verify the MT5 retcode classification (default-deny) against MetaQuotes documentation | required | P5 |
| OD-A5-9 | Host may add whole-file pins for runners (closes the Liquidity identity gap) | yes | S1 |
| OD-A5-10 | EA read-side enrichment (`comment`, `magic`, ids) | optional hardening after the ledger | - |
| OD-A5-11 | OD-01 disposition: accept as-is, or schedule a separately approved fix (and decide whether tradeability policy should ever gate REAL) | decide; never fix implicitly | P2 reconciliation wording |
| OD-A5-12 | Who implements the platform schema deltas (`assert_generation`, lease liveness, attempt table): V1.3 or a later Codex task | later, before P5 | P5 |
| OD-A5-13 | Declare the observation producer (retire/replace Phase 7 observer) | decide before P4 observation migration | P4 |
| OD-A5-14 | Read-only runtime facts only the owner can supply: does `real_execution_resume_generations/` exist; which orchestrator code version is loaded (OD-01); is demo used; is the observer running | supply | OD-02/03/04 closure |

## Tooling

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/runtime_boundaries/tools/verify_evidence.py          # table, exit 1 on FAIL
PYTHONDONTWRITEBYTECODE=1 python3 docs/runtime_boundaries/tools/verify_evidence.py --write  # also data/evidence_check.json
```

Reads the bridge from git objects at a pinned commit, the platform from the checkout, evaluates one pure bridge function in isolation with `ast`, and reproduces OD-01 in a subprocess against a temporary runtime directory. No runtime file, broker, MT5, PostgreSQL, NATS or Kubernetes access.

## Limits

* Static analysis of two commits; **no live runtime, log, database or stream was inspected**. Anything about what is *running* is marked runtime-dependent.
* The retcode table (`06` 5) was written from memory of MT5 semantics with no external documentation available; it is explicitly unverified.
* Timing parameters (TTLs, horizons) are proposals to be tuned with measured latencies; the EA poll floor (5 s default) is an operator-configurable input and may differ in production.
* A4 is not modified; its corrections are listed in `10`.
