-- Register the database-backed Context V2 research instance.
-- V1/phase6 is intentionally untouched.  The new instance starts OFFLINE and
-- execution-ineligible; the operator must explicitly enable it from the UI.

CREATE SCHEMA IF NOT EXISTS strategy_mgmt;

DO $$
DECLARE
    definition_id uuid;
    version_id uuid;
    parameter_id uuid;
BEGIN
    SELECT d.id INTO definition_id
      FROM strategy_mgmt.strategy_definition AS d
     WHERE d.family_key = 'CONTEXT_STRUCTURE_RETRACE_V2';
    IF definition_id IS NULL THEN
        INSERT INTO strategy_mgmt.strategy_definition
            (name, family_key, description, provenance_notes, created_by)
        VALUES
            ('Context Structure Retrace V2', 'CONTEXT_STRUCTURE_RETRACE_V2',
             'Database-configured research runner derived from Context V1.',
             'V1 remains frozen; V2 parameters are immutable PostgreSQL ParameterSets.',
             'migration:044')
        RETURNING id INTO definition_id;
    END IF;

    SELECT v.id INTO version_id
      FROM strategy_mgmt.strategy_version AS v
     WHERE v.definition_id = definition_id AND v.version_label = 'V2';
    IF version_id IS NULL THEN
        INSERT INTO strategy_mgmt.strategy_version
            (definition_id, version_label, evaluator_key, lifecycle, schema_id, release_notes, created_by)
        VALUES
            (definition_id, 'V2', 'context_structure_retrace_v2_research', 'FROZEN',
             'context-structure-retrace-v2-runtime-v1',
             'Research-only Context V2 with database-backed runtime parameters.', 'migration:044')
        RETURNING id INTO version_id;
    END IF;

    INSERT INTO strategy_mgmt.parameter_schema(schema_id, fields, created_by)
    VALUES ('context-structure-retrace-v2-runtime-v1',
      '{
        "minimum_required_r":{"type":"decimal","required":true,"minimum":0.1,"maximum":10.0,"label":"Minimum planned R:R","unit":"R"},
        "target_extension_fraction":{"type":"decimal","required":true,"minimum":0.0,"maximum":5.0,"label":"Target extension","unit":"setup range"},
        "stop_atr_buffer_fraction":{"type":"decimal","required":true,"minimum":0.0,"maximum":5.0,"label":"Stop ATR buffer","unit":"ATR"},
        "stop_spread_buffer_multiplier":{"type":"decimal","required":true,"minimum":0.0,"maximum":10.0,"label":"Stop spread buffer","unit":"spread"},
        "retracement_entry_fraction":{"type":"decimal","required":true,"minimum":0.0,"maximum":1.0,"label":"Retracement entry fraction","unit":"setup range"},
        "max_retrace_candles":{"type":"integer","required":true,"minimum":1,"maximum":100,"label":"Maximum retracement candles","unit":"M5 candles"},
        "max_hold_minutes":{"type":"integer","required":true,"minimum":1,"maximum":10080,"label":"Maximum hold","unit":"minutes"},
        "enabled_setup_events":{"type":"text_list","required":true,"label":"Enabled setup patterns"},
        "reentry_enabled":{"type":"boolean","required":true,"label":"Allow re-entry"},
        "target_selection_policy":{"type":"enum","required":true,"enum":["NEAREST_VALID_STRUCTURE","EXTENSION_ONLY","OPPOSING_STRUCTURE_ONLY"],"label":"Target selection policy"}
      }'::jsonb, 'migration:044')
    ON CONFLICT (schema_id) DO NOTHING;

    SELECT id INTO parameter_id
      FROM strategy_mgmt.parameter_set
     WHERE parameter_set_id = 'context-v2-runtime-default';
    IF parameter_id IS NULL THEN
        INSERT INTO strategy_mgmt.parameter_set
            (parameter_set_id, strategy_version_id, schema_id, values, fingerprint, frozen,
             frozen_at, frozen_by, provenance, created_by)
        VALUES
          ('context-v2-runtime-default', version_id, 'context-structure-retrace-v2-runtime-v1',
           '{
             "minimum_required_r":1.0,
             "target_extension_fraction":0.5,
             "stop_atr_buffer_fraction":0.1,
             "stop_spread_buffer_multiplier":1.25,
             "retracement_entry_fraction":0.2,
             "max_retrace_candles":12,
             "max_hold_minutes":1440,
             "enabled_setup_events":["BULLISH_ENGULFING","BEARISH_ENGULFING","MORNING_STAR","EVENING_STAR","BULLISH_REJECTION_WICK","BEARISH_REJECTION_WICK"],
             "reentry_enabled":true,
             "target_selection_policy":"NEAREST_VALID_STRUCTURE"
           }'::jsonb,
           'ad897dff76e5b119d52fe7f05203d25d214b61c1dff69be18b9e8f784284335f', true, now(), 'migration:044',
           '{"research_only":true,"broker_writes":false,"source":"migration:044"}'::jsonb,
           'migration:044')
        RETURNING id INTO parameter_id;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM strategy_mgmt.strategy_instance_v2
         WHERE attributes->>'instance_id' = 'context-v2-research'
    ) THEN
        INSERT INTO strategy_mgmt.strategy_instance_v2
            (strategy_version_id, parameter_set_id, display_name, online,
             execution_eligible, instruments, attributes, created_by)
        VALUES
          (version_id, parameter_id, 'Context Structure Retrace V2 research', false, false,
           '[{"canonical_instrument":"XAUUSD","state":"ACTIVE"},{"canonical_instrument":"BTCUSD","state":"ACTIVE"},{"canonical_instrument":"USDJPY","state":"ACTIVE"},{"canonical_instrument":"EURUSD","state":"ACTIVE"}]'::jsonb,
           '{"instance_id":"context-v2-research","research_only":true,"broker_writes":false}'::jsonb,
           'migration:044');
    END IF;
END $$;
