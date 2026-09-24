-- Canonical operator-controlled V2 execution authority. This is deliberately a
-- singleton control plane; per-account authority is deferred.
CREATE TABLE IF NOT EXISTS execution_v2.execution_authority (
    authority_id text PRIMARY KEY DEFAULT 'current',
    state text NOT NULL CHECK (state IN ('DISABLED', 'ENABLED')),
    revision bigint NOT NULL CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text,
    source text NOT NULL DEFAULT 'POSTGRES',
    CONSTRAINT execution_authority_singleton CHECK (authority_id = 'current')
);

CREATE TABLE IF NOT EXISTS execution_v2.execution_authority_change (
    change_id text PRIMARY KEY,
    authority_id text NOT NULL REFERENCES execution_v2.execution_authority(authority_id),
    previous_state text CHECK (previous_state IS NULL OR previous_state IN ('DISABLED', 'ENABLED')),
    new_state text NOT NULL CHECK (new_state IN ('DISABLED', 'ENABLED')),
    previous_revision bigint,
    new_revision bigint NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT now(),
    changed_by text,
    source text NOT NULL DEFAULT 'POSTGRES',
    preflight jsonb NOT NULL DEFAULT '{}'::jsonb
);

INSERT INTO execution_v2.execution_authority
    (authority_id, state, revision, updated_by, source)
VALUES ('current', 'DISABLED', 1, 'migration:020', 'POSTGRES')
ON CONFLICT (authority_id) DO NOTHING;
