import unittest
from pathlib import Path
from research.strategy_library_registration import CANARY, candidate_sql, registration
from strategy_backtest.library import BY_KEY, ComposedResearchRuntime


class RegistrationCandidateTests(unittest.TestCase):
    def test_parameter_references_match_frozen_runtime(self):
        for key in CANARY:
            with self.subTest(key=key):
                row = registration(key)
                runtime = ComposedResearchRuntime(BY_KEY[key],dataset_id='run',artifact_digest='artifact')
                self.assertEqual(row['parameter_hash'], runtime.parameter_hash)
                self.assertEqual(row['parameter_set_id'], runtime.parameter_set_id)
                self.assertEqual(row, registration(key))

    def test_candidate_is_reproducible(self):
        path = Path(__file__).resolve().parents[1] / 'docs/strategy_library/forward/registration_candidate.sql'
        self.assertEqual(path.read_text(), candidate_sql())

    def test_definitions_do_not_share_identity_or_catalog_key(self):
        rows = [registration(key) for key in CANARY]
        self.assertEqual(len({r['definition_id'] for r in rows}), 3)
        self.assertEqual(len({r['catalog_key'] for r in rows}), 3)
        with self.assertRaises(ValueError): candidate_sql(CANARY + CANARY)

    def test_provisioning_bundle_is_atomic_and_matches_candidates(self):
        root = Path(__file__).resolve().parents[1] / 'docs/strategy_library/forward'
        bundle = (root/'provisioning_candidate.sql').read_text()
        for name in ('schema_candidate.sql', 'registration_candidate.sql', 'observer_role_candidate.sql'):
            self.assertIn((root/name).read_text(), bundle)
        self.assertIn('\nBEGIN;\n', bundle)
        self.assertTrue(bundle.endswith('COMMIT;\n'))
        migration = (root.parents[2]/'postgres/migrations/063_strategy_library_forward_canary.sql').read_text()
        for name in ('schema_candidate.sql', 'registration_candidate.sql', 'observer_role_candidate.sql'):
            self.assertIn((root/name).read_text(), migration)
