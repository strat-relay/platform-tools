-- Explicit pip/point units for planned signal distances.
-- This is metadata only: it does not change signal prices, outcomes, risk, or execution.
ALTER TABLE platform.instrument_provider_mapping
    ADD COLUMN IF NOT EXISTS pip_size numeric;

ALTER TABLE platform.instrument_provider_mapping
    DROP CONSTRAINT IF EXISTS instrument_provider_mapping_pip_size_check;
ALTER TABLE platform.instrument_provider_mapping
    ADD CONSTRAINT instrument_provider_mapping_pip_size_check
    CHECK (pip_size IS NULL OR pip_size > 0);

-- Existing canonical mappings have stable display units. Instruments without a
-- configured unit remain explicitly unavailable for planned-pip reporting.
UPDATE platform.instrument_provider_mapping
SET pip_size = CASE canonical_instrument
    WHEN 'EURUSD' THEN 0.0001
    WHEN 'USDJPY' THEN 0.01
    WHEN 'XAUUSD' THEN 0.1
    WHEN 'BTCUSD' THEN 1
    WHEN 'ETHBTC' THEN 0.00001
    ELSE pip_size
END,
updated_at = now(), updated_by = 'migration:062'
WHERE provider = 'MT5'
  AND canonical_instrument IN ('EURUSD', 'USDJPY', 'XAUUSD', 'BTCUSD', 'ETHBTC')
  AND pip_size IS NULL;

CREATE INDEX IF NOT EXISTS instrument_provider_mapping_pip_size_idx
    ON platform.instrument_provider_mapping (provider, canonical_instrument, pip_size);
