-- Dedicated research-only Price Action observer registrations and durable ledger.
-- Additive and idempotent.  Both instances are online for observation only and remain
-- execution-ineligible; no platform signal/outbox/execution table is touched.
CREATE SCHEMA IF NOT EXISTS research;

CREATE TABLE IF NOT EXISTS research.price_action_observer_state (
    instance_id text PRIMARY KEY,
    strategy_key text NOT NULL CHECK (strategy_key IN ('sr_retest', 'engulfing_continuation')),
    state jsonb NOT NULL CHECK (jsonb_typeof(state) = 'object'),
    last_observed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS research.price_action_observer_evaluation (
    event_id text PRIMARY KEY,
    instance_id text NOT NULL REFERENCES research.price_action_observer_state(instance_id),
    strategy_key text NOT NULL CHECK (strategy_key IN ('sr_retest', 'engulfing_continuation')),
    instrument text NOT NULL,
    observed_at timestamptz NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    UNIQUE (instance_id, event_id)
);

GRANT SELECT, INSERT, UPDATE ON research.price_action_observer_state,
    research.price_action_observer_evaluation TO trading_app;
GRANT SELECT ON research.price_action_observer_state,
    research.price_action_observer_evaluation TO trading_readonly;

DO $$
DECLARE
    sr_def uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0101';
    sr_ver uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0102';
    sr_ps uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0103';
    sr_inst uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104';
    eg_def uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0201';
    eg_ver uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0202';
    eg_ps uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0203';
    eg_inst uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204';
    universe jsonb := '["EURUSD","GBPUSD","USDJPY","USDCHF","USDCAD","AUDUSD","NZDUSD","EURJPY","GBPJPY","EURGBP","AUDJPY","CADJPY","CHFJPY","GBPAUD","GBPCAD"]'::jsonb;
BEGIN
    INSERT INTO strategy_mgmt.parameter_schema(schema_id, fields, created_by)
    VALUES ('PRICE_ACTION_V1', '{"frozen":true,"source":"strategy_backtest.price_action_*"}', 'migration:043')
    ON CONFLICT (schema_id) DO NOTHING;

    INSERT INTO strategy_mgmt.strategy_definition(id,name,family_key,description,provenance_notes,created_by)
    VALUES (sr_def,'Support/Resistance Retest V1','PRICE_ACTION_SUPPORT_RESISTANCE_RETEST_V1','Research-only causal Price Action retest evaluator','Frozen evaluator and parameter hashes are in the forward manifest; observation only.','migration:043')
    ON CONFLICT (family_key) DO NOTHING;
    INSERT INTO strategy_mgmt.strategy_definition(id,name,family_key,description,provenance_notes,created_by)
    VALUES (eg_def,'Engulfing Continuation V1','PRICE_ACTION_ENGULFING_CONTINUATION_V1','Research-only causal Price Action continuation evaluator','Frozen evaluator and parameter hashes are in the forward manifest; observation only.','migration:043')
    ON CONFLICT (family_key) DO NOTHING;

    INSERT INTO strategy_mgmt.strategy_version(id,definition_id,version_label,evaluator_key,lifecycle,schema_id,release_notes,frozen_at,frozen_by,created_by)
    VALUES (sr_ver,sr_def,'V1','price_action_sr_retest_v1','FROZEN','PRICE_ACTION_V1','Frozen research evaluator; no rule changes.',now(),'migration:043','migration:043')
    ON CONFLICT (definition_id,version_label) DO NOTHING;
    INSERT INTO strategy_mgmt.strategy_version(id,definition_id,version_label,evaluator_key,lifecycle,schema_id,release_notes,frozen_at,frozen_by,created_by)
    VALUES (eg_ver,eg_def,'V1','price_action_engulfing_continuation_v1','FROZEN','PRICE_ACTION_V1','Frozen research evaluator; no rule changes.',now(),'migration:043','migration:043')
    ON CONFLICT (definition_id,version_label) DO NOTHING;

    INSERT INTO strategy_mgmt.parameter_set(id,parameter_set_id,strategy_version_id,schema_id,values,fingerprint,frozen,frozen_at,frozen_by,provenance,created_by)
    VALUES (sr_ps,'PRICE_ACTION_SR_RETEST_V1_FROZEN',sr_ver,'PRICE_ACTION_V1','{"context_timeframe":"H1","execution_timeframe":"M15","minimum_planned_r":1.5,"allow_retest_confirmation":false,"break_buffer_ticks":1,"level_lookback_context_bars":80,"level_max_age_context_bars":120,"level_tolerance_ticks":8,"max_retest_bars":4,"rejection_close_location":0.65,"rejection_min_wick_body_ratio":1.5,"stop_buffer_ticks":2,"swing_left_bars":2,"swing_right_bars":2,"entry_gap_cancel_ticks":8}'::jsonb,'572a9be2cc48bdbcf07004a832803e7cd3cc4e1ffa0e4e0b11d3de60a5789e90',true,now(),'migration:043','{"status":"RESEARCH","execution_eligible":false}'::jsonb,'migration:043')
    ON CONFLICT (parameter_set_id) DO NOTHING;
    INSERT INTO strategy_mgmt.parameter_set(id,parameter_set_id,strategy_version_id,schema_id,values,fingerprint,frozen,frozen_at,frozen_by,provenance,created_by)
    VALUES (eg_ps,'PRICE_ACTION_ENGULFING_CONTINUATION_V1_FROZEN',eg_ver,'PRICE_ACTION_V1','{"context_timeframe":"H1","execution_timeframe":"M15","minimum_planned_r":1.5,"allow_same_candle_interaction_confirmation":false,"entry_gap_cancel_ticks":8,"max_pullback_bars":6,"minimum_engulfing_body_ticks":4,"stop_buffer_ticks":2,"swing_left_bars":2,"swing_right_bars":2,"zone_max_age_context_bars":120,"zone_tolerance_ticks":8}'::jsonb,'e39630114425a73aea70ea8768273cba4eb3fd834cef6593a1d84618004127a1',true,now(),'migration:043','{"status":"RESEARCH","execution_eligible":false}'::jsonb,'migration:043')
    ON CONFLICT (parameter_set_id) DO NOTHING;

    INSERT INTO strategy_mgmt.strategy_instance_v2(id,strategy_version_id,parameter_set_id,display_name,online,execution_eligible,instruments,attributes,created_by)
    VALUES (sr_inst,sr_ver,sr_ps,'Price Action Support/Resistance Retest V1 observer',true,false,universe,'{"status":"RESEARCH","mode":"OBSERVER_ONLY","broker_writes":false,"signal_publication":false,"observer_instance_id":"price-action-sr-retest-v1"}'::jsonb,'migration:043')
    ON CONFLICT (id) DO NOTHING;
    INSERT INTO strategy_mgmt.strategy_instance_v2(id,strategy_version_id,parameter_set_id,display_name,online,execution_eligible,instruments,attributes,created_by)
    VALUES (eg_inst,eg_ver,eg_ps,'Price Action Engulfing Continuation V1 observer',true,false,universe,'{"status":"RESEARCH","mode":"OBSERVER_ONLY","broker_writes":false,"signal_publication":false,"observer_instance_id":"price-action-engulfing-continuation-v1"}'::jsonb,'migration:043')
    ON CONFLICT (id) DO NOTHING;
END $$;
