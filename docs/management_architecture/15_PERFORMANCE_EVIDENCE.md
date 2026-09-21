# 15 - ENTRY_ONLY vs ENTRY_PLUS_TM evidence produced by P4 (data requirements; no calculation)

Grounded in A2 `06_PERFORMANCE_EVIDENCE.md` (series key, provenance, "two outcomes per trade") and `03` section 4-5. P4 produces the **inputs** that make the two series computable and defensible; it computes no performance.

## 1. Rules (from the task and A2)

1. **Never blend** `ENTRY_ONLY` and `ENTRY_PLUS_TM`. Series key = `(stream_id, series_kind, provenance, strategy_version_id, trade_manager_version_id | null, window)`.
2. **Managed outcome uses only decisions actually emitted in real time by the bound, frozen TM version.** A decision counts only if it was **persisted** (`persisted_at`) before it is applied, and its `observation_id` refers to data with `observed_at <= decision_time`.
3. **No hindsight management**: no retroactive re-run of a policy over history to produce a "managed" number; no counterfactual is ever labelled FORWARD.
4. **No retroactive better policy**: applying a later TradeManagerVersion to old trades is a `BACKTEST`/research series, labelled as such.
5. **A derived TM version starts its own FORWARD clock** at its `frozen_at`; its evidence never inherits the parent's.

## 2. What each series is

| | `ENTRY_ONLY` | `ENTRY_PLUS_TM` |
|---|---|---|
| Question | how does the strategy's own frozen entry/stop/target logic perform? | how does the reference trade perform when the bound TM's real-time decisions are applied in order? |
| Outcome source | the **strategy's frozen ledger** (stop/target/exit events) - never recomputed by the TM | the **managed reference simulation** in Trade Management (decisions applied to the reference trade) |
| Requires TM? | no | yes: a `FROZEN` (or explicitly labelled) `trade_manager_version_id` |
| Provenance | FORWARD after strategy freeze | FORWARD after **TM** freeze |

Every eligible closed trade may have both; each contributes to its own series only.

## 3. Data P4 must record

| Datum | Where | Why |
|---|---|---|
| EntrySignal geometry and reference fill: `entry, stop, target, decision_time, signal_id`, feed | `managed_trade` (copied at open) | both series start from the same reference trade |
| Bound `strategy_version_id`, `parameter_set_id`, `trade_manager_version_id`, `bound_at` | `managed_trade` (immutable) | series attribution; no silent rebind |
| TM version `frozen_at` and eligibility classification (`PROSPECTIVE_ELIGIBLE`, `PRE_FREEZE_EXPOSED`, `LEFT_TRUNCATED`) | `managed_trade` | evidence clock; Phase 7's vocabulary |
| Strategy exit event (ledger): time, reason, price, realised R | `managed_trade` / `trade_closed` | `ENTRY_ONLY` outcome |
| Every `trade_observation` used (id, seq, `observed_at`, quote, `bars_ref.digest`) | `trade_observation` | causality proof and replay |
| Every decision: id, action, parameters, reason codes, `decision_time`, **`persisted_at`**, trace ref | `trade_manager_decision` | only persisted, real-time decisions enter the managed series |
| Applied-decision ledger of the managed track: stop movements, partial fractions, managed exit | managed simulation rows | `ENTRY_PLUS_TM` outcome |
| Cost model version and spread source | recorded per outcome (A2 O13) | comparability |
| `data_status` (FORWARD/REPLAY/BACKTEST) | observation and decision | series provenance |
| `publication_status` (PUBLISHED/UNPUBLISHED) | `managed_trade` | A2 orthogonal attribute |

## 4. Real-time admissibility rule (the anti-hindsight guard)

A decision is applied to the managed track **iff** all hold:

* it is the decision for an observation with `observation_seq` greater than every previously applied one (in-order);
* `persisted_at` <= the time the *next* observation for that trade was recorded (it was known before the market moved on) - configurable latency bound `L`;
* the trade was `OPEN` at `decision_time`;
* the decision's version equals the trade's bound version (BOUND track).

Anything else is recorded (`LATE_DECISION`, `WRONG_TRACK`) and excluded from the managed series. This rule is data-only; P4 stores what is needed to evaluate it.

## 5. What is not FORWARD evidence

| Artefact | Status |
|---|---|
| `experiments.MultiPolicyExperimentRunner` hypothetical outcomes (`BE_PROTECTION_*`, trailing, hybrid) | **research/counterfactual**; parameters set after seeing outcomes; never FORWARD |
| Phase 7 thresholds and checkpoints | research observations of the strategy's own trades |
| `replay.py` fixture output | replay/backtest |
| CHALLENGER-track decisions | own series, own clock (`07`, `06`) |
| any managed result computed after the fact from observations | BACKTEST |

## 6. Interface to Performance (not built in P4)

`trade.closed.v1` carries both outcomes (`entry_only{...}`, `managed{...}`) with references to the TM version, decision ids and the observation window digest; Performance ingests, classifies and publishes with methodology versions (A2). P4's deliverable is that everything Performance needs is **already persisted with the causality fields above**.
