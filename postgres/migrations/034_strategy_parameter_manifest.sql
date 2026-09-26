-- Machine-readable strategy parameter manifests, published from strategy code.
--
-- A StrategyVersion owns its parameter schema (keys, types, units, bounds, classification) and its
-- immutable semantics; a StrategyInstance runs one frozen ParameterSet identified by
-- parameter_set_id + config_fingerprint. Both are published from the code that runtimes execute
-- (scripts/publish_strategy_manifests.py), never authored in place: editing a parameter means a
-- new ParameterSet, so rows here are replaced only by republishing, and a changed ParameterSet
-- must carry a new parameter_set_id / config_fingerprint.
CREATE TABLE IF NOT EXISTS platform.strategy_version_manifest (
    strategy_id text NOT NULL REFERENCES platform.strategy_definition(strategy_id),
    strategy_version text NOT NULL,
    manifest jsonb NOT NULL,
    manifest_fingerprint text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    published_by text NOT NULL,
    PRIMARY KEY (strategy_id, strategy_version),
    CHECK (jsonb_typeof(manifest) = 'object'),
    CHECK (jsonb_typeof(manifest -> 'parameters') = 'array')
);

CREATE TABLE IF NOT EXISTS platform.strategy_instance_parameter_set (
    instance_id text PRIMARY KEY REFERENCES platform.strategy_instance(instance_id),
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    parameter_set_id text NOT NULL CHECK (parameter_set_id <> ''),
    config_fingerprint text NOT NULL CHECK (config_fingerprint <> ''),
    parameters jsonb NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    published_by text NOT NULL,
    FOREIGN KEY (strategy_id, instance_id) REFERENCES platform.strategy_instance(strategy_id, instance_id),
    FOREIGN KEY (strategy_id, strategy_version)
        REFERENCES platform.strategy_version_manifest(strategy_id, strategy_version),
    CHECK (jsonb_typeof(parameters) = 'object')
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_app') THEN
        GRANT SELECT, INSERT, UPDATE ON platform.strategy_version_manifest,
                                        platform.strategy_instance_parameter_set TO trading_app;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'trading_readonly') THEN
        GRANT SELECT ON platform.strategy_version_manifest, platform.strategy_instance_parameter_set
            TO trading_readonly;
    END IF;
END
$$;
