"""Record-agnostic, fail-closed instance eligibility. Never grants broker authority."""


def research_restricted(*documents):
    for document in documents:
        if not isinstance(document, dict):
            return True
        if any(key in document and not isinstance(document[key], bool)
               for key in ('research_only', 'shadow_only', 'execution_eligible', 'production_eligible')):
            return True
        if (document.get('research_only') is True or document.get('shadow_only') is True
                or document.get('execution_eligible') is False
                or document.get('production_eligible') is False
                or document.get('signal_emission_class') == 'RETROSPECTIVE_RESEARCH_EVIDENCE'
                or document.get('classification') == 'RETROSPECTIVE_RESEARCH_EVIDENCE'):
            return True
    return False


def instance_policy_reason(record, rows):
    """Exactly one explicitly linked, online, eligible LIVE instance is required."""
    if not rows:
        return 'INSTANCE_EXECUTION_POLICY_MISSING'
    if len(rows) != 1:
        return 'INSTANCE_EXECUTION_POLICY_AMBIGUOUS'
    row = rows[0]
    if row['declared_strategy_id'] != record.get('strategy_id'):
        return 'INSTANCE_STRATEGY_IDENTITY_MISMATCH'
    if row['version_label'] != record.get('strategy_version'):
        return 'STRATEGY_VERSION_MISMATCH'
    expected_ref = f"{record.get('strategy_id')}@{record.get('strategy_version')}"
    if record.get('strategy_ref') != expected_ref:
        return 'STRATEGY_REF_MISMATCH'
    parameter_set_ref = record.get('parameter_set_ref')
    if parameter_set_ref is not None and row['parameter_set_id'] != parameter_set_ref:
        return 'PARAMETER_SET_MISMATCH'
    if row['online'] is not True or row['parent_enabled'] is not True:
        return 'INSTANCE_OR_PARENT_DISABLED'
    if row['execution_mode'] != 'LIVE':
        return 'INSTANCE_NOT_LIVE'
    if row['execution_eligible'] is not True:
        return 'INSTANCE_NOT_EXECUTION_ELIGIBLE'
    if row['mode_revision'] != (record.get('source_provenance') or {}).get('execution_mode_revision'):
        return 'INSTANCE_EXECUTION_REVISION_MISMATCH'
    if research_restricted(row['attributes'] or {}, row['parent_attributes'] or {}):
        return 'STRATEGY_NOT_EXECUTION_ENABLED'
    return None


def database_policy_reason(conn, record):
    instance_id = record.get('strategy_instance_id')
    if not isinstance(instance_id, str) or not instance_id:
        return 'INSTANCE_EXECUTION_POLICY_MISSING'
    # Registry family is used only for an exact identity declaration, never an
    # evaluator/name heuristic. An alias without an agreeing strategy link fails closed.
    with conn.cursor() as cur:
        cur.execute('''SELECT i.id, i.online, i.execution_mode, i.execution_eligible,
            i.execution_mode_revision, i.attributes,
            coalesce(i.attributes->>'strategy_id', i.attributes->>'legacy_strategy_id', d.family_key),
            v.version_label, p.parameter_set_id, parent.enabled, parent.attributes
            FROM strategy_mgmt.strategy_instance_v2 i
            JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id
            JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id
            JOIN strategy_mgmt.strategy_definition d ON d.id=v.definition_id
            JOIN platform.strategy_definition parent ON parent.strategy_id=%s
            WHERE %s = ANY(ARRAY[i.id::text, i.attributes->>'instance_id', i.attributes->>'legacy_instance_id'])
              AND coalesce(i.attributes->>'strategy_id', i.attributes->>'legacy_strategy_id', d.family_key) = parent.strategy_id
            FOR SHARE OF i, parent''', (record['strategy_id'], instance_id))
        columns = ('id', 'online', 'execution_mode', 'execution_eligible', 'mode_revision',
                   'attributes', 'declared_strategy_id', 'version_label', 'parameter_set_id',
                   'parent_enabled', 'parent_attributes')
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    for row in rows:
        attrs = row['attributes'] or {}
        declared = {attrs[k] for k in ('strategy_id', 'legacy_strategy_id') if attrs.get(k)}
        if len(declared) > 1:
            return 'INSTANCE_STRATEGY_IDENTITY_AMBIGUOUS'
    return instance_policy_reason(record, rows)
