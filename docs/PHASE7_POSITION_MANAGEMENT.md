# Phase 7 — Passive Position-Management Observer

Phase 7 is a separate, paper/research-only process for `CONTEXT_STRUCTURE_RETRACE_V1`.
It reads immutable V1 economic-position metadata from the Phase 6 state and
read-only M5 market data. It does not alter V1 entries, stops, targets,
qualification, exits, sizing, or state transitions.

## Boundary and identity

The Phase 7 freeze is recorded in `context_structure_retrace_phase7_manifest.json`.
Only positions filled at or after that timestamp are prospective Phase 7
evidence. Existing closed positions are `PRE_PHASE7_EXPOSED_REFERENCE`.
An existing position still open at the boundary is `LEFT_TRUNCATED_EXISTING_POSITION`
and is observed prospectively only from the boundary onward.

Phase 6 remains identified by its original source/configuration/decision and
Phase 2 hashes in the manifest. Phase 7 has its own source and configuration
hashes.

## Observation windows

Two windows are never mixed:

1. `V1_WINDOW`: executable entry through the actual V1 exit. Its MFE/MAE are
   `v1_window_mfe_R` and `v1_window_mae_R`.
2. `PHASE7_SHADOW_WINDOW`: executable entry through the fixed 180-minute
   observation horizon. Its MFE/MAE are `shadow_mfe_R` and `shadow_mae_R`.
   Movement after V1 exit is separately recorded as `post_exit_shadow_*`.

The horizon is an observation boundary, not a V1 exit rule.

## Causal measurements

Favorable thresholds are `+0.25R`, `+0.50R`, `+0.75R`, `+1R`, `+1.5R`, `+2R`,
and `+3R`; adverse thresholds are `-0.25R`, `-0.50R`, `-0.75R`, and `-1R`.
Each threshold has a first causal timestamp. Registration of a hypothesis
never counts as reaching it. Completed M5 bars are used; the current forming
bar is excluded. Long favorable/adverse sides use bid high/bid low, while
short observations use ask low/ask high, with ask derived from the observed
bar spread and broker point metadata.

R@5/10/15/30/60/90/120/180 are actual completed-bar observations and include
the timestamp used and delay from the requested checkpoint. Missing data is
not interpolated. Gap events are append-only Phase 7 events.

## Hypotheses

The observer registers, but does not rank or select, V1 target, original-stop
continuation, fixed favorable thresholds, and breakeven-after-threshold
hypotheses. Lower-timeframe trail, EMA/structure management, and opposite
price-action exit are explicitly `DEFINED_BUT_NOT_EXECUTABLE` until their
definitions are frozen independently. Commission is `UNKNOWN`; slippage is
observation-only.

The two-leg research accounting convention is one total setup risk:
Leg A = 0.5R and Leg B = 0.5R. `combined_R = 0.5 * LegA_R + 0.5 * LegB_R`.
This never creates V1 positions or scale-ins.

## Commands

```sh
python3 context_structure_retrace_phase7_observer.py freeze
python3 context_structure_retrace_phase7_observer.py start --interval 15
python3 context_structure_retrace_phase7_observer.py health
python3 context_structure_retrace_phase7_observer.py report
python3 context_structure_retrace_phase7_observer.py audit-order-isolation
python3 context_structure_retrace_phase7_observer.py stop
```

Phase 7 files are separate from Phase 6:

- `context_structure_retrace_phase7_state.json`
- `context_structure_retrace_phase7.jsonl`
- `context_structure_retrace_phase7.heartbeat.json`
- `context_structure_retrace_phase7.pid`
- `context_structure_retrace_phase7_manifest.json`

The Phase 7 observer cannot call broker order, cancel, close, or trailing-stop
primitives. Do not delete or reset its state while it is collecting.
