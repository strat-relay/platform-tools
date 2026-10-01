-- Register ETHBTC through the same canonical/provider-neutral boundaries as the existing
-- Context instruments.  The strategy and risk policy keep the canonical identity; only the
-- provider mapping carries the Exness MT5 symbol.
--
-- 035 was reserved as an empty placeholder on codex/liquidity-live-runtime before this
-- content was written; the SQL is applied here as 036 to avoid a checksum conflict.

INSERT INTO platform.instrument_provider_mapping
    (provider, canonical_instrument, provider_symbol, asset_class, display_name, updated_by)
VALUES
    ('MT5', 'ETHBTC', 'ETHBTCm', 'CRYPTO', 'ETHBTC', 'migration:036')
ON CONFLICT (provider, canonical_instrument) DO UPDATE SET
    provider_symbol = EXCLUDED.provider_symbol,
    asset_class = EXCLUDED.asset_class,
    display_name = EXCLUDED.display_name,
    state = 'ACTIVE',
    updated_at = now(),
    updated_by = EXCLUDED.updated_by;

INSERT INTO strategy.instrument_membership
    (strategy_instance_id, strategy_id, canonical_instrument, state, revision, updated_by)
VALUES
    ('phase6', 'CONTEXT_STRUCTURE_RETRACE_V1', 'ETHBTC', 'ACTIVE', 1, 'migration:036')
ON CONFLICT (strategy_instance_id, canonical_instrument) DO UPDATE SET
    state = 'ACTIVE',
    updated_at = now(),
    updated_by = EXCLUDED.updated_by;

-- This changes symbol scope only.  It does not alter risk amounts, account scope, authority,
-- or execution mode.  The canonical policy remains the runtime authority.
INSERT INTO execution_v2.risk_policy_allowed_symbol (policy_id, symbol)
SELECT 'current', 'ETHBTC'
WHERE EXISTS (SELECT 1 FROM execution_v2.risk_policy WHERE policy_id = 'current')
ON CONFLICT (policy_id, symbol) DO NOTHING;
