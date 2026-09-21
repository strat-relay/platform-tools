# 20 - Review checklist for CODEX-V1.3-P2-SIGNAL-MIGRATION (P4-relevant boundaries)

Use the **clean P2 handoff commit** in a separate worktree; do not inspect Codex's uncommitted work. ★ = blocking for P4.2 (ManagedTrade creation) or for the product/execution separation. Complements A5 `16` (P0/P1 checklist) and A4 `05` (signal migration).

## A. EntrySignal identity and payload (what ManagedTrade depends on)

| # | Check | Pass criterion | How |
|---|---|---|---|
| 1 ★ | **`signal_id` derivation unchanged** | still `stable_id("SIG", identity)` with the same identity dict for both adapters (Context: strategy_id, strategy_version, `strategy_instance_id="phase6"`, `economic_position_id`, `entry_opportunity_id`, `source_event_id`; Liquidity: `..."forward-paper"`, `source_event_id`); every historical id reproduces | golden vectors from legacy `signals.jsonl` rows re-derived by the P2 code |
| 2 ★ | **Signal event carries what a ManagedTrade needs** | `signal_id`, `strategy_id`, `strategy_version`, `strategy_instance_id` (=> ParameterSet for Liquidity), `symbol`/`canonical_symbol`, `direction`, `entry_type`, `entry_price`, `stop_price`, `target_price`, `risk_distance`, `decision_time`, `signal_timestamp`, `created_at`/`signal_emitted_at`, and the legacy refs `economic_position_id`, `entry_opportunity_id`, `setup_id` | event schema review vs `orchestration/models.StrategySignal` |
| 3 ★ | **`decision_time` and provenance preserved** | `decision_time` (= reference fill time) is not replaced by discovery time; `signal_emitted_at` remains the orchestrator discovery time; `provenance` (fingerprints, `classification`, `gap_recovery`, `source_state_reference`) survives byte-canonically | compare event vs legacy row field by field |
| 4 | **Evaluation reference usable by ManagedTrade** | the event/row references the V1.1 `Evaluation` by `evaluation_hash` + `trace_hash` + row id (not embedded); the Context adapter's L1 and Liquidity's L2 fidelity are recorded; the hash round-trips (A4 checklist 15) | tests |
| 5 | **Uniqueness key is not the hash** | canonical-result uniqueness is `(strategy_id, strategy_version, parameter_set_id, instrument, decision_time, candidate_id)`, not `evaluation_hash` (A5 `09` section 3.2) | schema constraints |
| 6 | **Ordering key** | events carry a stream/strategy key so per-stream ordering is possible; no global ordering claim | envelope review |

## B. What P2 must NOT do

| # | Check | Pass criterion | How |
|---|---|---|---|
| 7 ★ | **A signal event does not imply publication** | `signal.entry.created.v1` has no `published`/`PUBLISHED` semantics, no customer-facing field, no publication status; nothing consumes it as a distribution trigger | payload + consumers grep |
| 8 ★ | **No customer distribution hidden in P2** | the legacy `distribution_queue` / `delivery_status` placeholders are not "promoted" to a delivery mechanism; no `PublishedSignal`, entitlement, channel or commerce concept introduced | grep; schema review |
| 9 ★ | **No execution coupling introduced** | signals plane imports no execution/broker/bridge-write code; the signal event does not carry account, size, lot or execution mode; execution consumption of signals is unchanged (file consumer still the only sender) | import audit; diff |
| 10 ★ | **No broker-state / ownership authority movement** | no DB-authoritative broker state, ownership ledger, `real_state`, resume generation; tailers are read-only shadow (A5 `16` 13) | grep; mode defaults |
| 11 ★ | **OD-06 remains an explicit P5-only blocker** | docs/status still say P5 blocked; no fence, `T_cut`, attempt machine, or bridge change slipped in | handoff notes; grep |
| 12 ★ | **Frozen strategy identity preserved** | no edit to frozen runners; Context decision fingerprint (`P26`), Liquidity `source_hash`, runner bytes unchanged | A5 `verify_evidence.py` P16/P17/P26 still pass |
| 13 | **StrategyHost seam remains possible** | signal/evaluation ingestion accepts a source other than the runner-file tailer without schema change (tailer today; in-process host later, A5 `09`); adapter outputs go through one function boundary; no code assumes "the file is the source" beyond the tailer module | code review of the ingest boundary |
| 14 | **Legacy defects not silently fixed** | OD-01 (`tradeability_decisions`) output is still reproduced and classed `KNOWN_LEGACY_DEFECT` (A5 `10`); sizing rows remain analytical | `verify_evidence.py` P9/P22 |

## C. Projector and reconciliation (P2 side effects that P4 inherits)

| # | Check | Pass criterion |
|---|---|---|
| 15 ★ | **Projected `signals.jsonl` is append-only, deterministic, order-preserving, `created_at`-preserving** | replay yields byte-identical files; startup-baseline and resume-cutoff filters in `create_intents` cannot release old signals (A5 `11` section 4) |
| 16 | **Reconciliation is identity + canonical hash + terminal state**, not counts; `economic_position_id` and `entry_opportunity_id` are part of the compared identity | reconciler review |
| 17 | **Replay watermark authority** (`startup_epoch`) imported, not regenerated; stale-on-discovery signals recorded, never routed | tests |

## D. Contract hygiene for P4

| # | Check | Pass criterion |
|---|---|---|
| 18 | **Subject/stream names** | if P2 adds subjects, `signal.entry.created.v1` remains in `TRADING_CORE`; **no `trade.*` or management subject is claimed** by P2 (A6 `09`); no overlapping stream subject filters |
| 19 | **Envelope conformance** | `event_id` deterministic from `(causation, type)` for handler-produced events (A5/A4); `aggregate_type=signal`, `aggregate_id=signal_id`; `Nats-Msg-Id = event_id` |
| 20 | **Inbox names** | consumers (`trade-mgmt-open` in A2) can be added without changing signal schema |

## E. Mechanical re-verification

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/management_architecture/tools/verify_management_evidence.py
PYTHONDONTWRITEBYTECODE=1 python3 docs/runtime_boundaries/tools/verify_evidence.py   # if A5 docs are present in the handoff
```

Expected: A6 checks `M1`-`M26` still pass (P2 should not touch the Trade Manager, the Phase 7 observer, the executor, the bridge, or the strategy adapters' identity). Any flip in `M14`, `M15`, `M16` needs an explanation: `M14` = adapter/StrategySignal changed; `M15` = ownership row changed; `M16` = P2 added subjects/tables - allowed for `signal.*` only, and `M16`'s "no trade.* subject" clause must still hold.
