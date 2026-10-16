-- Publish only the three frozen canary compositions through the orchestrator.

-- No evaluator, observer run, execution authority, account, or historical identity changes.

DO $guard$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i
 JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id
 JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id
 WHERE i.id='fea3769c-c3f2-52f5-8ae2-f0be9d12abeb'::uuid AND p.fingerprint='477eae93ee755f09dd974d7c52b95fcf9363d69aa7af508d3978199fd8c9ab0a'
 AND v.evaluator_key='library.donchian_breakout@V1' AND NOT i.execution_eligible AND i.execution_mode='OFF')
 THEN RAISE EXCEPTION 'Library publication identity/safety conflict: donchian_breakout'; END IF;
 IF EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id='BREAKOUT_DONCHIAN_BREAKOUT'
 AND (adapter IS DISTINCT FROM 'StrategyLibraryAdapter' OR strategy_version <> 'V1'))
 THEN RAISE EXCEPTION 'Library parent conflict: donchian_breakout'; END IF;
END $guard$;

INSERT INTO platform.strategy_definition
 (strategy_id,strategy_version,display_name,description,adapter,enabled,routes,default_instance_id,updated_by)
 VALUES ('BREAKOUT_DONCHIAN_BREAKOUT','V1','Donchian Breakout',
 'Research-only completed-candle evidence; no validated performance or broker fill claim.',
 'StrategyLibraryAdapter',true,'{"audit":true,"shadow_execution":false,"distribution_queue":true}'::jsonb,
 'fea3769c-c3f2-52f5-8ae2-f0be9d12abeb','migration:064') ON CONFLICT (strategy_id) DO NOTHING;

UPDATE strategy_mgmt.strategy_instance_v2 SET online=true,
 attributes=attributes || '{"orchestrator_run_id": "library-canary-20261016-v1", "publication_activation_time": 1792174500, "publication_mode": "ORCHESTRATOR_RESEARCH_ONLY", "signal_publication": true}'::jsonb,
 updated_at=now() WHERE id='fea3769c-c3f2-52f5-8ae2-f0be9d12abeb'::uuid;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('fea3769c-c3f2-52f5-8ae2-f0be9d12abeb','BREAKOUT_DONCHIAN_BREAKOUT','XAUUSD','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('fea3769c-c3f2-52f5-8ae2-f0be9d12abeb','BREAKOUT_DONCHIAN_BREAKOUT','BTCUSD','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('fea3769c-c3f2-52f5-8ae2-f0be9d12abeb','BREAKOUT_DONCHIAN_BREAKOUT','USDJPY','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

DO $guard$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i
 JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id
 JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id
 WHERE i.id='1c6d1d50-8850-5b2a-b500-d9df0c51fe78'::uuid AND p.fingerprint='e3183b7bc463be53267ecf040b5694b23fd3e1e2f6d94e086304eab5ed7801a7'
 AND v.evaluator_key='library.ema_pullback@V1' AND NOT i.execution_eligible AND i.execution_mode='OFF')
 THEN RAISE EXCEPTION 'Library publication identity/safety conflict: ema_pullback'; END IF;
 IF EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id='TREND_FOLLOWING_EMA_PULLBACK'
 AND (adapter IS DISTINCT FROM 'StrategyLibraryAdapter' OR strategy_version <> 'V1'))
 THEN RAISE EXCEPTION 'Library parent conflict: ema_pullback'; END IF;
END $guard$;

INSERT INTO platform.strategy_definition
 (strategy_id,strategy_version,display_name,description,adapter,enabled,routes,default_instance_id,updated_by)
 VALUES ('TREND_FOLLOWING_EMA_PULLBACK','V1','EMA Pullback',
 'Research-only completed-candle evidence; no validated performance or broker fill claim.',
 'StrategyLibraryAdapter',true,'{"audit":true,"shadow_execution":false,"distribution_queue":true}'::jsonb,
 '1c6d1d50-8850-5b2a-b500-d9df0c51fe78','migration:064') ON CONFLICT (strategy_id) DO NOTHING;

UPDATE strategy_mgmt.strategy_instance_v2 SET online=true,
 attributes=attributes || '{"orchestrator_run_id": "library-canary-20261016-v1", "publication_activation_time": 1792174500, "publication_mode": "ORCHESTRATOR_RESEARCH_ONLY", "signal_publication": true}'::jsonb,
 updated_at=now() WHERE id='1c6d1d50-8850-5b2a-b500-d9df0c51fe78'::uuid;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('1c6d1d50-8850-5b2a-b500-d9df0c51fe78','TREND_FOLLOWING_EMA_PULLBACK','XAUUSD','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('1c6d1d50-8850-5b2a-b500-d9df0c51fe78','TREND_FOLLOWING_EMA_PULLBACK','BTCUSD','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('1c6d1d50-8850-5b2a-b500-d9df0c51fe78','TREND_FOLLOWING_EMA_PULLBACK','USDJPY','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

DO $guard$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM strategy_mgmt.strategy_instance_v2 i
 JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id
 JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id
 WHERE i.id='f8dcf44a-9b37-5f97-b766-318f71e9848f'::uuid AND p.fingerprint='ab185500095a1f175995a39dfd212fb5e99a38c454e3f819fb6d515e63f4eed9'
 AND v.evaluator_key='library.fvg_retracement@V1' AND NOT i.execution_eligible AND i.execution_mode='OFF')
 THEN RAISE EXCEPTION 'Library publication identity/safety conflict: fvg_retracement'; END IF;
 IF EXISTS (SELECT 1 FROM platform.strategy_definition WHERE strategy_id='SMC_FVG_RETRACEMENT'
 AND (adapter IS DISTINCT FROM 'StrategyLibraryAdapter' OR strategy_version <> 'V1'))
 THEN RAISE EXCEPTION 'Library parent conflict: fvg_retracement'; END IF;
END $guard$;

INSERT INTO platform.strategy_definition
 (strategy_id,strategy_version,display_name,description,adapter,enabled,routes,default_instance_id,updated_by)
 VALUES ('SMC_FVG_RETRACEMENT','V1','FVG Retracement',
 'Research-only completed-candle evidence; no validated performance or broker fill claim.',
 'StrategyLibraryAdapter',true,'{"audit":true,"shadow_execution":false,"distribution_queue":true}'::jsonb,
 'f8dcf44a-9b37-5f97-b766-318f71e9848f','migration:064') ON CONFLICT (strategy_id) DO NOTHING;

UPDATE strategy_mgmt.strategy_instance_v2 SET online=true,
 attributes=attributes || '{"orchestrator_run_id": "library-canary-20261016-v1", "publication_activation_time": 1792174500, "publication_mode": "ORCHESTRATOR_RESEARCH_ONLY", "signal_publication": true}'::jsonb,
 updated_at=now() WHERE id='f8dcf44a-9b37-5f97-b766-318f71e9848f'::uuid;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('f8dcf44a-9b37-5f97-b766-318f71e9848f','SMC_FVG_RETRACEMENT','XAUUSD','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('f8dcf44a-9b37-5f97-b766-318f71e9848f','SMC_FVG_RETRACEMENT','BTCUSD','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;

INSERT INTO strategy.instrument_membership
 (strategy_instance_id,strategy_id,canonical_instrument,state,updated_by)
 VALUES ('f8dcf44a-9b37-5f97-b766-318f71e9848f','SMC_FVG_RETRACEMENT','USDJPY','ACTIVE','migration:064')
 ON CONFLICT (strategy_instance_id,canonical_instrument) DO NOTHING;
