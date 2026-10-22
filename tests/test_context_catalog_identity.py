"""Read-model regression: one Context group, without deleting historical identities."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from platform_api.control import PlatformControlApi
from platform_api.strategy_catalog import StrategyCatalogRepository


def registry_row(version):
    return {"family_key": "CONTEXT_STRUCTURE_RETRACE", "name": "Context Structure Retrace",
            "version_label": version, "instance_id": f"uuid-{version}",
            "evaluator_key": ("ContextStructureRetraceAdapter" if version == "V1"
                              else "context_structure_retrace_v2_research"),
            "attributes": ({"legacy_instance_id": "phase6"} if version == "V1"
                           else {"instance_id": "context-v2-research"}),
            "online": True, "execution_eligible": True}


class ContextCatalogIdentityTests(unittest.TestCase):
    def test_registry_uses_versioned_strategy_and_publication_alias(self):
        summaries = PlatformControlApi._registry_summary([registry_row("V1"), registry_row("V2")])
        self.assertEqual([s["strategy_id"] for s in summaries],
                         ["CONTEXT_STRUCTURE_RETRACE_V1", "CONTEXT_STRUCTURE_RETRACE_V2"])
        self.assertEqual([s["instances"][0]["instance_id"] for s in summaries],
                         ["phase6", "context-v2-research"])
        self.assertEqual(summaries[1]["instances"][0]["registry_instance_id"], "uuid-V2")

    def test_canonical_history_wins_over_zero_registry_placeholder(self):
        api = PlatformControlApi.__new__(PlatformControlApi)
        api.strategy_mgmt_api = SimpleNamespace(registry_rows=lambda: [registry_row("V1"), registry_row("V2")])
        legacy = [{"strategy_id": f"CONTEXT_STRUCTURE_RETRACE_{v}",
                   "display_name": "Context Structure Retrace", "signals_published": count,
                   "instances": [{"instance_id": alias, "stats": {"signals": count}}]}
                  for v, alias, count in [("V1", "phase6", 1743), ("V2", "context-v2-research", 261)]]
        [group] = api._unified_strategy_list(legacy)
        self.assertEqual(group["strategy_id"], "CONTEXT_STRUCTURE_RETRACE")
        self.assertEqual(group["signals_published"], 2004)
        self.assertEqual([i["stats"]["signals"] for i in group["instances"]], [1743, 261])
        self.assertEqual(group["instance_count"], 2)

    def test_registry_only_version_is_not_hidden(self):
        api = PlatformControlApi.__new__(PlatformControlApi)
        api.strategy_mgmt_api = SimpleNamespace(registry_rows=lambda: [registry_row("V2")])
        [group] = api._unified_strategy_list([])
        self.assertEqual(group["instances"][0]["instance_id"], "context-v2-research")

    def test_empty_version_does_not_create_none_instance(self):
        row = {**registry_row("V2"), "instance_id": None}
        self.assertEqual(PlatformControlApi._registry_summary([row]), [])

    def test_duplicate_alias_keeps_canonical_row_and_distinct_identities(self):
        repo = StrategyCatalogRepository.__new__(StrategyCatalogRepository)
        repo._require_instances = Mock()
        repo._execution_facts = Mock(return_value=None)
        repo._published_parameter_sets = Mock(return_value={})
        original = {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V2", "instance_id": "context-v2-research",
                    "signals": 261}
        distinct = {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "instance_id": "uuid-V1", "signals": 308}
        repo._rows = Mock(side_effect=[[original], [dict(original, signals=0), distinct]])
        cur = Mock()
        cur.fetchone.return_value = (True,)
        with patch("platform_api.strategy_catalog._instance", side_effect=lambda row, *args: row), \
                patch("platform_api.library_observation.load_observation", return_value=None):
            result = repo._instances(cur)
        self.assertEqual(result, [original, distinct])
        self.assertEqual(result[0]["signals"], 261)


if __name__ == "__main__":
    unittest.main()
