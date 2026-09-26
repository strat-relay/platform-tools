-- Canonical instrument -> provider symbol mapping, owned by the database (previously the
-- `symbol_mappings` block of platform.json). Additive only.
--
-- This is the instrument catalog: a canonical instrument can join a strategy instance's
-- membership (025) only if it has an ACTIVE mapping for the provider. Strategy membership stays
-- provider-neutral; provider symbols live only here.
--
-- Not used by V2 execution symbol resolution (V2_BROKER_SYMBOL_MAP_JSON), which stays an explicit,
-- separately controlled real-money boundary.
CREATE TABLE IF NOT EXISTS platform.instrument_provider_mapping (
    provider text NOT NULL CHECK (provider <> ''),
    canonical_instrument text NOT NULL
        CHECK (canonical_instrument <> '' AND canonical_instrument = upper(canonical_instrument)),
    provider_symbol text NOT NULL CHECK (provider_symbol <> ''),
    asset_class text NOT NULL
        CHECK (asset_class IN ('FX', 'CRYPTO', 'METAL', 'INDEX', 'COMMODITY', 'EQUITY', 'OTHER')),
    display_name text,
    state text NOT NULL DEFAULT 'ACTIVE' CHECK (state IN ('ACTIVE', 'DISABLED')),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    PRIMARY KEY (provider, canonical_instrument),
    UNIQUE (provider, provider_symbol)
);

CREATE INDEX IF NOT EXISTS instrument_provider_mapping_active_idx
    ON platform.instrument_provider_mapping (provider, state, asset_class, canonical_instrument);

-- The four mappings previously carried by platform.json, so behaviour is unchanged on upgrade.
INSERT INTO platform.instrument_provider_mapping
    (provider, canonical_instrument, provider_symbol, asset_class, display_name, updated_by)
VALUES
    ('MT5', 'XAUUSD', 'XAUUSDm', 'METAL', 'Gold', 'migration:027'),
    ('MT5', 'BTCUSD', 'BTCUSDm', 'CRYPTO', 'Bitcoin', 'migration:027'),
    ('MT5', 'USDJPY', 'USDJPYm', 'FX', 'USDJPY', 'migration:027'),
    ('MT5', 'EURUSD', 'EURUSDm', 'FX', 'EURUSD', 'migration:027')
ON CONFLICT (provider, canonical_instrument) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON platform.instrument_provider_mapping TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON platform.instrument_provider_mapping TO trading_readonly;
    END IF;
END
$$;
