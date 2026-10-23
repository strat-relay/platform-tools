import unittest
from orchestration.execution_readiness import execution_readiness


class ExecutionReadinessDiagnosticsTest(unittest.TestCase):
    def config(self, mode='LIVE', research=False, route=True, portfolio=True):
        return {'strategies': [{'strategy_id': 'S', 'enabled': True,
                                'research_only': research, 'routes': {'shadow_execution': route}}],
                'instances': [{'instance_id': 'I', 'strategy_id': 'S', 'enabled': True,
                               'execution_mode': mode}],
                'accounts': [{'account_id': 'A', 'enabled': True}],
                'portfolios': [{'portfolio_id': 'portfolio-shadow-k8s', 'enabled': True,
                                'strategy_ids': ['S'] if portfolio else [], 'account_ids': ['A']}]}

    def test_any_enabled_portfolio_counts_and_does_not_grant_authority(self):
        row = execution_readiness(self.config())['instances'][0]
        self.assertEqual(row['legacy_routing_warnings'], [])
        self.assertFalse(row['authority_granted'])
        self.assertTrue(row['requires_runtime_preflight'])

    def test_shadow_is_a_v2_blocker_not_fixed_by_legacy_routes(self):
        row = execution_readiness(self.config(mode='SHADOW'))['instances'][0]
        self.assertEqual(row['known_v2_blockers'], ['MODE_NOT_LIVE'])
        self.assertEqual(row['legacy_routing_warnings'], [])

    def test_research_block_and_missing_routes_are_separate(self):
        row = execution_readiness(self.config(research=True, route=False, portfolio=False))['instances'][0]
        self.assertIn('CURRENT_CONFIGURATION_RESEARCH_ONLY', row['known_v2_blockers'])
        self.assertEqual(set(row['legacy_routing_warnings']),
                         {'LEGACY_SHADOW_ROUTE_NOT_ENABLED', 'NO_ENABLED_PORTFOLIO_ACCOUNT_ROUTE'})

    def test_disabled_account_does_not_supply_a_route(self):
        cfg = self.config()
        cfg['accounts'][0]['enabled'] = False
        row = execution_readiness(cfg)['instances'][0]
        self.assertIn('NO_ENABLED_PORTFOLIO_ACCOUNT_ROUTE', row['legacy_routing_warnings'])


if __name__ == '__main__':
    unittest.main()
