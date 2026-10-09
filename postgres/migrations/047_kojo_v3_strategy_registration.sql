-- KOJO_STRUCTURE_RECLAIM_V3 — first-class managed strategy registration.
--
-- Registers:
--   strategy_mgmt.strategy_definition  — KOJO family
--   strategy_mgmt.parameter_schema     — 18-parameter V3 schema
--   strategy_mgmt.strategy_version     — KOJO_STRUCTURE_RECLAIM@V3 (lifecycle=IMPLEMENTED)
--   strategy_mgmt.parameter_set        — "Kojo V3 Default" (frozen, parity with c82d290)
--   strategy_mgmt.strategy_instance_v2 — "Kojo V3 Forward" (online=TRUE, execution_mode=SHADOW)
--   platform.strategy_definition       — runtime routing entry (enabled=true, shadow only)
--
-- All inserts are idempotent (ON CONFLICT DO NOTHING).
-- Initial forward state: runtime_state=ONLINE, execution_mode=SHADOW.
-- BROKER_WRITES = 0.  EXECUTION_ELIGIBLE remains false.
-- LIVE transition requires operator action via Control API after explicit confirmation.
-- Do NOT activate LIVE execution in this migration.

-- ─── 1. Strategy Definition ────────────────────────────────────────────────────

INSERT INTO strategy_mgmt.strategy_definition
    (id, name, family_key, description, provenance_notes, created_by)
VALUES (
    '7f3a9c10-b4e2-4d8a-9c15-2e6f8b3a1d05',
    'Kojo Structure Reclaim',
    'KOJO',
    'Trend-aligned H1 structural close followed by M15 retest/rejection, '
    'with structural stop, current-day M15 reaction-zone TP1, and liquidity TP2.',
    'V3 evaluator at commit c82d290. SOURCE_FIDELITY_BLOCKED=false. '
    'Research commits: post-entry=1e321fe, counterfactual=d9264a7, anatomy=827a95f, '
    'geometry=3351fb3, M5-micro=afc1cca, M5-retest=5d18892, lower-TF=35dde76, M1=3566693.',
    'migration_047'
)
ON CONFLICT (family_key) DO NOTHING;

-- ─── 2. Parameter Schema ───────────────────────────────────────────────────────

INSERT INTO strategy_mgmt.parameter_schema
    (schema_id, fields, created_by)
VALUES (
    'kojo-structure-reclaim-v3',
    '{
        "context_timeframe":        {"required":true,"type":"enum","enum":["H1"],"default":"H1","display_name":"Context Timeframe","description":"Timeframe for H1 structural break detection (semantic invariant; H1 only in V3)","category":"TIMEFRAMES","restart_required":true,"research_only":false,"production_allowed":true},
        "confirmation_timeframe":   {"required":true,"type":"enum","enum":["M15"],"default":"M15","display_name":"Confirmation Timeframe","description":"Timeframe for retest and rejection confirmation (semantic invariant; M15 only in V3)","category":"TIMEFRAMES","restart_required":true,"research_only":false,"production_allowed":true},
        "execution_timeframe":      {"required":true,"type":"enum","enum":["M5","M15"],"default":"M5","display_name":"Execution Timeframe","description":"Reference timeframe for live entry monitoring; V3 entries fire on M15 open","category":"TIMEFRAMES","restart_required":true,"research_only":false,"production_allowed":true},
        "pivot_strength":           {"required":true,"type":"int","minimum":1,"maximum":10,"step":1,"default":2,"display_name":"Pivot Strength","description":"Number of H1 bars on each side required to confirm a swing pivot","category":"STRUCTURE","restart_required":false,"research_only":false,"production_allowed":true},
        "stop_buffer_type":         {"required":true,"type":"enum","enum":["PRICE"],"default":"PRICE","display_name":"Stop Buffer Type","description":"How the stop buffer beyond the structural extreme is measured (PRICE only in V3)","category":"STRUCTURE","restart_required":false,"research_only":false,"production_allowed":true},
        "stop_buffer_value":        {"required":true,"type":"float","minimum":0.0,"maximum":1000.0,"step":0.5,"default":1.0,"display_name":"Stop Buffer (price units)","description":"Price units beyond the M15 retest zone swing extreme for stop placement","category":"STRUCTURE","restart_required":false,"research_only":false,"production_allowed":true},
        "retest_tolerance_atr":     {"required":true,"type":"float","minimum":0.1,"maximum":3.0,"step":0.05,"default":0.5,"display_name":"Retest Tolerance (ATR fraction)","description":"Fraction of ATR within which price must approach the structural level to count as retest","category":"CONFIRMATION","restart_required":false,"research_only":false,"production_allowed":true},
        "max_retest_wait_h1_bars":  {"required":true,"type":"int","minimum":4,"maximum":100,"step":1,"default":24,"display_name":"Max Retest Wait (H1 bars)","description":"Maximum H1 bars to wait for M15 retest after structural break before expiry","category":"LIFECYCLE","restart_required":false,"research_only":false,"production_allowed":true},
        "max_confirmation_wait_m15_bars": {"required":true,"type":"int","minimum":4,"maximum":100,"step":1,"default":16,"display_name":"Max Confirmation Wait (M15 bars)","description":"Maximum M15 bars in retest zone before expiry if no strong rejection forms","category":"LIFECYCLE","restart_required":false,"research_only":false,"production_allowed":true},
        "trade_management_mode":    {"required":true,"type":"enum","enum":["OBSERVE"],"default":"OBSERVE","display_name":"Trade Management Mode","description":"Post-entry trade management policy; OBSERVE means no automated management interventions","category":"LIFECYCLE","restart_required":false,"research_only":false,"production_allowed":true},
        "allow_wick_rejection":     {"required":true,"type":"bool","default":true,"display_name":"Allow Wick Rejection","description":"Count M15 bars with an upper/lower wick >= 33% of range as TP1 reaction evidence","category":"ENTRY","restart_required":false,"research_only":false,"production_allowed":true},
        "allow_body_close_rejection": {"required":true,"type":"bool","default":true,"display_name":"Allow Body-Close Rejection","description":"Count M15 bars with a bearish/bullish body >= 25% of range as TP1 reaction evidence","category":"ENTRY","restart_required":false,"research_only":false,"production_allowed":true},
        "entry_type":               {"required":true,"type":"enum","enum":["MARKET"],"default":"MARKET","display_name":"Entry Type","description":"Order type for entry signal (MARKET only in V3)","category":"ENTRY","restart_required":false,"research_only":false,"production_allowed":true},
        "simple_pullback_enabled":  {"required":true,"type":"bool","default":true,"display_name":"Simple Pullback Enabled","description":"Accept single-swing M15 retest patterns (V3 scaffold; all pullbacks treated equally)","category":"PULLBACK","restart_required":false,"research_only":false,"production_allowed":true},
        "complex_pullback_enabled": {"required":true,"type":"bool","default":true,"display_name":"Complex Pullback Enabled","description":"Accept multi-swing M15 retest patterns (V3 scaffold; all pullbacks treated equally)","category":"PULLBACK","restart_required":false,"research_only":false,"production_allowed":true},
        "tp1_min_rr":               {"required":true,"type":"float","minimum":0.5,"maximum":5.0,"step":0.25,"default":1.0,"display_name":"TP1 Minimum R:R","description":"Minimum planned reward:risk for a reaction zone to qualify as TP1 (source-explicit default 1.0R)","category":"TARGETS","restart_required":false,"research_only":false,"production_allowed":true},
        "reaction_lookback_scope":  {"required":true,"type":"enum","enum":["CURRENT_TRADING_DAY"],"default":"CURRENT_TRADING_DAY","display_name":"Reaction Lookback Scope","description":"UTC-day window to scan for M15 reaction zone evidence for TP1","category":"TARGETS","restart_required":false,"research_only":false,"production_allowed":true},
        "tp2_selection_policy":     {"required":true,"type":"enum","enum":["NEAREST_VALID_EXTERNAL_LIQUIDITY"],"default":"NEAREST_VALID_EXTERNAL_LIQUIDITY","display_name":"TP2 Selection Policy","description":"Policy for choosing TP2 from external liquidity objectives beyond TP1","category":"TARGETS","restart_required":false,"research_only":false,"production_allowed":true}
    }'::jsonb,
    'migration_047'
)
ON CONFLICT (schema_id) DO NOTHING;

-- ─── 3. Strategy Version ───────────────────────────────────────────────────────

INSERT INTO strategy_mgmt.strategy_version
    (id, definition_id, version_label, evaluator_key, lifecycle, schema_id, release_notes, created_by)
VALUES (
    'a2b4c6d8-e0f2-4a6c-8e0a-2c4e6f8a0b2d',
    '7f3a9c10-b4e2-4d8a-9c15-2e6f8b3a1d05',  -- KOJO definition id
    'V3',
    'kojo_structure_reclaim_v3',
    'IMPLEMENTED',
    'kojo-structure-reclaim-v3',
    'V3 corrects 4 V2 semantic defects: opportunity retirement (CONSUMED-only permanent retirement), '
    'H1 confirmation source rule (break bar close IS the H1 confirmation), '
    'TP1 current-day M15 reaction zone (planned_r >= 1.0R), '
    'TP2 nearest valid external liquidity objective. '
    'Evaluator commit c82d290. SOURCE_FIDELITY_BLOCKED=false. '
    'ENTRY_SEMANTICS_READY=true. TARGET_SEMANTICS_READY=true. '
    'TRADE_MANAGEMENT_POLICY_INCLUDED=false. BROKER_WRITES=0.',
    'migration_047'
)
ON CONFLICT (definition_id, version_label) DO NOTHING;

-- ─── 4. Default Parameter Set ──────────────────────────────────────────────────
-- Fingerprint: b1228ba7513d41e23e503f9b770a222c8349a17c0b7348e53e3a87bcc4f751aa
-- Computed as SHA-256 of canonical_json(ParameterSet.canonical_payload()) with sort_keys=True.
-- Verified by test_kojo_v3_managed.py::test_default_parameter_set_fingerprint.

INSERT INTO strategy_mgmt.parameter_set
    (id, parameter_set_id, strategy_version_id, schema_id, values, fingerprint, frozen, frozen_at, frozen_by, provenance, created_by)
VALUES (
    'c3d5e7f9-a1b3-4c5d-9e1f-3a5c7e9b1d3f',
    'kojo-v3-default',
    'a2b4c6d8-e0f2-4a6c-8e0a-2c4e6f8a0b2d',  -- V3 version id
    'kojo-structure-reclaim-v3',
    '{
        "context_timeframe": "H1",
        "confirmation_timeframe": "M15",
        "execution_timeframe": "M5",
        "pivot_strength": 2,
        "stop_buffer_type": "PRICE",
        "stop_buffer_value": 1.0,
        "retest_tolerance_atr": 0.5,
        "max_retest_wait_h1_bars": 24,
        "max_confirmation_wait_m15_bars": 16,
        "trade_management_mode": "OBSERVE",
        "allow_wick_rejection": true,
        "allow_body_close_rejection": true,
        "entry_type": "MARKET",
        "simple_pullback_enabled": true,
        "complex_pullback_enabled": true,
        "tp1_min_rr": 1.0,
        "reaction_lookback_scope": "CURRENT_TRADING_DAY",
        "tp2_selection_policy": "NEAREST_VALID_EXTERNAL_LIQUIDITY"
    }'::jsonb,
    'b1228ba7513d41e23e503f9b770a222c8349a17c0b7348e53e3a87bcc4f751aa',
    true,
    now(),
    'migration_047',
    '{
        "baseline_rationale": "Default parameter set for KOJO_STRUCTURE_RECLAIM_V3 managed strategy instance. Values reproduce c82d290 V3 evaluator behavior exactly. PARAMETER_SEARCH=false.",
        "created_by": "migration_043",
        "source": "MANAGED_STRATEGY_DEFAULT",
        "strategy": "KOJO_STRUCTURE_RECLAIM_V3"
    }'::jsonb,
    'migration_047'
)
ON CONFLICT (parameter_set_id) DO NOTHING;

-- ─── 5. Strategy Instance ──────────────────────────────────────────────────────
-- Initial forward state: ONLINE + SHADOW.
--   online=true          → runtime_state=ONLINE; evaluator generates signals immediately.
--   execution_mode=SHADOW → signals are prospective/observed; no broker orders.
--   execution_eligible=false → never changed here; execution authority is separate.
--
-- LIVE transition requires explicit operator action via Control API after confirmation.
-- Migration 046 adds the execution_mode and execution_mode_revision columns.

INSERT INTO strategy_mgmt.strategy_instance_v2
    (id, strategy_version_id, parameter_set_id, display_name, online, execution_eligible,
     execution_mode, instruments, attributes, created_by)
VALUES (
    'e5f7a9b1-c3d5-4e7f-a1b3-5c7e9f1b3d5e',
    'a2b4c6d8-e0f2-4a6c-8e0a-2c4e6f8a0b2d',  -- V3 version id
    'c3d5e7f9-a1b3-4c5d-9e1f-3a5c7e9b1d3f',  -- default parameter set id
    'Kojo V3 Forward',
    true,       -- ONLINE; evaluator generates signals for forward cohort
    false,      -- execution_eligible NEVER changed here
    'SHADOW',   -- execution_mode=SHADOW; signals observed, no broker orders
    '[{"canonical_instrument": "XAUUSDm"}]'::jsonb,
    '{
        "shadow_only": true,
        "broker_writes": 0,
        "forward_testing": true,
        "source_fidelity_blocked": false,
        "ready_for_discovery": true,
        "ready_for_shadow_signals": true,
        "production_eligible": false
    }'::jsonb,
    'migration_047'
)
ON CONFLICT DO NOTHING;

-- ─── 6. Platform strategy_definition entry (runtime routing) ──────────────────
-- Registers V3 in the platform schema so the signal orchestrator can discover
-- the managed strategy instance.  Starts enabled=true to match instance ONLINE state.
-- execution_mode is governed by strategy_instance_v2.execution_mode, not this flag.

INSERT INTO platform.strategy_definition
    (strategy_id, strategy_version, display_name, description, adapter,
     enabled, routes, trade_management, attributes)
VALUES (
    'KOJO_STRUCTURE_RECLAIM_V3',
    'V3',
    'Kojo Structure Reclaim V3',
    'Trend-aligned H1 structural close followed by M15 retest/rejection, '
    'with structural stop, current-day M15 reaction-zone TP1, and liquidity TP2.',
    'KojoStructureReclaimV3Adapter',
    true,    -- enabled=true; instance is ONLINE from initial state
    '{"audit": true, "shadow_execution": false, "distribution_queue": true}'::jsonb,
    '{"mode": "OBSERVE", "broker_writes": 0}'::jsonb,
    '{"shadow_only": true, "source_fidelity_blocked": false, "execution_mode": "SHADOW"}'::jsonb
)
ON CONFLICT (strategy_id) DO NOTHING;

-- Grants (mirrors migration 042 pattern)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON
            strategy_mgmt.strategy_definition,
            strategy_mgmt.strategy_version,
            strategy_mgmt.parameter_schema,
            strategy_mgmt.parameter_set,
            strategy_mgmt.strategy_instance_v2
        TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON
            strategy_mgmt.strategy_definition,
            strategy_mgmt.strategy_version,
            strategy_mgmt.parameter_schema,
            strategy_mgmt.parameter_set,
            strategy_mgmt.strategy_instance_v2
        TO trading_readonly;
    END IF;
END
$$;
