# KOJO_STRUCTURE_RECLAIM_V1 — Implementation Progress

**Status: COMPLETE — all tests passing**  
**Branch: codex/kojo-structure-reclaim-v1** (created from this work)

---

## STEP 0 — Exploration Findings

### postgres/migrations/
- Most recent migration: `042_strategy_mgmt_pipeline.sql` — PRESENT
- Creates `strategy_mgmt` schema with:
  - `strategy_definition` (name, family_key, unique by family_key)
  - `strategy_version` (evaluator_key, lifecycle DRAFT→IMPLEMENTED→FROZEN, immutability trigger)
  - `parameter_schema` (jsonb fields)
  - `parameter_set` (values jsonb, fingerprint, immutability trigger)
  - `backtest_run` (DISCOVERY|VALIDATION, provenance fingerprints, metrics jsonb)
  - `strategy_instance_v2` (online=false default, execution_eligible=false default, instruments jsonb)
- DB triggers enforce immutability of frozen versions/parameter_sets
- DB trigger prevents execution_eligible change through normal update path

### platform_api/strategy_mgmt.py
- `StrategyMgmtRepository`: full CRUD for all pipeline tables
- `BacktestJobRunner`: thread-pool-based async backtest runner
- `StrategyMgmtApi`: HTTP-style handler wired into PlatformControlApi
- Test injection point: `_feed_events`, `_parameter_values`, `_evaluator_key` in request body
- Routes: POST/GET strategy-definitions, strategy-versions, parameter-sets, backtests, strategy-instances

### platform_api/control.py
- `StrategyMgmtApi` wired in at line 772: checked before catch-all 404
- `strategy_mgmt_api.handle(method, path, body)` returns `None` for unowned paths

### strategy_backtest/
- `engine.py`: `BacktestEngine.run()` — StrategyVersion → ParameterSet → Feed → CostModel → BacktestResult
- `models.py`: MarketEvent, EntrySignal (LONG/SHORT geometry validation), SetupLifecycleEvent, ParameterSchema, ParameterSet, StrategyVersion
- `registry.py`: StrategyEvaluatorRegistry (resolve by evaluator_key), register_builtin_evaluators
- `feeds.py`: HistoricalMarketFeed (completed-only, deterministic ordering by open_timestamp)
- `kojo_wedge.py`: existing evaluator, pivot detection, wedge geometry, next-bar-open entry pattern
- `execution.py`: SimulatedExecution — fills MARKET at first event with open_ts >= decision_ts
- `parity.py`: assert_live_replay_parity — verifies historical vs live feed give same outputs
- `metrics.py`: calculate_metrics — win_rate, net_r, expectancy_r, profit_factor, max_dd

### strategies/
- CONTEXT_STRUCTURE_RETRACE_V1, LIQUIDITY_DISPLACEMENT_SCALP_V1, SIMPLE_SR_CANDLE_V1 — documented
- No Kojo family strategy instances in the orchestrator config
- Kojo strategies exist only as backtest research evaluators

### Existing SR/swing infrastructure
- `context_structure_retrace/sr.py`: `confirmed_swings(bars, timeframe, as_of, lookback)` — causal swing detection
  - Uses `bar_end(bar, timeframe) <= as_of` filter (close_ts must be known)
  - Requires `lookback` bars on both sides of pivot center (confirmed by look-right constraint)
- `context_structure_retrace/indicators.py`: `ema(values, period)` — exponential moving average
- `context_structure_retrace/structures.py`: trendline/channel candidates from confirmed swings
- SR algorithm is deterministic, causal, and proven in production

### Confirmation patterns (existing)
- `simple_sr_candle_validate.py`: `candle_pattern()` implements:
  - BULLISH_ENGULFING: `cl1 < o1 and cl2 > o2 and o2 <= cl1 and cl2 >= o1`
  - BEARISH_ENGULFING (mirror)
  - MORNING_STAR / EVENING_STAR
- These are the reusable pattern definitions — reused in KOJO_STRUCTURE_RECLAIM_V1

### EMA indicators available
- `context_structure_retrace/indicators.py`: `ema(values, period)` and `ema_context(completed_bars, forming, as_of, timeframe)`
- EMA 20/50/100/200 computed from H1 closes
- `ema_context()` returns slopes, distances, alignment — ready to use

### signal_orchestrator.py and runtime
- `load_adapters(config, freeze_timestamp)` reads from `StrategyRegistry(config)` — strategy list from DB/config
- Currently only handles CONTEXT_STRUCTURE_RETRACE_V1 and LIQUIDITY_DISPLACEMENT_SCALP_V1
- No adapter for Kojo family

### Research on Kojo Structure Reclaim
- `docs/KOJO_WEDGE_V1_ONBOARDING.md` — wedge onboarding, not directly applicable
- No existing research doc on KOJO_STRUCTURE_RECLAIM_V1 specifically
- Pattern semantics (break/reclaim + retest + confirmation) documented in this task

### Available data files
- XAUUSD H1: `/Users/caleb/mt5-native-bridge/platform-kojo-wedge/artifacts/research/kojo-wedge-v1/datasets/kojo_xauusd_h1_discovery.jsonl.gz`
  - Coverage: 2024-03-14 to 2026-03-25, 11,999 bars, MT5/Exness
- XAUUSD M15: **NONE** — no M15 dataset available for XAUUSD
- The discovery window (2026-07-01 to 2026-08-09) is OUTSIDE the H1 data range (ends 2026-03-25)

---

## CRITICAL FIRST CHECK — RUNTIME INTEGRATION

```
STRATEGY_INSTANCE_V2_RUNTIME_INTEGRATED=false

EXISTING_RUNTIME_INSTANCE_MODEL=
  - platform.strategy_instance (PostgreSQL): instance_id, strategy_id, display_name, enabled (ONLINE toggle)
  - Loaded by orchestration/config.py:load_instances_from_database()
  - Joined with strategy.instrument_membership for active instruments
  - Consumed by signal_orchestrator.py:load_adapters() to create ContextStructureRetraceAdapter or LiquidityInstanceAdapter

NEW_PIPELINE_INSTANCE_MODEL=
  - strategy_mgmt.strategy_instance_v2: id (UUID), strategy_version_id, parameter_set_id,
    display_name, online (default false), execution_eligible (default false, immutable via trigger),
    instruments (jsonb array), attributes (jsonb)
  - Created through pipeline API (POST /api/v1/strategy-instances)
  - Starts OFFLINE, execution_ineligible

RUNTIME_BRIDGE_PATH=
  - NO direct bridge exists yet
  - load_instances_from_database() only reads platform.strategy_instance
  - load_adapters() has no KOJO_STRUCTURE_RECLAIM_V1 branch
  - Implementation added:
    1. orchestration/config.py: load_v2_instances_from_database() reads strategy_mgmt.strategy_instance_v2
       for ONLINE instances, maps to the same dict format as platform.strategy_instance rows
    2. orchestration/adapters/kojo_structure_reclaim_adapter.py: adapter class
    3. signal_orchestrator.py: load_adapters() branch for KOJO_STRUCTURE_RECLAIM_V1
```

---

## Data Window

```
DISCOVERY_START=2026-07-01
DISCOVERY_END=2026-08-09

VALIDATION_START=2026-08-10
VALIDATION_END=2026-08-30

NEW_DATA_REQUIRED=true
REASON_H1=H1 data ends 2026-03-25; discovery window starts 2026-07-01
REASON_M15=No XAUUSD M15 dataset exists anywhere in the repo
DATASET_NEEDED=MT5/Exness XAUUSDm H1 and M15 bars, 2026-07-01 to 2026-08-30
```

Because real data is unavailable, the acceptance-test backtest uses synthetic fixture events.
The synthetic dataset is clearly labeled and fingerprinted. Results from synthetic data are
explicitly NOT performance claims.

---

## Implementation Steps

- [x] STEP 0: Exploration (this doc)
- [x] strategy_backtest/kojo_structure_reclaim.py — evaluator
- [x] strategy_backtest/registry.py — register kojo_structure_reclaim
- [x] strategy_backtest/__init__.py — export
- [x] orchestration/adapters/kojo_structure_reclaim_adapter.py — runtime bridge
- [x] orchestration/config.py — load_v2_instances_from_database
- [x] signal_orchestrator.py — adapter loading branch
- [x] tests/test_kojo_structure_reclaim.py — all tests
- [x] platform_api/strategy_mgmt.py — fixed _build_compute_fn _strategy_version_id injection

---

## Test Results (2026-10-07)

```
SEMANTIC_FIXTURE_COUNT=3
SEMANTIC_FIDELITY_GATE_PASS=true (Fixture1 MATCH, Fixture2 MATCH, Fixture3 MATCH)

NO_LOOKAHEAD_PASS=true
PREFIX_INVARIANCE_PASS=true
DETERMINISTIC_RERUN_PASS=true
HISTORICAL_LIVE_EVALUATOR_PARITY_PASS=true
CHECKPOINT_RESTART_PARITY_PASS=true
TERMINAL_REACTIVATION_GUARD_PASS=true
DUPLICATE_ECONOMIC_OPPORTUNITY_GUARD_PASS=true
ACTUAL_FILL_GEOMETRY_PASS=true

PIPELINE_ACCEPTANCE_PASS=true (all 11 steps, in-memory mock)

TOTAL_TESTS=25
PASSED=25
FAILED=0
```

---

## Safety Checks (invariants throughout)

```
KOJO_WEDGE_CHANGED=false
KOJO_STRUCTURE_FIB_CHANGED=false
KOJO_LIQUIDITY_SWEEP_CHANGED=false
CONTEXT_CHANGED=false
LIQUIDITY_CHANGED=false
PRODUCTION_CHANGED=false
EXECUTION_AUTHORITY_CHANGED=false
RISK_LIMITS_CHANGED=false
ROUTES_CHANGED=false
BROKER_WRITES=0
```
