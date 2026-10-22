"""Catalog-only identity resolution from explicit database metadata.

Never infer identity from display names, evaluators, family keys or signal history.
This module grants no runtime or execution authority.
"""
from .signals import CanonicalSourceUnavailable


class IdentityConflict(CanonicalSourceUnavailable):
    pass


def declared_identity(attributes, registry_id):
    attributes = attributes if isinstance(attributes, dict) else {}
    strategy_ids = {attributes[k] for k in ('strategy_id', 'legacy_strategy_id')
                    if isinstance(attributes.get(k), str) and attributes[k]}
    if len(strategy_ids) > 1:
        raise IdentityConflict('conflicting declared strategy identities')
    strategy_id = next(iter(strategy_ids), None)
    instance_id = (attributes.get('instance_id') or attributes.get('legacy_instance_id')
                   or str(registry_id))
    aliases = sorted({str(value) for value in
                      (registry_id, attributes.get('instance_id'), attributes.get('legacy_instance_id'))
                      if value})
    return strategy_id, instance_id, aliases


def signal_identity_links(rows):
    """Validate unique ownership of aliases, returning a JSON-compatible lookup.

    Only rows with a declared strategy identity can expand an operational scope.
    Registry-only rows without a declaration keep their existing fallback behavior.
    """
    owners, links = {}, {}
    for row in rows:
        if not row.get('instance_id'):
            continue
        if row.get('conflicting_operational_identity'):
            raise IdentityConflict('alias overlaps a distinct operational instance')
        sid, iid, aliases = declared_identity(row.get('attributes'), row['instance_id'])
        if sid is None:
            continue
        owner = str(row['instance_id'])
        for alias in aliases:
            key = (sid, alias)
            if key in owners and owners[key] != owner:
                raise IdentityConflict('ambiguous declared instance identity')
            owners[key] = owner
        links.setdefault(sid, {})[iid] = sorted(set(aliases + [iid]))
    return links


_IDENTITY_ROWS_SQL = """
SELECT v.id::text AS instance_id, v.attributes,
       EXISTS (
           SELECT 1 FROM platform.strategy_instance i
           WHERE i.strategy_id = coalesce(v.attributes->>'strategy_id', v.attributes->>'legacy_strategy_id')
             AND i.instance_id = ANY(ARRAY[v.id::text, v.attributes->>'instance_id', v.attributes->>'legacy_instance_id'])
             AND i.instance_id <> coalesce(v.attributes->>'instance_id', v.attributes->>'legacy_instance_id', v.id::text)
       ) AS conflicting_operational_identity
FROM strategy_mgmt.strategy_instance_v2 v ORDER BY v.id
"""

# A bound JSON parameter contains only validated explicit identity links. The
# current database rows are reloaded on every request; no per-record constants.
_IDENTITY_JOIN_SQL = """
CROSS JOIN LATERAL (
    SELECT coalesce(array(SELECT jsonb_array_elements_text(
        coalesce((%s::jsonb)->i.strategy_id->i.instance_id, '[]'::jsonb))), ARRAY[]::text[])
        || ARRAY[i.instance_id] AS ids
) identity
"""

_SINGLE_IDENTITY_JOIN_SQL = "CROSS JOIN LATERAL (SELECT ARRAY[i.instance_id] AS ids) identity"
