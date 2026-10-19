-- Rebind the registered research-only ICT/SMC instances to the rotated
-- immutable observer run. The previous run and its evidence remain intact.
-- This does not activate execution or change strategy semantics.

UPDATE strategy_mgmt.strategy_instance_v2
SET attributes = attributes || jsonb_build_object(
    'orchestrator_run_id', 'ict-smc-forward-20261019-0226',
    'publication_activation_time', 1792377096,
    'forward_image', 'ghcr.io/strat-relay/trading-platform-runtime@sha256:533e86147574e0a9c9735a9117aff8d9529f0c129ccce106a7d1754c912dc236'
),
updated_at = now()
WHERE id IN (
    '4a411452-1440-5f91-bb95-eaf6dabbccc2'::uuid,
    '3441a68f-6209-56fa-b3ad-c0e8d6c3cb86'::uuid,
    'b2af32aa-e42d-5ff6-9a42-2aece970459f'::uuid
)
AND attributes->>'orchestrator_run_id' = 'ict-smc-forward-20261018-1845'
AND execution_eligible = false
AND execution_mode = 'OFF';

DO $verify$
BEGIN
    IF (
        SELECT count(*)
        FROM strategy_mgmt.strategy_instance_v2
        WHERE id IN (
            '4a411452-1440-5f91-bb95-eaf6dabbccc2'::uuid,
            '3441a68f-6209-56fa-b3ad-c0e8d6c3cb86'::uuid,
            'b2af32aa-e42d-5ff6-9a42-2aece970459f'::uuid
        )
        AND attributes->>'orchestrator_run_id' = 'ict-smc-forward-20261019-0226'
        AND attributes->>'publication_activation_time' = '1792377096'
        AND execution_eligible = false
        AND execution_mode = 'OFF'
    ) <> 3 THEN
        RAISE EXCEPTION 'ICT/SMC run binding migration verification failed';
    END IF;
END $verify$;
