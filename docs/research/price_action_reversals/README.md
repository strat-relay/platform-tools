# Price Action Reversal V1 definitions

This directory freezes the three additional Price Action definitions requested
for research only:

- Engulfing Reversal V1
- Morning/Evening Star Reversal V1
- Rejection / Pin-Bar Reversal V1

They use completed H1 context and completed M15 execution candles, confirmed
H1 swings/levels, a strict prior directional move before the pattern starts,
a true candle/zone intersection with directional rejection, a full-pattern
extreme stop plus buffer, the nearest known opposing target, and the exact
next-M15-open paper-entry observation. A proposal is never an
execution signal: all outputs are `RESEARCH`, `research_only=true`, and
`execution_eligible=false`.

M15 events are buffered behind an explicit per-instrument H1 event-time
watermark. `finalize_context()` either releases evaluations once the cutoff is
covered or records a timeout; it never silently revises an emitted decision.
Next-open results are completed-candle reconstructions: the reference open is
known, while data availability and processing observation timestamps remain
unknown unless supplied by provenance.

The definitions deliberately do not modify the frozen Support/Resistance
Retest V1 or Engulfing Continuation V1 evaluators, and do not register an
active instance or change deployment state.

Historical replay is not claimed by this change. The prior audited dataset
manifest is present, but its referenced raw source root is not present in this
checkout. `readiness_report.json` records the exact acquisition blocker; the
synthetic fixture tests are only lifecycle and detector checks, not performance
evidence.

The implementation is in `strategy_backtest/price_action_*_reversal.py` and
the shared causal lifecycle is in
`strategy_backtest/price_action_reversal_common.py`.
