-- Stable unprefixed names for the normalized operational model.  These are
-- read-only views over the row tables; the only mutable economic-position
-- relation for the working-set path is strategy.economic_positions.
CREATE OR REPLACE VIEW strategy.symbol_progress AS
    SELECT strategy_id, symbol, last_m5, last_m15, initialized, last_candle,
           updated_at, source_timestamp, import_batch_id
    FROM strategy.phase6_symbol_progress;

CREATE OR REPLACE VIEW strategy.setups AS
    SELECT * FROM strategy.phase6_setups;

CREATE OR REPLACE VIEW strategy.setup_lifecycle AS
    SELECT * FROM strategy.phase6_setup_lifecycle;

CREATE OR REPLACE VIEW strategy.entry_opportunities AS
    SELECT * FROM strategy.phase6_entry_opportunities;
