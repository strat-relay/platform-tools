# Results registry

This is the canonical index of research, historical backtests, paper-forward
records and prospective observations found in the repository. It is an index,
not a strategy selector. The machine-readable counterpart is
`artifacts/results/results_registry.json`; the complete path-level catalog is
`artifacts/results/artifact_catalog.json`.

## Evidence classifications

- `PROSPECTIVE_PAPER`: collected after the frozen boundary without broker orders.
- `EXPOSED_DEVELOPMENT_ONLY`: corrected historical development data that has
  already influenced research.
- `HISTORICAL_DISCOVERY`: exploratory historical research.
- `HISTORICAL_VALIDATION_EXPOSED`: a later historical split that was already
  inspected and is not untouched validation.
- `RESEARCH_ONLY`: descriptive or non-deployed analysis.
- `SUPERSEDED_GEOMETRY_BUG`: retained for provenance; target direction or
  absolute-distance geometry was later corrected.
- `INVALID / DO NOT USE`: contaminated or otherwise unsafe artifact.
- `LIVE_REAL_MONEY`: none found in this repository.

## Current prospective experiment

| Experiment | Strategy/version | Symbols | Timeframes | Sample at registry time | Classification | Source |
|---|---|---|---|---:|---|---|
| `context_v1_prospective_current` | CONTEXT_STRUCTURE_RETRACE_V1 | XAUUSDm, BTCUSDm, USDJPYm, EURUSDm | M15 / M5 / H1 / H4 | 5 setups, 1 opportunity, 1 open paper position, 0 closed | `PROSPECTIVE_PAPER` | root forward state/ledger/manifest |

This sample is too small for performance inference. Its prospective boundary is
`2026-09-16T05:00:08.854816+00:00`.

## Context Structure Retrace corrected results

These are corrected Phase 5C descriptive results, not untouched validation.

| Symbol | Economic positions | Directionally valid | Wins | Losses | Time exits | Target/win rate | PF | Expectancy R | Cum R | Max DD R | Median target R | Behind entry | At entry | Valid target <0.25R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| XAUUSDm | 5014 | 4062 | 3445 | 617 | 74 | 84.81% | 5.239 | +0.621 | 2523.93 | 17.00 | 0.698 | 952 | 0 | 703 |
| BTCUSDm | 8405 | 6823 | 5770 | 1053 | 148 | 84.54% | 4.471 | +0.498 | 3400.22 | 14.00 | 0.600 | 1582 | 0 | 1391 |
| USDJPYm | 7691 | 5990 | 5034 | 956 | 196 | 84.02% | 3.727 | +0.372 | 2229.62 | 13.52 | 0.484 | 1691 | 10 | 1518 |
| EURUSDm | 6868 | 5197 | 4436 | 761 | 131 | 85.34% | 3.996 | +0.382 | 1988.34 | 18.21 | 0.483 | 1656 | 15 | 1380 |

The supplied invalid-target counts and valid `<0.25R` counts match the Phase
5C artifact exactly. These positions include descriptive economic replay and
remain `EXPOSED_DEVELOPMENT_ONLY`. They are not expected live performance.

### Superseded Context metrics

Phase 3/4/early Phase 5 results remain indexed in the JSON registry and phase
summaries. Any performance number generated before the signed target-direction
correction is `SUPERSEDED_GEOMETRY_BUG`: opposing structure could be behind the
entry and `abs(target-entry)` could mask the sign. They must not be compared as
current evidence.

Phase 3 also reported 5,521/9,185/8,572/7,603 entry attempts for
XAU/BTC/USDJPY/EURUSD, but those were not independent economic positions.
Phase 4 documented signal overlap and economic-position normalization.

## Liquidity Displacement historical results

### Original XAUUSDm V1

Artifact `liquidity_displacement_results.json` contains 613 setups and 260
filled trades for the 1.25R target, with 59.73% win rate, PF 1.854,
expectancy +0.330R, cumulative +85.74R and max DD 6.07R. Its later split has
144 setups / 72 fills and +0.222R, PF 1.473, max DD 4.04R. This is
`HISTORICAL_VALIDATION_EXPOSED`, not untouched validation.

### Five-candle V1 multi-instrument replay

| Symbol | Setups | Fills | Fill rate | Win rate | PF | Expectancy R | Cum R | Max DD R | Median duration |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| EURUSDm | 677 | 397 | 58.6% | 52.0% | 1.353 | +0.176 | 69.74 | 16.63 | 55m |
| GBPUSDm | 691 | 383 | 55.4% | 52.9% | 1.402 | +0.148 | 56.66 | 13.77 | 60m |
| USDJPYm | 577 | 322 | 55.8% | 56.4% | 1.616 | +0.232 | 74.73 | 11.17 | 65m |
| USTEC_x100m | 705 | 376 | 53.3% | 52.1% | 1.360 | +0.205 | 76.95 | 15.10 | 40m |
| USTECm | 705 | 376 | 53.3% | 52.1% | 1.360 | +0.205 | 76.95 | 15.10 | 40m |

These are historical instrument studies, not the active Context V1 cohort.

### Shallow-entry variants

The entry-depth registry records 25%, 33% and 50% controls for XAUUSDm,
USDJPYm and EURUSDm. The USDJPY 25% OOS artifact records 121 validation fills,
PF 2.560 and +0.302R; the XAUUSDm 33% artifact records 143 fills, PF 1.341
and +0.147R. Both periods were exposed and are
`HISTORICAL_VALIDATION_EXPOSED`. The BTC25 overlap audit records 800 candidate
records, 535 candidate fills, 72 overlap groups and 160 overlapping candidates;
these are not independent trades.

The BTC overlap event (three nested sweeps, one displacement/MSS, one fill)
is indexed separately. Its stop alternatives are hypotheses attached to one
opportunity, not three realized exposures.

## Raw-structure study

The outcome-first raw structure study contains 35,843 M5 observations:

| Split | N | Baseline normalized six-candle movement |
|---|---:|---:|
| Discovery | 20,879 | -0.059 |
| Later split | 14,964 | +0.024 |

P01: discovery N 2,993, expected movement -0.067; later N 2,110, +0.020.
P06: discovery N 1,946, -0.048; later N 1,424, +0.174. These were broad
candidate families, not frozen definitions; P06's apparent later lift was not
accepted as robust without a new prospective freeze. Classification:
`RESEARCH_ONLY`.

## Other research families

The registry also indexes the complete artifact catalog for:

- MICRO_SCALP raw edge, filter and canonical-setups studies; archived,
  approximately break-even raw edge and weak out-of-sample filtering.
- SIMPLE_SR_CANDLE_V1 and named-candle variants; historical research only.
- engulfing continuation, prior-rally/engulfing and named-pattern studies;
  research only.
- baseline/backtest and paper-engine evidence.
- Context Phase 1/2 descriptive ledgers and cross-instrument sanity results.
- archived LIQUIDITY_DISPLACEMENT_V2 and contaminated historical copies.

No artifact is classified `LIVE_REAL_MONEY`. The presence of live-capable
bridge primitives is not evidence that any result used broker execution.

## Artifact organization

The logical indexes under `artifacts/results/` point to existing files without
moving active runtime paths:

- `artifacts/results/context_structure_retrace/`
- `artifacts/results/liquidity_displacement/`
- `artifacts/results/raw_structure/`
- `artifacts/results/prospective/`
- `artifacts/results/superseded/`

Large generated datasets remain at their original paths with checksums in the
machine-readable registry. They should be copied to backup storage rather than
committed blindly.

## Phase 7 observer

`CONTEXT_STRUCTURE_RETRACE_V1_PHASE7_OBSERVER` is a separate prospective
management/path observer, classified `PROSPECTIVE_PAPER` only for observations
after its manifest freeze. It reads Phase 6 economic positions but does not
alter them. Threshold registrations are not results; only causal post-entry
threshold events count. Pre-freeze references are excluded from prospective
totals.
