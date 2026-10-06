# Context Structure Retrace V1/V2 investigation

## Scope

V1 remains frozen. This document records the live/research split and the additive V2
research experiment; it is not a production routing change.

## V1 implementation and callers

The canonical V1 decision implementation is `context_structure_retrace_forward.py`. Its
startup guard verifies the frozen configuration hash and decision-code fingerprint.

The live path is:

`context_structure_retrace_forward.py` -> compact runtime state ->
`orchestration.adapters.context_structure_retrace.ContextStructureRetraceAdapter` ->
`signal_orchestrator.py` -> canonical signal publisher.

The research raw-OHLC path is `strategy_backtest/raw_ohlc_adapters.py`. It imports V1's
`_geometry`, `_m5_mechanisms`, and `make_setup`, but implements a separate event/state loop.
It therefore does not prove complete live/backtest parity: setup scanning, retrace expiry,
re-entry handling, gap recovery, and publication are not the same caller path.

The older `context_structure_retrace/` package is a research feature/snapshot foundation;
it is not the production forward runner.

## Known parity differences

| Area | Live V1 | Raw research adapter |
|---|---|---|
| Base data | Redis canonical cache, bounded read (`limit=320`) | supplied completed `MarketEvent` feed |
| Completion rule | drops the newest returned bar; acts on completed M5/M15 bars | completed event contract; derives higher timeframes causally |
| Setup scan | scans newly completed M15 bars since `last_m15` | scans on M15 event boundaries in its own state |
| State recovery | persists state, detects M5 gaps, applies recovery lineage | restorable in-memory evaluator state; no live gap-recovery policy |
| Retrace lifecycle | 12 M5 candles, invalidation, re-entry, scale-in disabled | separate research setup loop and expiry parameter |
| Publication | adapter consumes compact state and publisher writes canonical signals | evaluator emits research `EntrySignal` objects |
| V1 target | `_geometry` single effective structural target | shared `_geometry` target helper |

The first causal divergence must be measured with a dated market-data snapshot. A read-only
replay was run against the live Redis M5 cache on 2026-10-06. The cache covered 400 completed
bars per instrument, from approximately 2026-10-05 00:40/01:40 UTC through 2026-10-06
10:55 UTC. The replay was grouped by instrument so state was not incorrectly shared between
symbols. This is evidence for the research adapter, not proof of exact live publisher parity;
the persisted live compact state and publication ledger were not exported.

| Broker symbol | V1 setups | V1 signals | V2 setups | V2 signals | V2 signal delta |
|---|---:|---:|---:|---:|---:|
| BTCUSDm | 98 | 24 | 98 | 20 | -4 |
| EURUSDm | 79 | 24 | 79 | 18 | -6 |
| GBPUSDm | 96 | 29 | 96 | 22 | -7 |
| XAUUSDm | 73 | 25 | 73 | 22 | -3 |
| **Total** | **346** | **102** | **346** | **82** | **-20** |

The observed first divergence is therefore the V2 target-eligibility decision: setup detection
is unchanged, while 20 V1 entry candidates are rejected with `RR_BELOW_MINIMUM` because no
existing direction-valid structural target reaches 1.0 planned R. The run deliberately did not
fabricate a farther target or alter V1 stop/entry logic. The canonical API returned 116 V1
signals for the same broad date query, but that endpoint is paginated and includes records outside
the exact 400-bar cache slice; it must not be used as a one-to-one parity count without matching
the immutable input and publication boundaries.

## V2 contract

V2 uses the V1 setup/stop/structural-candidate model in a research-only evaluator. It adds only
the `MIN_PLANNED_R = 1.0` eligibility rule. It does not manufacture a target. If the nearest V1
structural target is below 1R but a farther existing structural candidate is at least 1R, V2
selects that farther candidate. If no candidate qualifies, it emits the diagnostic lifecycle
reason `RR_BELOW_MINIMUM` and no signal.

V2 is registered only in the research backtest registry. It is not added to production strategy
routing, execution policy, the live orchestrator, or broker-enabled configuration.

## Remaining parity limitation

For exact live/backtest parity, export the same immutable M5/M15/H1/H4 snapshot plus the
forward runner's compact state and canonical publication records, then run
`research/context_structure_retrace_parity.py`. The tool records the dataset fingerprint,
timestamp/instrument rows, first divergence, and V1/V2 counts. No count should be normalized or
tuned to match. Until that export exists, the table above is the strongest reproducible
research result and must be labelled `research_adapter_replay`, not `live_parity`.
