# 02 - A6 P2 review checklist run against the actual P2 (`8f49aef`)

Checklist: A6 `docs/management_architecture/20_CODEX_P2_REVIEW_CHECKLIST.md` (20 items). Verdicts are against the criterion as written. **P2 was not modified to make anything pass.** `N-A` = the criterion concerns a component P2 did not build.

**Totals: 20 items - 12 PASS, 6 FAIL, 2 N-A.** Blocking (★) failures: items 2 and 3.

| # | Item | Verdict | Evidence / reason |
|---|---|---|---|
| 1 ★ | `signal_id` derivation unchanged | **PASS** | P2 does not derive `signal_id`; it passes the legacy id through verbatim (`R11`, `canonical_signal`). Nothing can drift - but there is also **no golden-vector test** re-deriving legacy ids (recommendation in `12`) |
| 2 ★ | Signal event carries what a ManagedTrade needs | **FAIL** | payload = ids/hashes + `source_reference` only (`R5`); DB payload = `Evaluation.to_dict()` without geometry, `economic_position_id`, `entry_opportunity_id`, `setup_id`, `strategy_instance_id`, mechanism, `signal_emitted_at` (`R4`, `R25`). Instrument/direction/decision_time/version are reachable only through the evaluation row |
| 3 ★ | `decision_time` and provenance preserved | **FAIL (partial)** | `decision_time` **is** preserved as the evaluation's `decision_time` (reference fill time) and `signal_emitted_at` as outbox `occurred_at` (`R6`). But provenance is **mutated** (keys dropped; `legacy_source_reference` path:line, `legacy_source_hash`, `as_of` added into the hashed provenance, `R12`), `signal_emitted_at`/`created_at` are not stored on any strategy row, and the string `decision_time` is not normalised (`R10`) |
| 4 | Evaluation reference usable | **PASS** | event carries `evaluation_id`, `evaluation_hash`, `trace_hash`; `strategy.decision_traces` holds `trace_hash`; fidelity L1 recorded. (The *stability* of the hash is item 5's failure.) |
| 5 | Uniqueness key is not the hash | **FAIL** | canonical-result uniqueness `(strategy_id, strategy_version, parameter_set_id, instrument, decision_time, candidate_id)` does not exist (`R22`); the evaluation PK **is** the hash and the hash is unstable (`R1`), so re-ingest can create additional evaluation families for one signal |
| 6 | Ordering key | **FAIL (minor)** | envelope has `aggregate_id = signal_id`, version 1; no strategy/stream key in envelope or payload, so per-stream ordering/filtering is impossible without a DB read (`R5`, `R6`). The candidate event is filed under `aggregate_type='signal'` |
| 7 ★ | Signal event does not imply publication | **PASS** | no publication/PUBLISHED/customer field in payload; no consumer treats it as a delivery trigger (`R5`, `R13`) |
| 8 ★ | No customer distribution hidden | **PASS** | no Distribution, entitlement, channel or `PublishedSignal` code; legacy placeholders untouched (`R14`, `R15`) |
| 9 ★ | No execution coupling | **PASS** | `migration/signal.py` imports no execution/Trade Manager/bridge/orchestration module (`R13`); event carries no account/size/mode; execution consumer byte-identical (`R15`) |
| 10 ★ | No broker-state / ownership authority movement | **PASS** | no broker-state or ownership code in the diff; only shadow signal tables (`R14`) |
| 11 ★ | OD-06 remains an explicit P5-only blocker | **PASS** | status doc states no execution cutover; no fence/attempt/bridge change in the diff (`R14`, `R15`) |
| 12 ★ | Frozen strategy identity preserved | **PASS** | runners, strategy modules, Phase 7 observer, consumer, `signal_orchestrator.py` byte-identical to `17c09b2` (`R15`); A5 fingerprint checks P16/P17/P26 pass |
| 13 | StrategyHost seam remains possible | **PASS (with caveat)** | `ingest_signal(conn, CanonicalSignal)` is source-agnostic and accepts `source_reference` from any host. Caveat: the hash-affecting `source_reference` makes different hosts produce different evaluation ids (item 5) |
| 14 | Legacy defects not silently fixed | **FAIL (disclosed deviation)** | OD-01 **was repaired** (`R16`): `tradeability_decisions` declared, tests added. It is documented (status doc) and minimal, but contradicts the criterion "still reproduced and classed `KNOWN_LEGACY_DEFECT`". Effect: orchestrator-side tradeability gate now executes in REAL mode (analytical sizing rows only; the REAL consumer still ignores them). A5 checks P9/P22 legitimately flip |
| 15 ★ | Projected `signals.jsonl` append-only/deterministic | **N-A** | P2 builds no projector (S0 only; legacy remains authority) |
| 16 | Reconciliation is identity + canonical hash + terminal state, incl. economic/opportunity ids | **FAIL** | `reconcile()` is a generic comparator with semantic fields `strategy_id, instrument, direction, decision_time, decision`; there is no harness that builds legacy and DB sets from real data (`R20`) and the legacy "hash" (raw-line sha) and DB hash (evaluation hash) are different constructs; `economic_position_id`/`entry_opportunity_id` are not compared because they are not stored (`R4`) |
| 17 | Replay watermark authority imported | **N-A** | P2 does not touch the replay guard |
| 18 | Subject/stream names | **PASS** | only `strategy.candidate.detected.v1` and `signal.entry.created.v1` (existing, `TRADING_CORE`); no `trade.*` claimed (`R19`) |
| 19 | Envelope conformance | **PASS (with gap)** | event ids deterministic, `aggregate_type/id` present. `Nats-Msg-Id` is **not** set by the publisher (`R17`) - a V1.2 adapter gap, not a P2 regression; no relay exists (`R18`) |
| 20 | Inbox names | **PASS** | consumer `p2-signal-shadow` claims `(consumer, event_id)`; a `trade-mgmt-open` consumer needs no signal schema change |

## Results by requested attention area

| Area | Result |
|---|---|
| stable `signal_id` derivation | PASS (passthrough) |
| restart / duplicate stability | signal, candidate, event ids stable; **evaluation id unstable** (`R1`, `R2`) |
| `decision_time` | value preserved (reference fill time); representation not canonical |
| no future-data leakage | outcome/future keys stripped from provenance; but the filter is a blocklist (`outcome, future_return, pnl, exit, fill`), not an allowlist |
| provenance | present but mutated and hash-affecting |
| strategy version binding | ambiguous label `V1` shared across strategies (`R7`, `R8`) |
| ParameterSet binding | none (`R9`) |
| instrument identity | PASS (canonical symbol) |
| Evaluation / DecisionTrace linkage | PASS (hashes in event and tables) |
| publication / execution not implied | PASS |
| entitlement absent from Trading Core | PASS (no entitlement concept anywhere in P2) |
| no Personal Execution / broker / account identity required | PASS |
| frozen hashes unchanged | PASS |
| OD-06 outside P2 | PASS |
| **ManagedTrade has sufficient information to bind immutably** | **FAIL** - no geometry, no unique version/parameter references, no instance/legacy refs in the event or DB record (`R4`, `R5`, `R7`, `R9`) |

## Consequence

Two blocking findings (items 2, 3) and four non-blocking ones (5, 6, 14, 16) are reported. None is an incompatibility with the **observation-producer decision** of ADR-0004 (`05`); all of them concern the **EntrySignal -> ManagedTrade input contract** and the quality of P2's shadow evidence, and are resolved by a small, well-bounded **P2 amendment** (`12`) that gates `P4.2` and `P2.1` but not `P4.1`.
