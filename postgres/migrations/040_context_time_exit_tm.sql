-- Context time exits are Trade Manager decisions, not strategy entry/stop/target logic.
-- The policy is captured per managed trade so later instance-policy edits do not rewrite
-- deadlines for already-open trades.

ALTER TABLE trade_management.managed_trade
    ADD COLUMN IF NOT EXISTS time_exit_minutes integer,
    ADD COLUMN IF NOT EXISTS time_exit_at timestamptz;

ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_status_check,
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_check;
ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_status_check
    CHECK (status IN ('OPEN', 'TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'EXPIRED', 'INVALIDATED',
                      'AMBIGUOUS_INTRABAR'));
ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_source_check;
ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_source_check
    CHECK (source IN ('CONTEXT_STRUCTURE_RETRACE_V1', 'LIQUIDITY_DISPLACEMENT_SCALP_V1'));
ALTER TABLE strategy.entry_signal_outcomes
    DROP CONSTRAINT IF EXISTS entry_signal_outcomes_shape_check;
ALTER TABLE strategy.entry_signal_outcomes
    ADD CONSTRAINT entry_signal_outcomes_shape_check
    CHECK (
        (status = 'OPEN' AND realized_r IS NULL AND exit_timestamp IS NULL)
        OR
        (status IN ('TARGET_HIT', 'STOPPED', 'TIME_EXIT', 'AMBIGUOUS_INTRABAR')
            AND realized_r IS NOT NULL AND exit_timestamp IS NOT NULL)
        OR
        (status IN ('EXPIRED', 'INVALIDATED') AND realized_r IS NULL AND exit_timestamp IS NOT NULL)
    );

INSERT INTO trade_management.trade_manager_version
    (tm_version_id, evaluator_id, label, manifest, manifest_hash, status)
VALUES (
    'TMV_8a8689394fbfc7d1db87f573',
    'tm-context-time-exit.v1',
    'TM-CONTEXT-TIME-EXIT-1',
    '{"action_vocabulary_version":"tm-actions.v1","arithmetic":{"deadline":"filled_at + captured_time_exit_minutes"},"code_manifest":{"trade_management/tm_time_exit.py":"computed-at-registration"},"decision_schema_version":"trade-manager-decision.v1","evaluator_id":"tm-context-time-exit.v1","manifest_schema":"tm-version-manifest.v1","observation_spec":{"late_event_rule":"EVALUATE_AT_EFFECTIVE_TIME","quote_required":false,"timeframes_consumed":[]},"policy_bundle":["deadline_source=managed_trade.time_exit_at"],"price_semantics":{"version":"not_used_for_deadline"},"reason_code_registry_version":"tm-reasons.v1","resolution_table":[],"scope":{"instruments":["*"],"strategies":["CONTEXT_STRUCTURE_RETRACE_V1"]}}'::jsonb,
    '8a8689394fbfc7d1db87f57368c64847771c1d29f73e1501ed6934d50ac54985',
    'FROZEN'
)
ON CONFLICT (tm_version_id) DO NOTHING;

INSERT INTO trade_management.tm_version_promotion
    (tm_version_id, publication_eligibility, decided_by, evidence_ref)
VALUES ('TMV_8a8689394fbfc7d1db87f573', 'PUBLISHABLE', 'migration:040_context_time_exit_tm',
        'Context time exit is a reduce-only close through execution_v2; no entry or sizing authority')
ON CONFLICT DO NOTHING;

-- Bind only future Context managed trades. Existing trades retain their immutable TM binding.
INSERT INTO trade_management.legacy_stream_binding
    (binding_id, strategy_id, strategy_instance_id, instrument, tm_version_id, valid_from, binding_hash)
VALUES ('BIND_context_time_exit_v1', 'CONTEXT_STRUCTURE_RETRACE_V1', NULL, NULL,
        'TMV_8a8689394fbfc7d1db87f573', now(), 'BIND_context_time_exit_v1')
ON CONFLICT DO NOTHING;
