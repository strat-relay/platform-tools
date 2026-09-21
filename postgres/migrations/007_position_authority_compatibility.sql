-- Preserve the legacy Phase 6 position table as audit data, then expose a
-- read-only compatibility view.  strategy.economic_positions is the sole
-- mutable position relation for the working-set model.
ALTER TABLE IF EXISTS strategy.phase6_economic_positions SET SCHEMA audit;
ALTER TABLE IF EXISTS audit.phase6_economic_positions RENAME TO phase6_economic_positions_legacy;

CREATE OR REPLACE VIEW strategy.phase6_economic_positions AS
    SELECT economic_position_id, entry_opportunity_id, setup_id, status,
           current_stop AS stop, target, mfe_price, mae_price, realized_r,
           NULL::numeric AS fill_timestamp, opened_at AS fill_timestamp_iso,
           NULL::numeric AS exit_timestamp, exit_reason, reentry_state AS reentry_type,
           provenance, updated_at
    FROM strategy.economic_positions;
