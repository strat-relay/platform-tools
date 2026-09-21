CREATE TABLE IF NOT EXISTS strategy.phase6_runners (
    strategy_id text PRIMARY KEY, strategy_version text NOT NULL, schema_version text NOT NULL,
    manifest_ref text, freeze_timestamp timestamptz, configuration_hash text,
    phase2_representation_hash text, runner_status text, prospective_boundary jsonb,
    kill_switch boolean, created_at timestamptz, last_poll_at timestamptz,
    last_successful_read_at timestamptz, poll_interval_seconds numeric,
    source_state_sha256 text, source_manifest_sha256 text, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy.phase6_symbol_progress (
    strategy_id text NOT NULL REFERENCES strategy.phase6_runners(strategy_id), symbol text NOT NULL,
    last_m5 text, last_m15 text, initialized boolean, last_candle jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(strategy_id, symbol)
);
CREATE TABLE IF NOT EXISTS strategy.phase6_setups (
    setup_id text PRIMARY KEY, strategy_id text NOT NULL, market_event_id text, symbol text NOT NULL,
    direction text, pattern text, setup_timestamp numeric, setup_timestamp_iso text,
    qualification text, qualification_flags jsonb, retrace_state text, entry_level numeric,
    theoretical_entry numeric, spread_at_detection numeric, target_completed boolean,
    status text, m5_start_index integer, zone_left boolean, thesis_invalidated boolean,
    event_bar jsonb NOT NULL DEFAULT '{}'::jsonb, compact_geometry jsonb NOT NULL DEFAULT '{}'::jsonb,
    provenance jsonb NOT NULL DEFAULT '{}'::jsonb, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy.phase6_setup_lifecycle (
    event_id text PRIMARY KEY, setup_id text, event_time text, event_type text NOT NULL,
    source text, payload jsonb NOT NULL, imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy.phase6_entry_opportunities (
    entry_opportunity_id text PRIMARY KEY, setup_id text NOT NULL REFERENCES strategy.phase6_setups(setup_id),
    entry_attempt_id text, economic_position_id text, fill_timestamp numeric, fill_timestamp_iso text,
    fill_candle_number integer, entry_mechanisms jsonb, theoretical_entry numeric,
    executable_paper_entry numeric, spread_at_fill numeric, stop numeric, target numeric,
    status text, mfe_price numeric, mae_price numeric, reentry_type text, exit_timestamp numeric,
    exit_reason text, realized_r numeric, symbol text, direction text, pattern text,
    provenance jsonb, compact_geometry jsonb, leg_a jsonb, leg_b jsonb, updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(entry_attempt_id)
);
CREATE TABLE IF NOT EXISTS strategy.phase6_economic_positions (
    economic_position_id text PRIMARY KEY, entry_opportunity_id text NOT NULL REFERENCES strategy.phase6_entry_opportunities(entry_opportunity_id),
    setup_id text NOT NULL REFERENCES strategy.phase6_setups(setup_id), status text,
    stop numeric, target numeric, mfe_price numeric, mae_price numeric, realized_r numeric,
    fill_timestamp numeric, fill_timestamp_iso text, exit_timestamp numeric, exit_reason text,
    reentry_type text, provenance jsonb, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy.phase6_position_lifecycle (
    event_id text PRIMARY KEY, economic_position_id text, event_time text, event_type text NOT NULL,
    source text, payload jsonb NOT NULL, imported_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research.phase6_observations (
    observation_id text PRIMARY KEY, content_sha256 text NOT NULL UNIQUE, kind text NOT NULL,
    source_file text, source_line bigint, event_id text, snapshot_ref text, observed_at text,
    symbol text, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS strategy.phase6_setup_observation_refs (
    setup_id text NOT NULL REFERENCES strategy.phase6_setups(setup_id),
    observation_id text NOT NULL REFERENCES research.phase6_observations(observation_id),
    role text NOT NULL, PRIMARY KEY(setup_id, observation_id, role)
);
CREATE INDEX IF NOT EXISTS phase6_setups_symbol_status_idx ON strategy.phase6_setups(symbol, status);
CREATE INDEX IF NOT EXISTS phase6_observations_kind_idx ON research.phase6_observations(kind);
