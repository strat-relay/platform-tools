# CONTEXT_STRUCTURE_RETRACE_V1 frozen specification

## Identity

- freeze: `2026-09-16T05:00:08.854816+00:00`
- source identity: `f931fe449d1bee78fde768374ad7ce88ded3f19f9afdf349e9acb47602772a0f`
- configuration hash: `1f1da2a63d69ac79e4aca21d0de33c860e76f4c33d9bd321cb50b20353114e1e`
- decision fingerprint: `70dba71d28fe8a5c09f9033b80eeb4c27a733c6c342537e03c631f41e2a1cdda`
- Phase 2 hash: `923d0d2762b6b78515a96e96dba17e42e34818aa82c406dc9ebc6f43b1a54c41`

## Frozen behavior

Setup events are bullish/bearish engulfing, morning/evening star and
rejection-wick events. Context (S/R zones, trendlines/channels, EMA20/50/100/200,
M5/M15/H1/H4 alignment) is recorded descriptively; it is not silently turned
into a profitability filter. The M15 execution/lower M5/higher H1+H4 wiring is
configuration, not symbol-specific strategy code.

A qualified setup waits for the configured 20% depth-only retracement while
recording other observed mechanisms: `DEPTH_ONLY`, `REJECTION_WICK`,
`LOWER_TF_ENGULFING`, `MORNING_EVENING_STAR`, `EMA_TOUCH_REJECTION`, and
`STRUCTURE_TOUCH_REJECTION`. Several mechanisms at one interaction remain one
entry opportunity.

The originating setup extreme is the canonical structural stop. The target is
`STRUCTURE_CAPPED_EXTENSION`: setup extreme plus 0.50 times setup range,
capped only by directionally valid opposing structure. A target at or behind
the executable entry is `NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY` and
is not opened. No minimum-R filter and no fixed time exit are frozen.

One economic decision has total 1R risk: Leg A 0.5R, Leg B 0.5R. Leg A exits
at the first target; after that Leg B moves to breakeven. The runner exit is
shadow research only. Scale-in is disabled. Pre-target leave/close-outside /
return can create a re-entry opportunity; return after target completion
cannot reuse the old setup.

Observed executable spread is recorded. Commission is
`UNKNOWN_UNRESOLVED`; slippage is telemetry/sensitivity only. This is paper
only and uses no broker order endpoint.
