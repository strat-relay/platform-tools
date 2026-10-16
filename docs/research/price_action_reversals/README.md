# Price Action Reversal V1 definitions

This directory freezes the three additional Price Action definitions requested
for research only:

- Engulfing Reversal V1
- Morning/Evening Star Reversal V1
- Rejection / Pin-Bar Reversal V1

They use completed H1 context and completed M15 execution candles, confirmed
H1 swings/levels, a strict prior directional move, a structural pattern
location, a pattern-extreme stop plus buffer, the nearest known opposing
target, and a next-M15-open paper-entry observation. A proposal is never an
execution signal: all outputs are `RESEARCH`, `research_only=true`, and
`execution_eligible=false`.

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
