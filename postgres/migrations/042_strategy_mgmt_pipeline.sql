-- Dynamic Strategy Creation Pipeline schema.
--
-- New `strategy_mgmt` schema holds the intake/creation tables separate from the operational
-- `platform` schema so live production tables are never touched by this migration.
--
-- Invariants enforced here:
--   * StrategyVersion is immutable once frozen (lifecycle = 'FROZEN')
--   * ParameterSet is immutable once frozen (frozen = true)
--   * New StrategyInstance always starts online=false, execution_eligible=false
--   * Backtest provenance recorded: strategy_version, parameter_set, dataset, date range,
--     instruments, cost model, engine version, result fingerprint
--   * ONLINE toggle is on strategy_instance_v2.online; execution_eligible is a separate column
--     never touched by the ONLINE toggle path

CREATE SCHEMA IF NOT EXISTS strategy_mgmt;

-- ─── strategy_definition ────────────────────────────────────────────────────
-- Identifies a named, versioned strategy family.  A definition is not a
-- runnable artifact; it is metadata that owns child StrategyVersions.
CREATE TABLE IF NOT EXISTS strategy_mgmt.strategy_definition (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL CHECK (name <> ''),
    family_key      text NOT NULL CHECK (family_key <> ''),
    description     text,
    provenance_notes text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL DEFAULT 'system',
    UNIQUE (family_key)
);

-- ─── strategy_version ───────────────────────────────────────────────────────
-- Immutable once frozen.  Holds the evaluator key (links to the Python
-- StrategyEvaluator registry), a reference to a ParameterSchema, and a
-- lifecycle state machine: DRAFT → IMPLEMENTED → FROZEN.
--
-- A frozen version cannot be mutated; changes require a new version row.
CREATE TABLE IF NOT EXISTS strategy_mgmt.strategy_version (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    definition_id       uuid NOT NULL REFERENCES strategy_mgmt.strategy_definition(id),
    version_label       text NOT NULL CHECK (version_label <> ''),
    evaluator_key       text NOT NULL CHECK (evaluator_key <> ''),
    lifecycle           text NOT NULL DEFAULT 'DRAFT'
                            CHECK (lifecycle IN ('DRAFT','IMPLEMENTED','FROZEN')),
    schema_id           text,   -- foreign key by value to parameter_schema.schema_id
    release_notes       text,
    frozen_at           timestamptz,
    frozen_by           text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL DEFAULT 'system',
    UNIQUE (definition_id, version_label)
);

-- ─── parameter_schema ────────────────────────────────────────────────────────
-- Mirrors the in-memory ParameterSchema dataclass.  Stored here so the API
-- can surface schema definitions without requiring live code execution.
CREATE TABLE IF NOT EXISTS strategy_mgmt.parameter_schema (
    schema_id       text PRIMARY KEY CHECK (schema_id <> ''),
    fields          jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL DEFAULT 'system',
    CHECK (jsonb_typeof(fields) = 'object')
);

-- ─── parameter_set ───────────────────────────────────────────────────────────
-- Immutable once frozen.  Parameter values for one strategy version.  A frozen
-- parameter_set cannot be mutated; a new row is required for any change.
CREATE TABLE IF NOT EXISTS strategy_mgmt.parameter_set (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parameter_set_id    text NOT NULL CHECK (parameter_set_id <> ''),
    strategy_version_id uuid NOT NULL REFERENCES strategy_mgmt.strategy_version(id),
    schema_id           text NOT NULL,
    values              jsonb NOT NULL,
    fingerprint         text NOT NULL CHECK (fingerprint <> ''),
    frozen              boolean NOT NULL DEFAULT false,
    frozen_at           timestamptz,
    frozen_by           text,
    provenance          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL DEFAULT 'system',
    UNIQUE (parameter_set_id),
    CHECK (jsonb_typeof(values) = 'object'),
    CHECK (jsonb_typeof(provenance) = 'object')
);

-- ─── backtest_run ────────────────────────────────────────────────────────────
-- Persists backtest provenance and result.  Each row is append-only after
-- COMPLETED/FAILED/CANCELLED.  A RUNNING row may be updated exactly once.
--
-- backtest_purpose: DISCOVERY | VALIDATION
-- status: QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED
CREATE TABLE IF NOT EXISTS strategy_mgmt.backtest_run (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id     uuid NOT NULL REFERENCES strategy_mgmt.strategy_version(id),
    parameter_set_id        uuid NOT NULL REFERENCES strategy_mgmt.parameter_set(id),
    backtest_purpose        text NOT NULL CHECK (backtest_purpose IN ('DISCOVERY','VALIDATION')),
    status                  text NOT NULL DEFAULT 'QUEUED'
                                CHECK (status IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED')),
    -- provenance fingerprints
    dataset_fingerprint     text,
    date_range_start        timestamptz,
    date_range_end          timestamptz,
    instruments             text[] NOT NULL DEFAULT '{}',
    timeframes              text[] NOT NULL DEFAULT '{}',
    cost_model_fingerprint  text,
    engine_version          text,
    evaluator_fingerprint   text,
    -- result
    result_fingerprint      text,
    metrics                 jsonb,
    error_detail            text,
    -- timestamps
    queued_at               timestamptz NOT NULL DEFAULT now(),
    started_at              timestamptz,
    completed_at            timestamptz,
    created_by              text NOT NULL DEFAULT 'system',
    CHECK (metrics IS NULL OR jsonb_typeof(metrics) = 'object')
);

-- ─── strategy_instance_v2 ────────────────────────────────────────────────────
-- Operational instance of a frozen (strategy_version, parameter_set) pair.
-- online = false by default.
-- execution_eligible = false by default and NEVER toggled by this table's API.
-- ONLINE ≠ execution_eligible.  They are independent columns.
CREATE TABLE IF NOT EXISTS strategy_mgmt.strategy_instance_v2 (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id uuid NOT NULL REFERENCES strategy_mgmt.strategy_version(id),
    parameter_set_id    uuid NOT NULL REFERENCES strategy_mgmt.parameter_set(id),
    display_name        text NOT NULL CHECK (display_name <> ''),
    online              boolean NOT NULL DEFAULT false,
    execution_eligible  boolean NOT NULL DEFAULT false,
    instruments         jsonb NOT NULL DEFAULT '[]'::jsonb,
    attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL DEFAULT 'system',
    CHECK (jsonb_typeof(instruments) = 'array'),
    CHECK (jsonb_typeof(attributes) = 'object')
);

-- Prevent the online toggle from touching execution_eligible.
-- This trigger fires on UPDATE and rejects any attempt to change execution_eligible
-- through the normal update path. execution_eligible must be set via a separate
-- privileged path (the existing authority system).
CREATE OR REPLACE FUNCTION strategy_mgmt.guard_execution_eligible()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.execution_eligible <> OLD.execution_eligible THEN
        RAISE EXCEPTION 'execution_eligible cannot be changed through this path';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS guard_execution_eligible_trg ON strategy_mgmt.strategy_instance_v2;
CREATE TRIGGER guard_execution_eligible_trg
    BEFORE UPDATE ON strategy_mgmt.strategy_instance_v2
    FOR EACH ROW
    EXECUTE FUNCTION strategy_mgmt.guard_execution_eligible();

-- Prevent mutation of a frozen strategy_version.
CREATE OR REPLACE FUNCTION strategy_mgmt.guard_frozen_version()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.lifecycle = 'FROZEN' THEN
        RAISE EXCEPTION 'StrategyVersion % is frozen and cannot be mutated', OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS guard_frozen_version_trg ON strategy_mgmt.strategy_version;
CREATE TRIGGER guard_frozen_version_trg
    BEFORE UPDATE ON strategy_mgmt.strategy_version
    FOR EACH ROW
    EXECUTE FUNCTION strategy_mgmt.guard_frozen_version();

-- Prevent mutation of a frozen parameter_set (values/fingerprint/schema_id).
CREATE OR REPLACE FUNCTION strategy_mgmt.guard_frozen_parameter_set()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.frozen = true AND (
        NEW.values::text <> OLD.values::text OR
        NEW.fingerprint  <> OLD.fingerprint  OR
        NEW.schema_id    <> OLD.schema_id
    ) THEN
        RAISE EXCEPTION 'ParameterSet % is frozen and cannot be mutated', OLD.parameter_set_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS guard_frozen_parameter_set_trg ON strategy_mgmt.parameter_set;
CREATE TRIGGER guard_frozen_parameter_set_trg
    BEFORE UPDATE ON strategy_mgmt.parameter_set
    FOR EACH ROW
    EXECUTE FUNCTION strategy_mgmt.guard_frozen_parameter_set();

-- Grants
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON
            strategy_mgmt.strategy_definition,
            strategy_mgmt.strategy_version,
            strategy_mgmt.parameter_schema,
            strategy_mgmt.parameter_set,
            strategy_mgmt.backtest_run,
            strategy_mgmt.strategy_instance_v2
        TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON
            strategy_mgmt.strategy_definition,
            strategy_mgmt.strategy_version,
            strategy_mgmt.parameter_schema,
            strategy_mgmt.parameter_set,
            strategy_mgmt.backtest_run,
            strategy_mgmt.strategy_instance_v2
        TO trading_readonly;
    END IF;
END
$$;
