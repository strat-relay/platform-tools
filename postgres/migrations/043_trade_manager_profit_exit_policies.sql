-- Optional Trade Manager exit rules.  NULL means disabled; existing TIME_EXIT rows and
-- bindings remain unchanged.  New policy values are captured on each ManagedTrade.
ALTER TABLE trade_management.managed_trade
    ADD COLUMN IF NOT EXISTS net_profit_target_usd numeric,
    ADD COLUMN IF NOT EXISTS profit_target_pips numeric,
    ADD COLUMN IF NOT EXISTS profit_target_r numeric,
    ADD COLUMN IF NOT EXISTS pip_size numeric;

ALTER TABLE trade_management.managed_trade
    DROP CONSTRAINT IF EXISTS managed_trade_profit_exit_policy_check;
ALTER TABLE trade_management.managed_trade
    ADD CONSTRAINT managed_trade_profit_exit_policy_check CHECK (
        (net_profit_target_usd IS NULL OR net_profit_target_usd > 0) AND
        (profit_target_pips IS NULL OR profit_target_pips > 0) AND
        (profit_target_r IS NULL OR profit_target_r > 0) AND
        (pip_size IS NULL OR pip_size > 0)
    );

ALTER TABLE execution_v2.management_intent
    ADD COLUMN IF NOT EXISTS exit_policy_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS observed_quote jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS estimated_net_profit numeric,
    ADD COLUMN IF NOT EXISTS initial_risk_amount numeric,
    ADD COLUMN IF NOT EXISTS exit_trigger_reason text,
    ADD COLUMN IF NOT EXISTS realized_net_profit numeric;

ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_status_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_check;
ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_status_check
    CHECK (status IN ('OPEN', 'TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'PROFIT_EXIT', 'EXPIRED',
                      'INVALIDATED', 'AMBIGUOUS_INTRABAR'));
ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_shape_check;
ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_shape_check CHECK (
        (status = 'OPEN' AND realized_r IS NULL AND exit_timestamp IS NULL)
        OR
        (status IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'PROFIT_EXIT', 'AMBIGUOUS_INTRABAR')
            AND realized_r IS NOT NULL AND exit_timestamp IS NOT NULL)
        OR
        (status IN ('EXPIRED', 'INVALIDATED') AND realized_r IS NULL AND exit_timestamp IS NOT NULL)
    );

ALTER TABLE trade_management.managed_trade_lifecycle_event
    DROP CONSTRAINT IF EXISTS managed_trade_lifecycle_event_strategy_outcome_check;
ALTER TABLE trade_management.managed_trade_lifecycle_event
    ADD CONSTRAINT managed_trade_lifecycle_event_strategy_outcome_check
    CHECK (strategy_outcome IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'PROFIT_EXIT'));

INSERT INTO trade_management.trade_manager_version
    (tm_version_id, evaluator_id, label, manifest, manifest_hash, status)
VALUES (
    'TMV_f94068d2b525b265025b531e',
    'tm-exit-policy.v1',
    'TM-EXIT-POLICY-1',
    '{"action_vocabulary_version":"tm-actions.v1","arithmetic":{"net_profit":"broker_valuation_at_execution_boundary","pips":"executable_price_movement / captured_pip_size","r":"executable_price_movement / immutable_initial_risk"},"code_manifest":{"trade_management/tm_profit_exit.py":"computed-at-registration"},"decision_schema_version":"trade-manager-decision.v1","evaluator_id":"tm-exit-policy.v1","manifest_schema":"tm-version-manifest.v1","observation_spec":{"late_event_rule":"REJECT_STALE_QUOTE","quote_required":true,"timeframes_consumed":[]},"policy_bundle":["precedence=TIME_EXIT>NET_PROFIT_USD>PROFIT_R>PROFIT_PIPS"],"price_semantics":{"close_side":"bid_for_long_ask_for_short","version":"ps.v1"},"reason_code_registry_version":"tm-reasons.v1","resolution_table":[],"scope":{"instruments":["*"],"strategies":["*"]}}'::jsonb,
    'f94068d2b525b265025b531e2915ef3ed7c8c5043d6a7421651071e756210177',
    'FROZEN'
)
ON CONFLICT (tm_version_id) DO NOTHING;

INSERT INTO trade_management.tm_version_promotion
    (tm_version_id, publication_eligibility, decided_by, evidence_ref)
VALUES ('TMV_f94068d2b525b265025b531e', 'PUBLISHABLE', 'migration:043_trade_manager_profit_exit_policies',
        'Optional reduce-only exit policy; broker valuation remains execution-v2 gated')
ON CONFLICT DO NOTHING;

-- Existing Context trades keep TM-CONTEXT-TIME-EXIT-1.  This later binding applies the generic
-- evaluator only to Context trades created after this migration.
INSERT INTO trade_management.legacy_stream_binding
    (binding_id, strategy_id, strategy_instance_id, instrument, tm_version_id, valid_from, binding_hash)
VALUES ('BIND_context_exit_policy_v1', 'CONTEXT_STRUCTURE_RETRACE_V1', NULL, NULL,
        'TMV_f94068d2b525b265025b531e', now(), 'BIND_context_exit_policy_v1')
ON CONFLICT DO NOTHING;
