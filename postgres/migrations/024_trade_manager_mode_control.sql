-- Operator-controlled Trade Manager mode (singleton control plane, mirroring
-- execution_v2.execution_authority from 020). Additive only.
--
--   SHADOW  current deployed behaviour: ManagedTrades are created, observed and evaluated;
--           every decision is recorded SHADOW and withheld. No broker effects.
--   OFF     the Trade Manager runtime does no work: no ManagedTrade creation (recorded in
--           managed_trade_skip as TRADE_MANAGER_OFF), no observations, no decisions.
--
-- There is deliberately no mode with broker effects. Seeded SHADOW so applying this migration
-- changes nothing about the running system.
CREATE TABLE IF NOT EXISTS trade_management.trade_manager_mode (
    mode_id text PRIMARY KEY DEFAULT 'current',
    mode text NOT NULL CHECK (mode IN ('OFF', 'SHADOW')),
    revision bigint NOT NULL CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    CONSTRAINT trade_manager_mode_singleton CHECK (mode_id = 'current')
);

CREATE TABLE IF NOT EXISTS trade_management.trade_manager_mode_change (
    change_id text PRIMARY KEY,
    mode_id text NOT NULL REFERENCES trade_management.trade_manager_mode(mode_id),
    previous_mode text CHECK (previous_mode IS NULL OR previous_mode IN ('OFF', 'SHADOW')),
    new_mode text NOT NULL CHECK (new_mode IN ('OFF', 'SHADOW')),
    previous_revision bigint,
    new_revision bigint NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    changed_by text,
    reason text
);

CREATE OR REPLACE FUNCTION trade_management.forbid_mode_change_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'trade_manager_mode_change is append-only: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trade_manager_mode_change_append_only ON trade_management.trade_manager_mode_change;
CREATE TRIGGER trade_manager_mode_change_append_only
    BEFORE UPDATE OR DELETE ON trade_management.trade_manager_mode_change
    FOR EACH ROW EXECUTE FUNCTION trade_management.forbid_mode_change_mutation();

INSERT INTO trade_management.trade_manager_mode (mode_id, mode, revision, updated_by)
VALUES ('current', 'SHADOW', 1, 'migration:024')
ON CONFLICT (mode_id) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, UPDATE ON trade_management.trade_manager_mode TO trading_app;
        GRANT SELECT, INSERT ON trade_management.trade_manager_mode_change TO trading_app;
    END IF;
END
$$;
