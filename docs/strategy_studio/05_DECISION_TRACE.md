# DecisionTrace

Every strategy evaluation — DETERMINISTIC or HYBRID, live or replay, signal or
rejection — produces one canonical, machine-readable, human-explainable
**`Evaluation`** containing a **`DecisionTrace`**.  A final boolean is never the record.

## 1. What exists today (so the model builds on it)

| Where | What it records | Gap |
|---|---|---|
| `liquidity_displacement_forward.decision_telemetry` | per candidate: `sweep{passed,…}`, `reclaim{passed}`, `displacement{passed, observed_body_atr, threshold_body_atr, observed_body_multiple, threshold_median_body,…}`, `mss{passed,…}`, `final_status`, `reason_code` — **observed vs threshold** already present | it **re-implements** `find_candidate`'s logic in a second function (drift risk); only for the base runner |
| `research/multitimeframe_liquidity_sniper/state_machine.py` | per setup `trace: [{state, timestamp}…]` and `terminal_reason` (`RECLAIM_TIMEOUT`, `DISPLACEMENT_NOT_CONFIRMED`, `STRUCTURE_CONFIRMATION_TIMEOUT`) | research only |
| Context runner events | lifecycle events (`SETUP_DETECTED`, `SETUP_INVALIDATED_BEFORE_ENTRY`, `NO_RETRACE`, `INVALIDATED_NO_REENTRY`, `NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY`) and **flags** (`HTF_CONTRADICTION_FLAG`, `NEAR_OPPOSING_STRUCTURE_FLAG`, `OPPOSING_STRUCTURE_VERY_CLOSE`) | event log, not a per-evaluation stage list; no observed/threshold |
| `paper_engine.Signal` | `reasons: list[str]`, `invalidation_reason`, `confidence_score` | flat strings |

Design consequence: the declarative runtime emits the trace **as a by-product of
evaluation** (one code path), eliminating the duplicated-logic pattern.

## 2. Structure

```jsonc
{
  "evaluation_id": "ev_9c1e…",            // deterministic: hash(strategy_version_id, instrument, as_of, direction)
  "schema": "decision-trace/1",
  "strategy_version_id": "sv_41ab…",
  "parameter_set_hash": "ps_77d0…",
  "engine_version": "engine/1",           // runtime-semantics version
  "instrument": "XAUUSD",
  "as_of": "2026-09-21T14:05:00Z",       // decision time; nothing later was visible
  "direction": "LONG",
  "inputs_digest": {"data_snapshot": "ds_2b…", "last_bar": {"M5": "…14:00", "M15": "…13:45", "H1": "…13:00"}},
  "outcome": "REJECT",                    // SIGNAL | REJECT | NO_CANDIDATE | PENDING | AWAITING_REVIEW | ERROR
  "terminal_reason": "INVALIDATION.H1_PROTECTED_LOW.BROKEN",
  "stages": [
    {
      "stage_id": "h1_bias", "kind": "CONTEXT_GATE", "rule": "structure.trend@1",
      "status": "PASS", "severity": "GATE",
      "observed": {"state": "UP"}, "thresholds": {"required": "WITH_DIRECTION"},
      "evidence_times": ["2026-09-21T13:00:00Z"],
      "explanation": "H1 structure is UP (higher highs and higher lows)."
    },
    {
      "stage_id": "zone_touch", "kind": "SETUP", "rule": "position.zone_interaction@1",
      "status": "PASS", "observed": {"zone_low": 4310.2, "zone_high": 4312.9}, "event_time": "2026-09-21T13:15:00Z",
      "explanation": "Price interacted with M15 support 4310.2–4312.9."
    },
    {
      "stage_id": "confirmation", "kind": "CONFIRMATION", "rule": "price_action.engulfing@1",
      "status": "PASS", "event_time": "2026-09-21T14:05:00Z",
      "explanation": "M5 bullish engulfing closed at 14:05."
    },
    {
      "stage_id": "h1_protected_low_broken", "kind": "INVALIDATION", "rule": "closes_through(context, lv.h1_protected)",
      "status": "FAIL", "severity": "GATE", "reason_code": "INVALIDATION.H1_PROTECTED_LOW.BROKEN",
      "observed": {"h1_close": 4306.8}, "thresholds": {"protected_low": 4308.0}, "margin": -1.2,
      "explanation": "H1 closed at 4306.8, below the protected low 4308.0."
    }
  ],
  "flags": [{"id": "rsi_stretched", "status": "FLAG", "observed": {"value": 72.4}}],
  "state_transitions": [{"from": "WAITING_FOR_CONFIRMATION", "to": "REJECTED", "at": "2026-09-21T14:05:00Z"}],
  "candidate": {"direction": "LONG", "proposed_entry": 4311.6},   // present for signals and rejected candidates
  "review": null,                          // HYBRID: {gate, question, answer, answered_by, answered_at}
  "trace_hash": "sha256:…"
}
```

**Stage `status`:** `PASS` · `FAIL` · `FLAG` (annotation, never rejects) · `PENDING`
(window still open) · `SKIPPED` (short-circuited by an earlier failure) ·
`NOT_EVALUATED` · **`UNKNOWN`** (required data unavailable — never coerced to pass or
fail; forces `outcome = ERROR`, fail-closed).

**Outcome:** `SIGNAL` (emit an EntrySignal) · `REJECT` (a candidate existed and failed) ·
`NO_CANDIDATE` (no setup reached its first stage) · `PENDING` (setup in progress) ·
`AWAITING_REVIEW` (HYBRID gate open) · `ERROR`.

Each stage records **`observed` and `thresholds` and `margin`** so a near-miss is
distinguishable from a blowout, and threshold-sensitivity analysis needs no re-run.

## 3. Reason codes

* Namespaced, stable, machine-readable: **`<KIND>.<RULE_OR_PRIMITIVE>.<CODE>`** —
  `SETUP.LIQUIDITY_SWEEP.NO_RECLAIM`, `CONFIRMATION.ENGULFING.NOT_PRESENT`,
  `INVALIDATION.H1_PROTECTED_LOW.BROKEN`, `DATA.INSUFFICIENT_BARS`,
  `SEQUENCE.WINDOW_EXPIRED.DISPLACEMENT`.
* Held in a **reason-code registry** (code, kind, severity, explanation template,
  `audience`), versioned with the primitive that emits it.  A definition may add
  rule-specific codes (`reason:` on an invalidation) but they are registered at freeze.
* **Codes never change meaning**; a semantic change is a new code.  Human text is
  rendered from the template with observed/threshold values, never stored as the identity.
* `audience` ∈ `INTERNAL` | `CUSTOMER_SAFE`.  A customer-facing explanation (V4+) is a
  *whitelisted projection* of the trace (e.g. "H1 structure invalidated") that never
  exposes thresholds or internal identifiers.  Designing that projection now costs
  nothing; building it is out of V1 scope.

### Mapping of legacy vocabulary (evidence from the code)

| Legacy | Source | Canonical (illustrative) |
|---|---|---|
| `REJECTED_NO_RECLAIM` | liquidity telemetry | `SETUP.LIQUIDITY_SWEEP.NO_RECLAIM` |
| `REJECTED_DISPLACEMENT_TOO_WEAK` | liquidity telemetry | `SETUP.DISPLACEMENT.TOO_WEAK` |
| `REJECTED_NO_MSS` | liquidity telemetry | `SETUP.MICRO_BREAK.NOT_CONFIRMED` |
| `ENTRY_EXPIRED_NO_RETRACE` | liquidity telemetry | `SEQUENCE.WINDOW_EXPIRED.RETRACE_FILL` |
| `FILLED` | liquidity telemetry | outcome `SIGNAL` |
| `RECLAIM_TIMEOUT` / `DISPLACEMENT_NOT_CONFIRMED` / `STRUCTURE_CONFIRMATION_TIMEOUT` | sniper state machine | `SEQUENCE.WINDOW_EXPIRED.RECLAIM` / `SETUP.DISPLACEMENT.NOT_CONFIRMED` / `SEQUENCE.WINDOW_EXPIRED.STRUCTURE` |
| `SETUP_INVALIDATED_BEFORE_ENTRY` | context runner | `INVALIDATION.EVENT_BAR_EXTREME.BROKEN` |
| `NO_RETRACE` | context runner (12-bar wait) | `SEQUENCE.WINDOW_EXPIRED.RETRACE` |
| `NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY`, `TARGET_BEHIND_ENTRY` | context runner | `TARGET.GEOMETRY.NOT_TRADABLE` |
| `HTF_CONTRADICTION_FLAG`, `NEAR_OPPOSING_STRUCTURE_FLAG`, `OPPOSING_STRUCTURE_VERY_CLOSE` | context runner | **flags** (`severity: FLAG`), not rejections — the Context strategy deliberately records these as descriptive |
| `POSITION_ALREADY_OPEN_SCALEIN_DISABLED` | context runner | `POLICY.SCALE_IN.DISABLED` |

The mapping is a data table used by *legacy adapters* (`06_…` §6); it does not
change any legacy code.

## 4. One trace, many uses

| Use | How the same trace serves it |
|---|---|
| Generated signals | `outcome=SIGNAL`; the EntrySignal's provenance references `evaluation_id` and `trace_hash` |
| Rejected candidates | `outcome=REJECT` with the failing stage — the Signals→Rejected view |
| Backtests / forward tests | the same runtime code path; backtest summaries aggregate stage outcomes into an **attrition funnel** (how many pass each stage), generalising the existing `audit_attrition` |
| Live evaluation | identical; recorded on every evaluation cycle that has ≥ 1 stage progress |
| Validation / disagreement review | the engine side of every case (`04_…`); blame table is a query over stages |
| Console debugging | stage list with observed/threshold and the exact bars used (`evidence_times`) |
| Customer explanations (later) | audience-filtered projection |

## 5. Volume, retention and reproducibility

`NO_CANDIDATE` evaluations dominate (every bar × instrument).  Retention tiers:

| Tier | Stored for | Form |
|---|---|---|
| `FULL` | signals, rejected candidates, validation cases, every HYBRID review, disagreements | full trace |
| `SUMMARY` | backtest/forward runs | per-run attrition funnel + `FULL` for a sampled and all-candidate subset |
| `ON_DEMAND` | `NO_CANDIDATE` and pending noise | **not stored**; recomputable exactly from `(strategy_version_id, instrument, as_of, data snapshot)` |

Traces are **reproducible by construction** (deterministic runtime, pinned primitives and
engine version, `inputs_digest`): recomputing yields the same `trace_hash`.  This is also
the regression test for engine changes: a corpus of stored traces must recompute
identically under any engine build that claims the same `engine_version`.

## 6. Trace fidelity levels (for the migration)

| Level | Content | Producer |
|---|---|---|
| **L0** | outcome + terminal reason only | minimal legacy adapter |
| **L1** | ordered stage/event list from existing telemetry/events, no observed/thresholds | Context adapter (events) |
| **L2** | stage list with observed/thresholds where the legacy telemetry has them | Liquidity adapter (`decision_telemetry`) |
| **L3** | full trace as specified above | `DeclarativeStrategyRuntime` |

Consumers must handle L0–L3; the Console shows the level so a coarse legacy trace is never
mistaken for a full one.
