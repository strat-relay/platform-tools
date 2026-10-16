-- The observer retains exclusive application writes to its research ledger.
-- Canonical publication and portal status readers need only SELECT access.
-- Do not amend 063/064 or grant membership in strategy_library_observer.
DO $permissions$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT USAGE ON SCHEMA research TO trading_app;
        GRANT SELECT ON research.strategy_library_forward_state,
                        research.strategy_library_forward_event TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT USAGE ON SCHEMA research TO trading_readonly;
        GRANT SELECT ON research.strategy_library_forward_state,
                        research.strategy_library_forward_event TO trading_readonly;
    END IF;
END $permissions$;
