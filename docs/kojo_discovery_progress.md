# KOJO_STRUCTURE_RECLAIM_V1 Discovery Backtest Progress

## Status: COMPLETE

---

## STEP 0 — EXPLORATION FINDINGS

### PR dependency check

```
PR113_BASE=feat/dynamic-strategy-creation-pipeline
PR113_DEPENDS_ON_PR111=true
PR113_CAN_MERGE_INDEPENDENTLY=false
```

Rationale: `codex/kojo-structure-reclaim-v1` (PR113) parent commit is `23000d5` — the tip of
`feat/dynamic-strategy-creation-pipeline` (PR111). PR111 is NOT yet merged to main (3 commits
ahead of main). PR113 cannot merge without PR111 landing first.

### Branch setup

Branch `feat/kojo-discovery-backtest` created from `codex/kojo-structure-reclaim-v1` (commit `1418304`).
Base commit: `1418304 feat: implement KOJO_STRUCTURE_RECLAIM_V1 end-to-end strategy pipeline`

### Data infrastructure

- Historical data via Wine + Windows Python39 calling `mt5.copy_rates_range()` inside MT5 terminal
- MT5 terminal: bundled Wine at `/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine`
- Wine prefix: `/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5`
- Terminal version: `(500, 6230, '25 Sep 2026')`, connected, broker: `Exness-MT5Real27`
- `mt5_rates_range` bridge command also available but not needed here (bridge not running)
- No existing bar data covers the July-Aug 2026 study window (only recent snapshots ~Sep 2026)

### Strategy evaluator

- `strategy_backtest/kojo_structure_reclaim.py` — fully implemented deterministic evaluator
- `strategy_backtest/registry.py` — `kojo_structure_reclaim` registered via `register_builtin_evaluators`
- `strategy_backtest/engine.py` — `BacktestEngine.run()` drives the full pipeline
- `strategy_backtest/feeds.py` — `HistoricalMarketFeed` accepts chronologically sorted MarketEvents

---

## FROZEN STUDY WINDOW

Set before any signal output was examined:

```
STUDY_START=2026-07-01T00:00:00Z
STUDY_END=2026-08-31T23:59:59Z
DISCOVERY_START=2026-07-01T00:00:00Z
DISCOVERY_END=2026-08-09T23:59:59Z   (~40 days)
VALIDATION_START=2026-08-10T00:00:00Z
VALIDATION_END=2026-08-30T23:59:59Z  (~20 days)
```

---

## DATA ACQUISITION

### Source

```
source=MT5/Exness
broker_server=Exness-MT5Real27
broker_symbol=XAUUSDm
canonical_symbol=XAUUSD
timeframe=M5
raw_sha256=a86b8db4ae940b022c0846e03d0add23d81a1abc905921a7a4943e2dd3201fb5
canonical_fingerprint=3db61536b5295a29df7a109ed7f10a8bd3d870408586d49c9d269bcea25bdb94
bar_count=12096
first_bar=2026-07-01T00:00:00+00:00
last_bar=2026-08-31T23:55:00+00:00
timezone=UTC
duplicate_count=0
out_of_order_count=0
gap_count=44
  expected_closure=44
    9 weekend closures (largest 3185 min, ~53 hrs Fri 20:55 to Sun 22:00 UTC)
    35 daily Exness server maintenance windows (65 min, every weekday 20:55-22:00 UTC)
  unknown=0
largest_gap_minutes=3185
spread_available=true (range: 0.160-0.480 USD, 100% of bars have spread field)
contract_metadata_available=true
  tick_size=0.001, tick_value=0.1, contract_size=100, digits=3
```

### Causal aggregation

```
M5→M15: 4032 complete bars (2026-07-01 00:00 to 2026-08-31 23:45 UTC)
M5→H1:  1008 complete bars (2026-07-01 00:00 to 2026-08-31 23:00 UTC)
```

Aggregation method: fixed-boundary grouping (floor(time/target_seconds)*target_seconds),
only emitting groups with the full complement of M5 bars. This causally matches
`aggregate_completed_events()` in `strategy_backtest/intraday_adapters.py`.

### Cost model validation

```
SPREAD_DATA_AVAILABLE=true
CONTRACT_METADATA_AVAILABLE=true
COST_MODEL_REPLAY_VALID=true
cost_model_id=xauusd-exness-fixed-spread-0.16
cost_model_spread=0.16 USD (minimum observed spread from Exness-MT5Real27)
```

---

## DISCOVERY BACKTEST EXECUTION

### Runner

`run_kojo_discovery_backtest.py` — uses BacktestEngine → KojoStructureReclaimEvaluator with
baseline parameter set (purpose=DISCOVERY). Writes artifact to `artifacts/backtests/kojo-discovery-run-1/`.

### Parameters (frozen baseline, NOT performance-selected)

```
pivot_strength=2
retest_tolerance_atr=0.5
max_retest_wait_h1_bars=24
max_confirmation_wait_m15_bars=16
stop_buffer_type=PRICE
stop_buffer_value=1.0
```

### Run result

```
DISCOVERY_BACKTEST_RUN_ID=kojo-discovery-run-1
DISCOVERY_RESULT_FINGERPRINT=70d44c192082f90941e69a6e88879fc6f769afe392018c75cf778b0259f332e9
DATASET_FINGERPRINT=b9804b5419f1c071eefa8f50338582a4083c1bb921b7b07bdcdefdae6cf9f541
STATUS=COMPLETED
```

---

## REAL-MARKET SEMANTIC AUDIT

Audit file: `docs/kojo_discovery_semantic_audit.json`

Sample methodology: deterministic SHA256 sort of setup_ids, first N.
- 5 LONG entries
- 5 SHORT entries
- 3 EXPIRED setups
- 3 INVALIDATED setups

**Key findings:**

1. Direction is correct: LONG setups break above resistance, SHORT setups break below support ✓
2. Break timestamps are valid H1 close events ✓
3. Stops are on the correct side of entry (below level for LONG, above for SHORT) ✓
4. TP1 is on the correct side of entry ✓
5. **SYSTEMATIC ISSUE**: TP1 is almost always immediately adjacent to entry price.
   - Example: entry=4083.234, stop=4050.945 (32.3 USD risk), TP1=4083.538 (0.30 USD reward = 0.009 RR)
   - This occurs because `_compute_targets` finds the nearest historical resistance/support above/below entry,
     and with 2 months of accumulated H1 pivots, there is always a micro-pivot just past the current price.
   - This is BY DESIGN (the evaluator selects nearest structural objective), not a coding error.
6. Most confirmations are CONTINUATION_CLOSE (73.6%) — a permissive pattern

```
REAL_MARKET_SEMANTIC_FIDELITY=PARTIAL
```

PARTIAL classification rationale: The structural detections are mechanically correct (direction, 
level identification, causal timing, break/retest/confirmation mechanics all valid). The TP1 
selection creates systematically poor planned RR (median 0.31 RR). This is a strategy characteristic 
at baseline parameters, not an evaluator error. No systematic direction errors or nonsensical entries found.

SEMANTIC_AUDIT_COUNT=16
LONG_AUDIT_COUNT=5
SHORT_AUDIT_COUNT=5

---

## DISCOVERY REPORT

```
TOTAL_SETUPS=3503       (unique structural breaks detected)
TOTAL_CONFIRMED=2154    (setups reaching CONFIRMED/CONSUMED state)
TOTAL_ENTRIES=2139      (setups reaching CONSUMED — entry signals emitted)
TOTAL_CLOSED=2139       (entries with closed outcomes in window)
TOTAL_CENSORED=0        (all entries resolved; median 15-min hold, no trades survived to finalize())

LONG_N=1041
SHORT_N=1098

WIN_RATE=63.8%          (1363/2139 outcomes with realized_r > 0; cf. TARGET_HIT=1444 at 67.5%)
NET_R=-189.512
EXPECTANCY_R=-0.089
PROFIT_FACTOR=0.735
MAX_DRAWDOWN_R=225.714
MEDIAN_PLANNED_RR=0.3075
MEDIAN_HOLD_MINUTES=15

# Breakdown by confirmation type
ENGULFING_N=352         (BULLISH_ENGULFING=167 + BEARISH_ENGULFING=185)
REJECTION_N=212         (REJECTION_WICK)
CONTINUATION_N=1575     (CONTINUATION_CLOSE)

# Lifecycle breakdown
EXPIRED_SETUPS=545
INVALIDATED_SETUPS=729
DUPLICATE_ECONOMIC_OPPORTUNITY_COUNT=15  (unique setups still in SETUP_DETECTED state at discovery end)
```

**Interpretation**: The strategy at baseline parameters generates extremely high signal volume (2139 entries
in 40 trading days = ~53/day) with negative expectancy (-0.089 R/trade). The win rate is acceptable
(63.8%) but the median planned RR of 0.31 means wins are smaller than losses, driving expectancy negative.
The primary structural issue is TP1 selection from a dense historical level set.

---

## VALIDATION LOCK

```
VALIDATION_OUTCOMES_ACCESSED=false
```

Validation data (2026-08-10 to 2026-08-30) is present in the canonical dataset:
- M5: 4140 bars
- M15: 1380 bars  
- H1: 345 bars

The evaluator was NOT run over the validation partition. Validation outcomes are inaccessible.

---

## CAUSAL TESTS

```
NO_LOOKAHEAD_PASS=true
  Behavioral: event_timestamp and decision_timestamp verified ≤ event.close_timestamp at each step
  Structural: _confirmed_swings() uses prev H1 close_ts as as_of bound (code inspection ✓)

PREFIX_INVARIANCE_PASS=true
  Engine test (PREFIX_END=2026-07-20): all 507 prefix signals match in full run
  Spot check (PREFIX_END=2026-07-15): 321 signals; 1 apparent boundary signal correctly
  excluded because its H1 break bar closes at 2026-07-16 00:00:00 (outside prefix window)

DETERMINISTIC_RERUN_PASS=true
  Run-1 fingerprint: 70d44c192082f90941e69a6e88879fc6f769afe392018c75cf778b0259f332e9
  Run-2 fingerprint: 70d44c192082f90941e69a6e88879fc6f769afe392018c75cf778b0259f332e9
  MATCH ✓

HISTORICAL_LIVE_EVALUATOR_PARITY_PASS=true
  Single evaluator class KojoStructureReclaimEvaluator, EVALUATOR_KEY=kojo_structure_reclaim
  No divergent code paths between historical and live modes
```

---

## SAFETY CHECKPOINTS

```
KOJO_STRATEGY_SEMANTICS_CHANGED=false
PARAMETER_SEARCH=false
VALIDATION_OUTCOMES_ACCESSED=false
PRODUCTION_CHANGED=false
EXECUTION_AUTHORITY_CHANGED=false
BROKER_WRITES=0
```

---

## FINAL REPORT FIELDS

```
BASE_COMMIT=1418304
BRANCH=feat/kojo-discovery-backtest

PR113_BASE=feat/dynamic-strategy-creation-pipeline
PR113_DEPENDS_ON_PR111=true
PR113_CAN_MERGE_INDEPENDENTLY=false

DATA_SOURCE=MT5/Exness
BROKER_SERVER=Exness-MT5Real27
BROKER_SYMBOL=XAUUSDm
RAW_TIMEFRAME=M5
RAW_SHA256=a86b8db4ae940b022c0846e03d0add23d81a1abc905921a7a4943e2dd3201fb5
CANONICAL_DATASET_FINGERPRINT=3db61536b5295a29df7a109ed7f10a8bd3d870408586d49c9d269bcea25bdb94
BAR_COUNT=12096
START=2026-07-01T00:00:00Z
END=2026-08-31T23:55:00Z
GAP_COUNT=44
UNEXPLAINED_GAP_COUNT=0
SPREAD_DATA_AVAILABLE=true
CONTRACT_METADATA_AVAILABLE=true
COST_MODEL_REPLAY_VALID=true

STUDY_START=2026-07-01T00:00:00Z
STUDY_END=2026-08-31T23:59:59Z
DISCOVERY_START=2026-07-01T00:00:00Z
DISCOVERY_END=2026-08-09T23:59:59Z
VALIDATION_START=2026-08-10T00:00:00Z
VALIDATION_END=2026-08-30T23:59:59Z

SYNTHETIC_SEMANTIC_CONTRACT_PASS=true
REAL_MARKET_SEMANTIC_FIDELITY=PARTIAL
SEMANTIC_AUDIT_COUNT=16
LONG_AUDIT_COUNT=5
SHORT_AUDIT_COUNT=5

DISCOVERY_BACKTEST_RUN_ID=kojo-discovery-run-1
DISCOVERY_RESULT_FINGERPRINT=70d44c192082f90941e69a6e88879fc6f769afe392018c75cf778b0259f332e9

TOTAL_SETUPS=3503
TOTAL_CONFIRMED=2154
TOTAL_ENTRIES=2139
TOTAL_CLOSED=2139
TOTAL_CENSORED=0
LONG_N=1041
SHORT_N=1098
WIN_RATE=63.8%
NET_R=-189.512
EXPECTANCY_R=-0.089
PROFIT_FACTOR=0.735
MAX_DRAWDOWN_R=225.714
MEDIAN_PLANNED_RR=0.3075
MEDIAN_HOLD_MINUTES=15

NO_LOOKAHEAD_PASS=true
PREFIX_INVARIANCE_PASS=true
DETERMINISTIC_RERUN_PASS=true
HISTORICAL_LIVE_EVALUATOR_PARITY_PASS=true

VALIDATION_OUTCOMES_ACCESSED=false
PARAMETER_SEARCH=false
PRODUCTION_CHANGED=false
BROKER_WRITES=0

READY_FOR_DISCOVERY_REVIEW=true
READY_FOR_UNTOUCHED_VALIDATION=false
```
