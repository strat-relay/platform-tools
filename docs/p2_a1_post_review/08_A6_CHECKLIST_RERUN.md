# 08 - A6 P2 review checklist, rerun against P2-A1

Baseline for the checklist: A6 `docs/management_architecture/20_CODEX_P2_REVIEW_CHECKLIST.md` (20 items), as previously run in A7 `docs/p2_p4_reconciliation/02_A6_P2_CHECKLIST_RESULTS.md` against `8f49aef` (12 PASS / 6 FAIL / 2 N-A). Rerun here against `3a39fd7`. **Historical A7 document is not modified**; this is a new, dated rerun.

| # | Item | A7 verdict (`8f49aef`) | **P2-A1 verdict (`3a39fd7`)** | Disposition |
|---|---|---|---|---|
| 1 ★ | `signal_id` derivation unchanged | PASS | **PASS** | unchanged - still verbatim passthrough |
| **2 ★** | Signal event carries what a ManagedTrade needs | **FAIL** | **RESOLVED (PASS)** | `strategy.entry_signals` now exists with geometry, legacy refs, instance, times (`02`); event payload extended with `entry_signal_hash, strategy_ref, strategy_id, instrument, direction, decision_time, signal_emitted_at` |
| **3 ★** | `decision_time` and provenance preserved | **FAIL (partial)** | **RESOLVED (PASS)** | provenance no longer mutated by hashed-in ingest metadata (`V2`); `decision_time` normalised deterministically (`V8`); `signal_emitted_at` separately persisted |
| 4 | Evaluation reference usable | PASS | **PASS** | unchanged, now on a stable identity |
| **5** | Uniqueness key is not the hash | **FAIL** | **RESOLVED (PASS)** | `entry_signals_canonical_result_uq` added (`V27`) |
| **6** | Ordering key | **FAIL (minor)** | **PARTIAL** | `strategy_ref` now in the payload gives a coarse filter key; still no explicit per-stream ordering sequence in the envelope (unchanged; low priority, `aggregate_version=1` for every signal event as before) |
| 7 ★ | Signal event does not imply publication | PASS | **PASS** | unchanged |
| 8 ★ | No customer distribution hidden | PASS | **PASS** | unchanged |
| 9 ★ | No execution coupling | PASS | **PASS** | confirmed again (`V31`, `V33`) |
| 10 ★ | No broker-state / ownership authority movement | PASS | **PASS** | unchanged |
| 11 ★ | OD-06 remains an explicit P5-only blocker | PASS | **PASS** | unchanged; untouched by P2-A1 |
| 12 ★ | Frozen strategy identity preserved | PASS | **PASS** | confirmed again (`V32`) |
| 13 | StrategyHost seam remains possible | PASS (caveat) | **PASS, caveat resolved** | `ingest_signal` is still source-agnostic; the caveat (unstable hash under different hosts) is now resolved by `V1` |
| **14** | Legacy defects not silently fixed (OD-01) | **FAIL (disclosed deviation)** | **SUPERSEDED_BY_APPROVED_DECISION** | A7 (`docs/p2_p4_reconciliation/05` and `12`, `OD-A7-5`) already accepted the OD-01 repair as a deliberate, documented, test-covered deviation with no P5 crossing. This item is not re-litigated as a defect; it is treated as approved architecture going forward, per this review's instruction not to keep treating it as a failure |
| 15 ★ | Projected `signals.jsonl` append-only/deterministic | N-A | **N-A** | still no projector built (unchanged scope) |
| **16** | Reconciliation is identity + canonical hash + terminal state | **FAIL** | **PARTIAL** | a real, semantic, file-vs-database reconciler now exists (`05` section 2) - a substantial improvement - but the `EXPECTED_LAG`/`KNOWN_LEGACY_ANOMALY` distinction is unimplemented, so it is not yet an *exact* match to A7's full taxonomy (`05` section 2.2) |
| 17 | Replay watermark authority imported | N-A | **N-A** | unchanged scope |
| 18 | Subject/stream names | PASS | **PASS** | still only `strategy.candidate.detected.v1`/`signal.entry.created.v1`; candidate event now correctly filed under `aggregate_type='candidate'` (an improvement beyond the checklist's letter) |
| 19 | Envelope conformance | PASS (with gap) | **RESOLVED (PASS)** | `Nats-Msg-Id` now set (`V23`); a relay now exists (`06`) - both prior gaps closed |
| 20 | Inbox names | PASS | **PASS** | unchanged |

## Totals

**20 total - 15 PASS, 2 PARTIAL, 0 FAIL, 2 N-A, 1 SUPERSEDED.**

Precisely: of the 20 items, **15 are now clean PASS** (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 18, 19, 20), **2 are PARTIAL** (item 6: ordering key, minor and non-blocking; item 16: reconciliation taxonomy, disclosed in `05`), **2 remain N-A** (items 15, 17 - out of P2-A1's scope by design), and **1 is SUPERSEDED_BY_APPROVED_DECISION** (item 14, OD-01, per A7's own architectural acceptance). **Zero items are a clean FAIL.**

Every item A7 explicitly asked to be revisited (2, 3, 5, 6, 14, 16) is accounted for above with an explicit disposition, as required.
