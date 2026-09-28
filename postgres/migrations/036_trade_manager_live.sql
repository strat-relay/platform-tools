-- Trade Manager LIVE mode and personal management execution (P5 management effects).
--
--   LIVE    as SHADOW (every observation is evaluated and recorded), and additionally the
--           execution plane acts on actionable decisions for trades that are linked to a platform
--           broker position: SL/TP changes and closes, through the REDUCE_ONLY fenced bridge path.
--
-- Additive: the mode row keeps its current value (SHADOW). Nothing reaches a broker until an
-- operator sets LIVE and execution authority is ENABLED.
ALTER TABLE trade_management.trade_manager_mode DROP CONSTRAINT IF EXISTS trade_manager_mode_mode_check;
ALTER TABLE trade_management.trade_manager_mode
    ADD CONSTRAINT trade_manager_mode_mode_check CHECK (mode IN ('OFF', 'SHADOW', 'LIVE'));
ALTER TABLE trade_management.trade_manager_mode_change DROP CONSTRAINT IF EXISTS trade_manager_mode_change_previous_mode_check;
ALTER TABLE trade_management.trade_manager_mode_change
    ADD CONSTRAINT trade_manager_mode_change_previous_mode_check
    CHECK (previous_mode IS NULL OR previous_mode IN ('OFF', 'SHADOW', 'LIVE'));
ALTER TABLE trade_management.trade_manager_mode_change DROP CONSTRAINT IF EXISTS trade_manager_mode_change_new_mode_check;
ALTER TABLE trade_management.trade_manager_mode_change
    ADD CONSTRAINT trade_manager_mode_change_new_mode_check CHECK (new_mode IN ('OFF', 'SHADOW', 'LIVE'));

-- One row per actionable Trade Manager decision considered for a platform position. The
-- authorization rules are the legacy trade_manager/central.authorize() rules, now in one DB
-- transaction (docs/migration/07): LIVE mode, execution authority, decision freshness, the
-- platform ownership link (managed trade -> execution intent -> FILLED result -> broker ticket),
-- the position present at the broker, stop safety (never widen or remove), and a unique action key.
CREATE TABLE IF NOT EXISTS execution_v2.management_intent (
    management_intent_id text PRIMARY KEY,
    decision_id text NOT NULL UNIQUE,
    managed_trade_id text NOT NULL,
    execution_intent_id text REFERENCES execution_v2.execution_intent(execution_intent_id),
    account_id text NOT NULL,
    broker_ticket text,
    broker_symbol text,
    tm_action text NOT NULL,
    broker_action text CHECK (broker_action IS NULL OR broker_action IN ('MODIFY', 'CLOSE')),
    requested_stop numeric,
    requested_target numeric,
    broker_stop_before numeric,
    broker_target_before numeric,
    action_key text UNIQUE,
    status text NOT NULL CHECK (status IN ('REJECTED', 'NO_CHANGE', 'AUTHORIZED', 'SENDING', 'APPLIED',
                                           'BROKER_REJECTED', 'FENCED', 'UNKNOWN_RECONCILIATION_REQUIRED')),
    reason text,
    attempt_id text UNIQUE,
    generation bigint,
    broker_response jsonb,
    decision_time timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS management_intent_trade_idx ON execution_v2.management_intent (managed_trade_id, created_at);
CREATE INDEX IF NOT EXISTS management_intent_status_idx ON execution_v2.management_intent (status);
