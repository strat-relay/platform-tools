import json
import unittest
from pathlib import Path

from postgres.phase6 import canonical_hash


class PostgresContractTests(unittest.TestCase):
    def test_canonical_hash_is_order_independent(self):
        self.assertEqual(canonical_hash({"b": 2, "a": 1}), canonical_hash({"a": 1, "b": 2}))

    def test_migrations_define_all_boundaries(self):
        root = Path(__file__).parent / "postgres" / "migrations"
        text = "\n".join(p.read_text() for p in sorted(root.glob("*.sql")))
        for schema in ("platform", "research", "strategy", "orchestration", "execution", "trade_management", "audit", "telemetry"):
            self.assertIn(f"CREATE SCHEMA IF NOT EXISTS {schema}", text)
        self.assertIn("NO_BROKER_WRITES_PHASE6_IMPORT", text)
        self.assertIn("platform.strategy_versions", text)
        self.assertIn("platform.configuration_versions", text)
        self.assertIn("platform.freeze_manifests", text)
        self.assertIn("strategy.runner_state", text)
        self.assertIn("strategy.phase6_lifecycle_events", text)
        self.assertIn("telemetry.phase6_unattached_events", text)
        self.assertNotIn("state_jsonb", text)

    def test_importer_defaults_to_compact_sidecar(self):
        from postgres.import_phase6 import ROOT
        self.assertTrue((ROOT / "context_structure_retrace_forward_state_compact.json").exists())


if __name__ == "__main__": unittest.main()
