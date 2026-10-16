-- Group role only: no login credentials, memberships or existing grants changed.
DO $observer_role$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='strategy_library_observer') THEN
        CREATE ROLE strategy_library_observer NOLOGIN NOSUPERUSER NOCREATEDB
            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='strategy_library_observer'
               AND (rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION 'Existing strategy_library_observer role has incompatible privileges';
    END IF;
END $observer_role$;
GRANT USAGE ON SCHEMA research TO strategy_library_observer;
GRANT SELECT, INSERT, UPDATE ON research.strategy_library_forward_state
    TO strategy_library_observer;
GRANT SELECT, INSERT ON research.strategy_library_forward_event
    TO strategy_library_observer;
-- No DELETE grant, no canonical strategy/outcome or execution grants.
-- A separate dedicated login/Secret must be provisioned without exposing credentials.
