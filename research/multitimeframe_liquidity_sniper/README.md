# MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH

Research-only, offline strategy family. It is not imported by any production
runner and has no MT5, bridge, orchestration, execution, or broker-write path.

The causal hierarchy is:

`completed H4 context -> completed H1 location -> completed M15 setup -> completed M5 sniper trigger`

An M5 trigger is retained as Control A, but only the higher-timeframe controls
can form a conditioned candidate. Continuation and reversal populations must be
kept separate by the analysis/reporting layer.

## Research protocol

1. Discovery: descriptive H4/H1 context families with neutral M15/M5 controls.
2. Selection: a small predeclared M15 finalist set, then M5 execution finalists.
3. Final untouched test: frozen parameters only; never used for ranking.
4. Walk-forward and equal-pair/leave-one-pair-out generalization.

Every candidate records the source timestamp of the latest completed H4, H1,
M15, and M5 candles. Fill-cost diagnostics require spread timestamp equal to
fill timestamp and return `FILL_COST_UNAVAILABLE` otherwise. No sweep-time
spread fallback is permitted.

The module exposes the parameter space and deterministic primitives in
`engine.py`; callers provide offline candle/fill fixtures and persist reports
where appropriate. This package does not write files by itself.
