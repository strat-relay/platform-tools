-- Repair the Price Action V1 managed registry after migration 057 collided with
-- earlier generic rows.  This is metadata identity repair only: frozen evaluator
-- versions, parameter values, canonical signals, and execution gates are unchanged.

DO $$
DECLARE
    sr_instance uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104';
    sr_version uuid;
    sr_dedicated_definition uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0107';
    sr_dedicated_version uuid;
    sr_parameter uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0103';
    sr_dedicated_parameter uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0105';
    eg_instance uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204';
    eg_version uuid := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0202';
    generic_definition uuid;
    generic_engulfing_version uuid;
BEGIN
    SELECT id INTO generic_definition
      FROM strategy_mgmt.strategy_definition
     WHERE family_key = 'PRICE_ACTION';
    SELECT v.id INTO sr_version
      FROM strategy_mgmt.strategy_version v
      JOIN strategy_mgmt.strategy_definition d ON d.id = v.definition_id
     WHERE d.family_key = 'PRICE_ACTION'
       AND v.evaluator_key = 'price_action_sr_retest_v1';
    SELECT v.id INTO generic_engulfing_version
      FROM strategy_mgmt.strategy_version v
      JOIN strategy_mgmt.strategy_definition d ON d.id = v.definition_id
     WHERE d.family_key = 'PRICE_ACTION'
       AND v.evaluator_key = 'price_action_engulfing_continuation_v1';

    INSERT INTO strategy_mgmt.strategy_definition
        (id, name, family_key, description, provenance_notes, created_by)
    VALUES
        (sr_dedicated_definition, 'Support/Resistance Retest V1',
         'PRICE_ACTION_SUPPORT_RESISTANCE_RETEST_V1',
         'Research-only causal Price Action retest evaluator',
         'Frozen evaluator and parameter hashes are in the forward manifest; observation only.',
         'migration:059')
    ON CONFLICT (family_key) DO NOTHING;

    SELECT id INTO sr_dedicated_definition
      FROM strategy_mgmt.strategy_definition
     WHERE family_key = 'PRICE_ACTION_SUPPORT_RESISTANCE_RETEST_V1';

    SELECT id INTO sr_dedicated_version
      FROM strategy_mgmt.strategy_version
     WHERE definition_id = sr_dedicated_definition AND version_label = 'V1';
    IF sr_dedicated_version IS NULL THEN
        sr_dedicated_version := '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0106';
        INSERT INTO strategy_mgmt.strategy_version
            (id, definition_id, version_label, evaluator_key, lifecycle, schema_id,
             release_notes, frozen_at, frozen_by, created_by)
        VALUES
            (sr_dedicated_version,
             sr_dedicated_definition, 'V1',
             'price_action_sr_retest_v1', 'FROZEN', 'PRICE_ACTION_V1',
             'Frozen research evaluator; no rule changes.', now(), 'migration:059', 'migration:059');
    END IF;

    -- The original SR parameter-set key already existed against the generic
    -- version.  Copy its frozen contents to a new immutable row owned by the
    -- dedicated frozen SR version; no parameter value is changed.
    INSERT INTO strategy_mgmt.parameter_set
        (id, parameter_set_id, strategy_version_id, schema_id, values, fingerprint,
         frozen, frozen_at, frozen_by, provenance, created_by)
    SELECT sr_dedicated_parameter, 'PRICE_ACTION_SR_RETEST_V1_FROZEN_DEDICATED',
           sr_dedicated_version, schema_id, values, fingerprint, frozen, frozen_at, frozen_by,
           provenance || '{"repair":"migration:059"}'::jsonb, 'migration:059'
      FROM strategy_mgmt.parameter_set
     WHERE id = sr_parameter
    ON CONFLICT (parameter_set_id) DO NOTHING;

    UPDATE strategy_mgmt.strategy_instance_v2
       SET strategy_version_id = sr_dedicated_version,
           parameter_set_id = sr_dedicated_parameter,
           display_name = 'Support/Resistance Retest V1 observer',
           attributes = attributes ||
             '{"status":"RESEARCH","mode":"OBSERVER_ONLY","broker_writes":false,
                "publication_mode":"ORCHESTRATOR_RESEARCH_ONLY","signal_publication":true,
                "observer_instance_id":"price-action-sr-retest-v1"}'::jsonb,
           updated_at = now()
     WHERE id = sr_instance;

    UPDATE strategy_mgmt.strategy_instance_v2
       SET strategy_version_id = eg_version,
           display_name = 'Engulfing Continuation V1 observer',
           attributes = attributes ||
             '{"status":"RESEARCH","mode":"OBSERVER_ONLY","broker_writes":false,
                "signal_publication":false,
                "observer_instance_id":"price-action-engulfing-continuation-v1"}'::jsonb,
           updated_at = now()
     WHERE id = eg_instance;

    UPDATE strategy_mgmt.strategy_definition
       SET name = 'Engulfing Continuation V1',
           updated_at = now()
     WHERE family_key = 'PRICE_ACTION_ENGULFING_CONTINUATION_V1';

    -- The generic Price Action rows are now unreferenced zombie registry data.
    -- Foreign keys intentionally make this fail rather than deleting an
    -- unexpected dependent backtest or instance.
    DELETE FROM strategy_mgmt.parameter_set WHERE id = sr_parameter;
    DELETE FROM strategy_mgmt.strategy_version WHERE id = sr_version;
    DELETE FROM strategy_mgmt.strategy_version WHERE id = generic_engulfing_version;
    DELETE FROM strategy_mgmt.strategy_definition WHERE id = generic_definition;
END $$;
