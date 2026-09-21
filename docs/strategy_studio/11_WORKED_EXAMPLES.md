# Worked examples

Files: `examples/liquidity_displacement.v1.definition.yaml`,
`examples/liquidity_displacement.v1.parameter_sets.yaml`,
`examples/htf_bias_retrace_engulf.v1.definition.yaml`,
`examples/primitive_catalog_v1_seed.yaml` — all checked by `tools/check_examples.py`
(internal consistency only; **not** a compiler, and **no parity with legacy is claimed** —
see `10_…` §4).  **All numeric results in this document are illustrative, not measured.**

---

## A. Liquidity Displacement, expressed without strategy-specific code

### A.1 Field-by-field mapping to what was read in `liquidity_displacement.py`

| Legacy behaviour | Definition element |
|---|---|
| `_levels`: last 36 bars → confirmed swings (lookback 2), last 3; plus prior session (08–21 UTC) extremes; take the higher of the lows / highs | `levels.reference` = `liquidity.reference_levels@1` with `level_lookback_bars`, `swing_lookback`, `recent_swings`, `session_*_hour` |
| sweep: bar trades beyond the level and **closes back** across it → direction | stage `sweep` = `liquidity.sweep@1 {require_reclaim_close: true}`; `direction.mode: DETECTED` from `sweep` |
| scan up to 5 bars after the sweep for a directional strong candle **and** a close beyond the recent 5-bar extreme | stage `displacement` (`after: sweep`, `within: $max_structure_break_bars`, `select: FIRST`) with two `require` predicates |
| ATR taken at the sweep bar (including it); median body of the 12 bars before it | `features.atr`, `features.median_body`, referenced as `f.atr@s.sweep` |
| entry = displacement bar low + 50 % of its range (long) | `derive.entry_level` = `position.bar_fraction_level@1 {fraction: $entry_fraction}` |
| fill: within 3 bars after displacement, touch **and** close on the entry side | stage `retrace_fill` = `execution.touch_and_hold@1`, `within: $max_retrace_bars` |
| stop = sweep extreme ∓ max(0.10 ATR, 1.25 × spread, broker minimum) | `stop.price` expression |
| target = 1.25 R; time exit 120 minutes | `target {R_MULTIPLE, $target_r}`, `exits[TIME]` |
| `len(m5) ≥ 40 and len(m15) ≥ 30` | `data_requirements` |
| four instances (base, xau33, btc25, usdjpy25) | four ParameterSets, one definition |

### A.2 DecisionTrace — a rejection (illustrative)

```jsonc
{
  "strategy_version_id": "sv_liq_disp_v1", "instrument": "XAUUSD", "as_of": "2026-09-21T09:20:00Z",
  "direction": "LONG", "outcome": "REJECT", "terminal_reason": "SETUP.DISPLACEMENT.TOO_WEAK",
  "stages": [
    {"stage_id": "sweep", "status": "PASS", "rule": "liquidity.sweep@1",
     "observed": {"level": 4302.10, "extreme": 4300.85, "distance": 1.25},
     "event_time": "2026-09-21T08:55:00Z", "explanation": "Low 4300.85 swept level 4302.10 and closed back above."},
    {"stage_id": "displacement", "status": "FAIL", "rule": "price_action.displacement@1",
     "reason_code": "SETUP.DISPLACEMENT.TOO_WEAK",
     "observed": {"best_body_atr": 0.31, "best_close_location": 0.72},
     "thresholds": {"min_body_atr": 0.50, "min_close_location": 0.60}, "margin": -0.19,
     "explanation": "Best candidate in the 5-bar window had body 0.31 ATR (< 0.50 required)."},
    {"stage_id": "retrace_fill", "status": "SKIPPED"}
  ],
  "trace_hash": "sha256:…"
}
```
This corresponds to legacy `REJECTED_DISPLACEMENT_TOO_WEAK` (whose telemetry already stores the observed and threshold body-ATR), but produced by the **same evaluation** that would have produced the signal.

---

## B. Multi-timeframe strategy: from words to a HYBRID definition

### B.1 The trader's description (Describe tab)

> "On gold I look at the H1 for the bias. If H1 is making higher highs and higher lows I only
> want longs. I wait for price to pull back to an M15 support zone. Then on M5 I want an
> engulfing or a rejection candle. The pullback shouldn't be too deep. And the H1 structure
> has to look clean."

### B.2 Extraction (analyst proposals, none applied)

| Statement | Term | Ambiguity | Proposed slot |
|---|---|---|---|
| "H1 … higher highs and higher lows" | H1 bias | needs `structure.trend` parameters | context gate (`structure.trend@1`) |
| "pull back to an M15 support zone" | support zone | zone definition (cluster tolerance, look-back) | `levels.m15_zones` (`structure.support_resistance_zones@1`) |
| "engulfing or a rejection candle" | confirmation | rejection thresholds; how long after touch | stage `confirmation` (`any_of`), window |
| "shouldn't be too deep" | **too deep** | `AMB-0004 UNDEFINED_THRESHOLD` | — (see B.3) |
| "H1 structure … clean" | **clean** | `AMB-0007 SUBJECTIVE_JUDGMENT` | — (see B.4) |

### B.3 "Too deep" → structural invalidation (see the dialogue in `03_…` §B.3)
Accepted interpretation `INT-0012`: invalidate if **H1 closes through the protected swing**.
The definition carries it as `invalidation.h1_protected_low_broken` with
`origin: {interpretation: INT-0012}` and the customer-safe-capable reason code
`H1_STRUCTURE_INVALIDATED` (canonical `INVALIDATION.H1_PROTECTED_LOW.BROKEN`).  The
trader's first instinct (a percentage) was *measured* against the examples and
**not** adopted — evidence was overlapping.

### B.4 "Clean" → a declared review gate (HYBRID)
The trader cannot define "clean"; the system does **not** invent a threshold.
`AMB-0007` is `DEFERRED_TO_REVIEW_GATE`; the definition declares
`review_gates.h1_structure_quality` (question, allowed answers, evidence, `on_timeout:
REJECT`).  Consequence: `evaluation_mode: HYBRID`.

### B.5 Blind validation case and result (illustrative)

Case object (what the trader sees is rendered from `snapshot_hash`; nothing after
`decision_time`):

```jsonc
{
  "case_id": "vc_0417", "pool": "VALIDATION_UNTOUCHED", "stratum": "NEAR_MISS_LATE",
  "instrument": "MASKED", "decision_time": "MASKED", "roles": {"context": "H1", "structure": "M15", "trigger": "M5"},
  "snapshot_hash": "sha256:…", "mode": "BLIND"
}
```
Human answer (stored before the engine is revealed):

```jsonc
{"case_id": "vc_0417", "answer": "YES", "reason_codes": [], "missing_information": null,
 "answered_at": "2026-09-22T10:41:07Z", "role": "reviewer"}
```
Engine (hidden until answered): `ENGINE_NO` at stage `retrace_complete`, code
`SEQUENCE.WINDOW_EXPIRED.RETRACE`, margin `-2 M15 bars`.

Round summary (**synthetic numbers for illustration only**):

| Human \ Engine | ENGINE_YES | ENGINE_NO | NOT_A_CANDIDATE |
|---|--:|--:|--:|
| **YES** | 14 | **9** | **3** |
| **NO** | **4** | 21 | 46 |
| **NEED_MORE_CONTEXT** | 1 | 2 | 6 |

Reading it: recall of human-YES = 14 / 26 = 54 % (interval reported, n small); of the 9
misses, **7 fail `retrace_complete` by ≤ 2 bars** → a *window*, not a rule problem; of
the 3 not-surfaced cases, all involve an H4 level the trader mentioned in
`missing_information`; the 4 false positives share reason code `NO_CLEAR_LEVEL` → candidate
new primitive.  A single "agreement 79 %" would have hidden all three findings.

### B.6 Path to DETERMINISTIC (the `h1_structure_quality` gate)
Promotion `HYBRID → DETERMINISTIC` requires **replacing or removing** the gate:
after several rounds, the human's YES/NO on this gate is compared with candidate
operational predicates (e.g. swing-sequence regularity, distance between swings in ATR,
number of overlapping candles).  If a predicate's agreement with blind answers on the
*untouched* pool meets the `PromotionPolicy` **and** the disagreement analysis leaves no
unexplained cluster, a new definition without the gate is created; otherwise the strategy
stays HYBRID (that is a valid, honest end state).

---

## C. Chart example object (anchored, POSITIVE)

```jsonc
{
  "example_id": "ex_0012", "label": "POSITIVE", "partition_role": "AUTHORING",
  "evidence_kind": "ANCHORED", "direction": "LONG",
  "instrument": "XAUUSD", "timeframes": ["H1", "M15", "M5"],
  "decision_time": "2026-08-14T13:35:00Z", "visible_until": "2026-08-14T13:35:00Z",
  "hindsight_exposed": false,
  "observed_geometry": {"entry": 4311.6, "stop": 4308.9, "target": null,
                        "zones": [{"role": "SUPPORT", "low": 4310.2, "high": 4312.9, "space": "MARKET"}]},
  "anchor": {"method": "USER_ENTERED", "verification": "VERIFIED", "feed_mismatch_risk": "LOW"},
  "images": ["sha256:9f…"],
  "annotations": [{"type": "ZONE", "space": "MARKET", "origin": "HUMAN"}],
  "statements": ["st_0031"]
}
```
Measured facts for this example (H1 trend, zone touch, retracement fraction, candle
pattern) are computed by registered primitives **as-of `decision_time` only**.

---

## D. Introducing a new primitive from a Studio session

The trader says: *"I only take it when the M15 was compressed before the move."*
`AMB-0011 UNDEFINED_TERM ("compressed")`.  No registered primitive matches, so the system
opens a `PrimitiveRequest` containing the statement, three anchored examples marked
compressed and two marked not compressed, expected outputs, edge cases and the required
`known_at`.

Path taken: **composite** — `range.compression@1` = "mean high–low range of the last *k*
bars ≤ *m* × ATR", expressed as an expression graph over `volatility.range_mean@1` and
`volatility.atr@1`; no code, golden vectors from the five examples, truncation test
passes → `EXPERIMENTAL` → reviewed → `APPROVED`.  From then on any strategy can
reference `range.compression@1` in a definition; its parameters (`k`, `m`) go through the
usual ParameterSet path.  A strategy already `FROZEN` is unaffected.
