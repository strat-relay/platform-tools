-- Enable the additive research-signal handoff for Engulfing Continuation V1.
-- Mirrors what migration 058 did for SR Retest.  No execution gate is changed;
-- broker_writes remain false and execution_eligible is not set.
INSERT INTO platform.strategy_definition
    (strategy_id, strategy_version, display_name, description, adapter, enabled, routes, updated_by)
VALUES
    ('PRICE_ACTION_ENGULFING_CONTINUATION', 'V1', 'Engulfing Continuation V1',
     'Research-only accepted-entry handoff to the canonical orchestrator and outcome resolver.',
     'PriceActionObserverAdapter', true,
     '{"audit":true,"shadow_execution":false,"distribution_queue":true}'::jsonb,
     'migration:060')
ON CONFLICT (strategy_id) DO UPDATE SET
    strategy_version=EXCLUDED.strategy_version, adapter=EXCLUDED.adapter,
    enabled=true, routes=EXCLUDED.routes, updated_by=EXCLUDED.updated_by,
    updated_at=now();

UPDATE strategy_mgmt.strategy_instance_v2
   SET attributes = attributes || '{"signal_publication":true,"publication_mode":"ORCHESTRATOR_RESEARCH_ONLY"}'::jsonb
 WHERE id = '7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204';
