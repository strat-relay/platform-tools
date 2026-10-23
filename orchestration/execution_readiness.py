"""Read-only configuration diagnostics; never derive or grant broker authority."""


def execution_readiness(config):
    definitions = {s['strategy_id']: s for s in config.get('strategies', [])}
    accounts = {a['account_id']: a for a in config.get('accounts', [])}
    rows = []
    for instance in config.get('instances', []):
        sid = instance['strategy_id']
        definition = definitions.get(sid)
        mode = instance.get('execution_mode', 'OFF')
        blockers, warnings = [], []
        if not instance.get('enabled'):
            blockers.append('INSTANCE_OFFLINE')
        if definition is None or not definition.get('enabled'):
            blockers.append('PARENT_MISSING_OR_DISABLED')
        if mode != 'LIVE':
            blockers.append('MODE_NOT_LIVE')
        if (definition or {}).get('research_only') is True or instance.get('research_only') is True:
            blockers.append('CURRENT_CONFIGURATION_RESEARCH_ONLY')
        routes = [{'portfolio_id': p['portfolio_id'], 'account_id': aid}
                  for p in config.get('portfolios', []) if p.get('enabled')
                  and sid in (p.get('strategy_ids') or [])
                  for aid in (p.get('account_ids') or []) if accounts.get(aid, {}).get('enabled')]
        if mode in {'SHADOW', 'LIVE'}:
            if ((definition or {}).get('routes') or {}).get('shadow_execution') is not True:
                warnings.append('LEGACY_SHADOW_ROUTE_NOT_ENABLED')
            if not routes:
                warnings.append('NO_ENABLED_PORTFOLIO_ACCOUNT_ROUTE')
        rows.append({'instance_id': instance['instance_id'], 'strategy_id': sid,
                     'online': bool(instance.get('enabled')), 'execution_mode': mode,
                     'known_v2_blockers': blockers, 'legacy_routing_warnings': warnings,
                     'enabled_portfolio_accounts': routes,
                     'authority_granted': False,
                     'requires_runtime_preflight': True})
    return {'schema': 'execution-readiness-diagnostics.v1', 'read_only': True,
            'canonical_instance_policy': 'NOT_EVALUATED',
            'broker_writes': 0, 'instances': rows,
            'note': 'Configuration diagnostics only. Empty blockers do not establish live eligibility; '
                    'immutable signal provenance, authority, risk and broker checks remain independent.'}
