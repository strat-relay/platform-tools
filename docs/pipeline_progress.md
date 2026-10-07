# Dynamic Strategy Creation Pipeline — Progress

## Exploration findings

### postgres/migrations/ (last migration: 041)
- `platform` schema already has: `strategy_definition` (028), `strategy_instance` (030),
  `strategy_version_manifest` (034), `strategy_instance_parameter_set` (034)
- `strategy` schema has: `entry_signals`, `entry_signal_outcomes`, `instrument_membership`
- Existing `platform.strategy_definition` has `strategy_version` as a plain text field (not FK)
  and lacks lifecycle/frozen/immutable concepts
- Existing `platform.strategy_instance` has `enabled` bool (the ONLINE toggle) and `attributes` jsonb
- Migration 034 has `strategy_version_manifest` and `strategy_instance_parameter_set` — these are for
  publishing manifests from code, NOT for the new creation pipeline

### platform_api/
- `control.py` (1104 lines): central WSGI-style router, handles GET/POST/PATCH for all routes
- `strategy_catalog.py`: StrategyCatalogRepository, strategy page model, instance ONLINE/OFFLINE writes
- No existing `/api/v1/strategy-definitions`, `/api/v1/strategy-versions`, `/api/v1/parameter-sets`,
  `/api/v1/backtests`, `/api/v1/strategy-instances` (new pipeline) routes yet
- ONLINE toggle exists at POST /api/v1/strategies/{id}/instances/{iid}/lifecycle
- Execution authority is completely separate (execution_authority.py)

### strategy_backtest/
- `engine.py`: BacktestEngine.run() — takes StrategyVersion, ParameterSet, feed, CostModel
- `models.py`: BacktestRun, StrategyVersion, ParameterSet, ParameterSchema, EntrySignal, etc.
- `registry.py`: StrategyEvaluatorRegistry, ParameterSetCatalog
- Same evaluator Protocol used for both backtest and live: `consume_market_event(event)` returns `tuple[SetupLifecycleEvent | EntrySignal, ...]`
- `BacktestRun.status` transitions: QUEUED → RUNNING → COMPLETED | FAILED | CANCELLED
- Already has `result_fingerprint`, `dataset_fingerprint`, `cost_model_fingerprint`, `engine_version`

### tests/
- Pattern: FreshDatabase + apply_migrations(), FakeConnection, unittest.TestCase
- Tests use real postgres via `_server_available()` guard or pure unit tests
- `test_strategy_backtest_core.py` — comprehensive unit tests for engine, feed, parity

### console (trading-ops-console/src/)
- `StrategyOnboardingPage.tsx` — currently reads "not built yet" placeholder
- `lib/strategyInstances.ts` — helpers for lifecycle toggle, config edit blocks
- `api/TradingApi.ts`, `api/RealTradingApi.ts` — typed API client
- `api/endpoints.ts` — endpoint paths

### deploy/platform_api/workload.yaml
- Image: localhost:5001/trading-platform-control-api
- Env: TRADING_POSTGRES_DSN, ORCHESTRATOR_MODE, SIGNAL_AUTHORITY_MODE, EXECUTION_AUTHORITY_MODE

---

## Migrations
- [x] 042_strategy_mgmt_pipeline.sql — strategy_mgmt schema: strategy_definition_v2, strategy_version, parameter_schema, parameter_set, backtest_run, strategy_instance_v2

## Backend API (platform_api/strategy_mgmt.py + wired into control.py)
- [x] POST /api/v1/strategy-definitions
- [x] GET  /api/v1/strategy-definitions
- [x] GET  /api/v1/strategy-definitions/{id}
- [x] POST /api/v1/strategy-versions
- [x] GET  /api/v1/strategy-versions/{id}
- [x] POST /api/v1/strategy-versions/{id}/freeze
- [x] POST /api/v1/parameter-sets
- [x] GET  /api/v1/parameter-sets/{id}
- [x] POST /api/v1/strategy-versions/{id}/backtests
- [x] GET  /api/v1/backtests/{id}
- [x] POST /api/v1/strategy-instances
- [x] GET  /api/v1/strategy-instances/{id}
- [x] PATCH /api/v1/strategy-instances/{id}  (online toggle only)

## Backtest async job
- [x] BacktestJobRunner in strategy_mgmt.py — thread pool, QUEUED→RUNNING→COMPLETED|FAILED

## Console
- [ ] Update StrategyOnboardingPage.tsx to guided 12-step flow
- [ ] Add API client methods for new endpoints
- [ ] Add ONBOARDING_STEPS with correct status

## Tests
- [x] tests/test_strategy_mgmt_pipeline.py

## Gaps / known issues
- New pipeline uses `strategy_mgmt` schema (separate from existing `platform` schema) to avoid
  disrupting live production tables
- BacktestJobRunner runs in a thread pool from the API process; for now this is acceptable
  (no separate worker process needed for minimum viable pipeline)
- Console UI changes are minimal scaffolding — the full wizard is wired but uses new API endpoints
