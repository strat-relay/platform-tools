# Intraday evidence and data readiness

This record closes the repository search from base commit `2e469d0` without
running discovery, validation, optimization, or production activation.

## Existing raw data recovered

The earlier empty eligibility lists meant “not yet proven under the raw
adapter contract,” not “no data exists.” Two sibling-checkout exports were
found:

- `multitimeframe_structure_sniper/paged_native_full`: native MT5 M5 OHLC for
  EURUSD, GBPUSD, and USDJPY, with recorded bar spreads and deterministic
  derivability of M15/H1/H4. The long windows are monotonic, unique, and OHLC
  valid; unexplained gaps are recorded and never filled.
- `multitimeframe_liquidity_sniper/expanded_historical_dataset.json`: native
  MT5 M5 OHLC for EURUSDm, GBPUSDm, and USDJPYm, with per-bar spread and
  contract metadata. EURUSD/GBPUSD are fixture-sized windows; USDJPY has 54
  unexplained gap intervals.

The canonical instrument remains provider-neutral. The `m` suffix occurs only
in the provider-symbol field.

## Bounded real-data invariants

Read-only bounded windows were exercised through the registered raw adapters:

- Context: 240 EURUSD M5 bars from the long native export.
- Liquidity: 700 USDJPY M5 bars from the expanded export.

Both passed deterministic rerun, prefix/restart state identity, and
historical/live replay parity. These are contract/invariant checks only; no
performance metrics or discovery result was produced.

## Parent parity status

Parent parity remains blocked. Context phase-3/phase-5 artifacts contain
observed parent outputs but the raw source window used to produce them is not
recoverable in the current evidence set. Liquidity has raw M5 plus cost
metadata, but the intraday variant deliberately changes the hold/target
research hypotheses, so those fields cannot be used as a parent-parity pass.
No expected lifecycle values were fabricated from the adapter.

The Liquidity scan change in `2e469d0` is consistent with the parent source:
the parent `find_candidate` searches up to five completed future M5 bars for
displacement/MSS after the sweep. The raw adapter waits until that same causal
window exists before marking a sweep index resolved. No semantic redefinition
was made.

## Remaining blockers

1. Recover or reacquire an authoritative Context raw M5 window alongside its
   parent replay output.
2. Approve a gap-exclusion policy and complete Context contract metadata.
3. Obtain a longer, gap-qualified Liquidity M5 dataset for discovery; the
   existing EURUSD/GBPUSD windows are fixtures and USDJPY has unexplained gaps.
4. Freeze separate parent-parity comparisons for intentionally changed
   intraday target/hold hypotheses.

```text
DISCOVERY_RUN=false
PERFORMANCE_METRICS_ACCESSED=false
UNTOUCHED_VALIDATION_ACCESSED=false
PARAMETER_OPTIMIZATION=false
PARENT_STRATEGIES_CHANGED=false
INTRADAY_STRATEGY_SEMANTICS_CHANGED=false
NEW_INSTANCES_ONLINE=false
NEW_INSTANCES_EXECUTION_ELIGIBLE=false
AUTHORITY_CHANGED=false
RISK_LIMITS_CHANGED=false
PRODUCTION_CHANGED=false
BROKER_WRITES=0
```
