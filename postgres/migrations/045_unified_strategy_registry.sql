-- Register the existing operational strategy catalog in strategy_mgmt.
--
-- The legacy platform tables remain intact for backward-compatible statistics and
-- signal history.  This migration copies their identity/configuration into the
-- unified registry without changing lifecycle or execution authority.

WITH legacy AS (
    SELECT d.strategy_id AS legacy_strategy_id,
           gen_random_uuid() AS definition_id
    FROM platform.strategy_definition d
    WHERE NOT EXISTS (
        SELECT 1 FROM strategy_mgmt.strategy_definition n
        WHERE n.family_key = d.strategy_id
    )
)
INSERT INTO strategy_mgmt.strategy_definition
    (id, name, family_key, description, provenance_notes, created_by)
SELECT legacy.definition_id, d.display_name, d.strategy_id, d.description,
       'Imported from platform.strategy_definition by migration 044; legacy identity preserved.',
       'migration:044'
FROM legacy
JOIN platform.strategy_definition d ON d.strategy_id = legacy.legacy_strategy_id
ON CONFLICT (family_key) DO NOTHING;

INSERT INTO strategy_mgmt.parameter_schema (schema_id, fields, created_by)
SELECT 'legacy:' || d.strategy_id || '@' || d.strategy_version,
       COALESCE(vm.manifest, '{}'::jsonb),
       'migration:044'
FROM platform.strategy_definition d
LEFT JOIN platform.strategy_version_manifest vm
  ON vm.strategy_id = d.strategy_id AND vm.strategy_version = d.strategy_version
WHERE EXISTS (
    SELECT 1 FROM strategy_mgmt.strategy_definition n WHERE n.family_key = d.strategy_id
)
ON CONFLICT (schema_id) DO NOTHING;

INSERT INTO strategy_mgmt.strategy_version
    (definition_id, version_label, evaluator_key, lifecycle, schema_id,
     release_notes, frozen_at, frozen_by, created_by)
SELECT n.id, d.strategy_version, COALESCE(d.adapter, d.strategy_id), 'FROZEN',
       'legacy:' || d.strategy_id || '@' || d.strategy_version,
       'Imported from the deployed legacy strategy registry.', now(), 'migration:044', 'migration:044'
FROM platform.strategy_definition d
JOIN strategy_mgmt.strategy_definition n ON n.family_key = d.strategy_id
WHERE NOT EXISTS (
    SELECT 1 FROM strategy_mgmt.strategy_version v
    WHERE v.definition_id = n.id AND v.version_label = d.strategy_version
);

INSERT INTO strategy_mgmt.parameter_set
    (parameter_set_id, strategy_version_id, schema_id, values, fingerprint,
     frozen, frozen_at, frozen_by, provenance, created_by)
SELECT DISTINCT ps.parameter_set_id, v.id,
       'legacy:' || ps.strategy_id || '@' || ps.strategy_version,
       ps.parameters, ps.config_fingerprint, true, ps.published_at,
       ps.published_by,
       jsonb_build_object('source', 'platform.strategy_instance_parameter_set',
                          'legacy_strategy_id', ps.strategy_id,
                          'legacy_instance_id', ps.instance_id),
       'migration:044'
FROM platform.strategy_instance_parameter_set ps
JOIN strategy_mgmt.strategy_definition n ON n.family_key = ps.strategy_id
JOIN strategy_mgmt.strategy_version v
  ON v.definition_id = n.id AND v.version_label = ps.strategy_version
WHERE NOT EXISTS (
    SELECT 1 FROM strategy_mgmt.parameter_set p
    WHERE p.parameter_set_id = ps.parameter_set_id
);

INSERT INTO strategy_mgmt.strategy_instance_v2
    (strategy_version_id, parameter_set_id, display_name, online,
     execution_eligible, instruments, attributes, created_by)
SELECT v.id, p.id, i.display_name, i.enabled,
       (
         EXISTS (
           SELECT 1
           FROM execution_v2.execution_authority a
           WHERE a.authority_id = 'current' AND a.state = 'ENABLED'
         )
         AND EXISTS (
           SELECT 1 FROM execution_v2.risk_policy rp
           WHERE rp.policy_id = 'current' AND rp.enabled = true
         )
         AND EXISTS (
           SELECT 1 FROM execution_v2.risk_policy_allowed_strategy ras
           WHERE ras.policy_id = 'current'
             AND ras.strategy_ref = d.strategy_id || '@' || d.strategy_version
         )
       ),
       COALESCE((
         SELECT jsonb_agg(m.canonical_instrument ORDER BY m.canonical_instrument)
         FROM strategy.instrument_membership m
         WHERE m.strategy_id = i.strategy_id
           AND m.strategy_instance_id = i.instance_id
           AND m.state = 'ACTIVE'
       ), '[]'::jsonb),
       jsonb_build_object(
         'legacy_strategy_id', i.strategy_id,
         'legacy_instance_id', i.instance_id,
         'legacy_revision', i.revision,
         'legacy_attributes', i.attributes,
         'execution_authority_source', 'execution_v2'
       ),
       'migration:044'
FROM platform.strategy_instance i
JOIN platform.strategy_definition d ON d.strategy_id = i.strategy_id
JOIN platform.strategy_instance_parameter_set ps ON ps.instance_id = i.instance_id
JOIN strategy_mgmt.strategy_definition n ON n.family_key = i.strategy_id
JOIN strategy_mgmt.strategy_version v
  ON v.definition_id = n.id AND v.version_label = d.strategy_version
JOIN strategy_mgmt.parameter_set p ON p.parameter_set_id = ps.parameter_set_id
WHERE NOT EXISTS (
    SELECT 1
    FROM strategy_mgmt.strategy_instance_v2 x
    WHERE x.attributes ->> 'legacy_strategy_id' = i.strategy_id
      AND x.attributes ->> 'legacy_instance_id' = i.instance_id
);
