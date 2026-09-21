# P2 <-> P4 architecture reconciliation (A7)

Agent CLAUDE-A7-P2-P4-ARCHITECTURE-RECONCILIATION. Baseline: trading-platform `8f49aef` (Codex P2 handoff; P2 started from `17c09b2`) in an isolated worktree (`claude-a7/p2-p4-reconciliation`). Inputs: A6 `db0b3d4` (read from its worktree; not part of this baseline), A5 `37321e5` (present in the baseline), A4 `89ba8b2`, A3 `26d4a40`. Precedence for overlapping questions: **A7 > A6 > A5 > A4**; no historical document was modified. **Documentation and read-only evidence tooling only.** No production, strategy, Trade Manager, bridge, Kubernetes, PostgreSQL/NATS runtime, or broker action.

## Verdicts

| Question | Answer |
|---|---|
| Can ADR-0004 be moved from RECOMMENDED to APPROVED without qualification? | **Yes.** No incompatibility between P2 and the producer/owner/dependency/event/order/dedupe decision. Ten implementation-level adjustments are recorded (`05`); none is a qualification. **OD-A6-1 resolved.** |
| Does the actual P2 provide what ManagedTrade creation needs? | **No.** `signal.entry.created.v1` carries ids/hashes only; the persisted signal record lacks entry/stop/target, legacy refs, instance, `signal_emitted_at`, and a unique StrategyVersion/ParameterSet reference (`01`, `03`; `R4`, `R5`, `R7`-`R9`). Fixed by **P2-A1** (`12`), which gates **P4.2** and **P2.1**, not P4.1 |
| Is P2's Evaluation identity stable? | **No** (`R1`, `R2`): the hash includes `path:line` where the line is chunk-relative; the same signal can produce several evaluation families. `signal_id`, `candidate_id` and event ids **are** stable |
| A6 P2 checklist | **20 items: 12 PASS, 6 FAIL, 2 N-A** (`02`); blocking failures: items 2 and 3 |
| Is `stable_id("MT", {signal_id})` sufficient and safe? | **Yes**, under stated conditions (`03` section 4) |
| TradeManagerVersion V1 | defined (`04`); `TM-NONE-1` is the first canonical version; silent rebinding prevented by 7 structural mechanisms |
| Phase 7 observer | **not canonical**; retire after canonical path is proven; gate G1-G6 (`06`); **OD-A6-5 resolved** |
| Publication boundary | Publication Gate + `PublishedSignal` + `ManagementSignal` owned by the **Signals domain**; entitlement affects distribution only; **blocks no stage in P4.1-P4.5 and only the `PUBLISHED` branch of P4.6** (`07`) |
| Ready for P2.1 live shadow? | **No - not until P2-A1** (evaluation-identity fix, durable malformed record, real reconciler, relay, flags) |
| Ready for P4.1 implementation? | **Yes** (contracts/schema; needs only the EntrySignal record *interface*, `03`) |
| Ready for P4.2 implementation? | **No - after P2-A1** (`12`: A1-1, A1-3, A1-4, A1-5) |
| Ready for P5? | **No** - blocked by OD-06 |

## Documents

| # | Document | Task part |
|---|---|---|
| 01 | [`01_P2_ACTUAL_IMPLEMENTATION.md`](01_P2_ACTUAL_IMPLEMENTATION.md) | 1 |
| 02 | [`02_A6_P2_CHECKLIST_RESULTS.md`](02_A6_P2_CHECKLIST_RESULTS.md) | 2 |
| 03 | [`03_ENTRY_SIGNAL_TO_MANAGED_TRADE_CONTRACT.md`](03_ENTRY_SIGNAL_TO_MANAGED_TRADE_CONTRACT.md) | 3 |
| 04 | [`04_TRADE_MANAGER_VERSION_V1_CONTRACT.md`](04_TRADE_MANAGER_VERSION_V1_CONTRACT.md) | 4 |
| 05 | [`05_ADR_0004_FINAL.md`](05_ADR_0004_FINAL.md) | 5 |
| 06 | [`06_PHASE7_RETIREMENT_GATE.md`](06_PHASE7_RETIREMENT_GATE.md) | 6 |
| 07 | [`07_PUBLICATION_BOUNDARY.md`](07_PUBLICATION_BOUNDARY.md) | 7 |
| 08 | [`08_EVIDENCE_CONTRACT.md`](08_EVIDENCE_CONTRACT.md) | 8 |
| 09 | [`09_P3_P4_P5_FINAL_BOUNDARIES.md`](09_P3_P4_P5_FINAL_BOUNDARIES.md) | 9 |
| 10 | [`10_P4_1_P4_2_IMPLEMENTATION_CONTRACT.md`](10_P4_1_P4_2_IMPLEMENTATION_CONTRACT.md) | 10 |
| 11 | [`11_P2_1_LIVE_SHADOW_CONTRACT.md`](11_P2_1_LIVE_SHADOW_CONTRACT.md) | 11 |
| 12 | [`12_P2_AMENDMENT_REQUIREMENTS.md`](12_P2_AMENDMENT_REQUIREMENTS.md) | P2-A1 |
| - | `tools/verify_p2_p4_reconciliation.py`, `data/p2_p4_evidence.json` | 25 re-runnable checks against the actual P2 |

## Superseded / adjusted A6 statements (A7 governs)

| A6 | A7 |
|---|---|
| ADR-0004 RECOMMENDED | **APPROVED** (`05`) |
| A6 checklist assumed the signal event carries geometry | it does not; EntrySignal canonical record + payload extension required (`03`, `12`) |
| subject `trade.observation.recorded.v1.<instrument>` | exact `trade.observation.recorded.v1` (`validate_subject`, `R17`) |
| `strategy_version_id` in payloads | `strategy_ref`; `strategy_version_id` NULL until a real registry exists |
| OD-A6-2 / -3 / -4 / -5 / -6 | resolved: `bars_ref`; evaluation tracks; `TM-NONE-1`; Phase 7 not canonical; `signal.management.published.v1` |
| OD-A6-7 | ownership resolved (Signals domain); timing gates only P4.6b |
| A6 P4.6 single stage | split into P4.6a (gate + records, all WITHHELD) and P4.6b (PUBLISHED) |
| A6 "P2 must not fix OD-01" (via A5) | P2 did repair it; recorded as a deliberate deviation (`02` item 14, `12`) |
| A2 `05`: `management_publication` under `trade_management` | belongs to Signals |

## Open decisions

| Id | Decision | Recommendation | Blocks |
|---|---|---|---|
| **OD-A7-1** | Authorise P2-A1 (`12`) as a Codex task now, in parallel with P4.1 | yes | P4.2, P2.1 |
| OD-A7-2 | Sign off P2.1 thresholds (`11` section 1, `Delta`, quiesced-run count) | adopt as proposed or adjust with the natural signal rate | P2.1 gate |
| OD-A7-3 | Timing/owner of the Signals-domain `PublishedSignal` deliverable | schedule before P4.6b | P4.6b |
| OD-A7-4 | Bounds `L_create` / `L_start` for FORWARD eligibility (creation and observation-start lag) | set from measured discovery latency in P2.1 | P4.2 eligibility values |
| OD-A7-5 | OD-01 repair: accept the tradeability gate now running in the orchestrator's analytical REAL path, or gate it | decide; no P5 impact | none |
| OD-A7-6 | Real StrategyVersion registry (unique ids, real source identity) - owner and timing | before `strategy_version_id` is used | future |
| OD-A7-7 | SignalStream / StreamBinding Trading Core deliverable (the `STREAM` resolver) | after P4.5; only affects later trades | future |
| OD-08 | retention/sizing of observation rows and `TRADING_OBSERVATION` | measure in P4.3 | production |
| OD-06 | broker-write fencing | as A5 | **P5** |

## Tooling

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/p2_p4_reconciliation/tools/verify_p2_p4_reconciliation.py          # table; exit 1 on FAIL
PYTHONDONTWRITEBYTECODE=1 python3 docs/p2_p4_reconciliation/tools/verify_p2_p4_reconciliation.py --write  # also data/*.json
```

Pure P2 functions on temporary data plus source/`git` reads; no database, NATS, broker, runtime file or Kubernetes access. Note: the A5 evidence tool (`docs/runtime_boundaries/tools/verify_evidence.py`) reports 6 flips at this baseline - **P9/P22** are the legitimate OD-01 repair; **P1/P10/P12/P18** are self-reference artefacts of A5's own documents/tool now being in the tree, not code changes.

## Limits

Static review of `8f49aef`; no runtime, cluster, database or stream was inspected. Timing/count thresholds are proposals. The K8s deployment of the Context strategy was not examined; `11` states requirements, not observations.
