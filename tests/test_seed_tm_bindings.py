"""scripts/seed_tm_bindings.py: registers a frozen TmVersion + legacy_stream_binding per
strategy's own `trade_management` config block.

Uses a small fake scoped to this test file rather than trade_management/fakes.py's
FakeConnection: that fake deliberately raises on any INSERT into trade_manager_version
("seeded via seed_tm_version(), never INSERTed by this package") because trade_management's own
runtime code is never supposed to write that table - only a migration or an explicit script like
this one does, so a different, permissive fake is the correct choice here, not a gap in the
shared one.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
import unittest

from scripts.seed_tm_bindings import SeedError, seed


class FakeCursor:
    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        upper = " ".join(sql.split()).upper()
        if upper.startswith("INSERT INTO TRADE_MANAGEMENT.TRADE_MANAGER_VERSION"):
            tm_version_id, evaluator_id, label, manifest_json, manifest_hash = params
            self.conn.versions.setdefault(tm_version_id, {
                "tm_version_id": tm_version_id, "evaluator_id": evaluator_id, "label": label,
                "manifest": json.loads(manifest_json), "manifest_hash": manifest_hash,
            })
            return
        if upper.startswith("INSERT INTO TRADE_MANAGEMENT.TM_VERSION_PROMOTION"):
            tm_version_id, decided_by, _tm_version_id_again = params
            self.conn.promotions.setdefault(tm_version_id, {"decided_by": decided_by, "publication_eligibility": "SHADOW_ONLY"})
            return
        if upper.startswith("INSERT INTO TRADE_MANAGEMENT.LEGACY_STREAM_BINDING"):
            binding_id, strategy_id, tm_version_id, valid_from, binding_hash = params
            self.conn.bindings.setdefault(binding_id, {
                "binding_id": binding_id, "strategy_id": strategy_id, "tm_version_id": tm_version_id,
                "valid_from": valid_from, "binding_hash": binding_hash,
            })
            return
        raise AssertionError(f"FakeCursor cannot handle: {sql[:120]}")


class FakeConnection:
    def __init__(self) -> None:
        self.versions: dict[str, dict[str, Any]] = {}
        self.promotions: dict[str, dict[str, Any]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.committed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def write_config(strategies: list[dict[str, Any]]) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "platform.json"
    tmp.write_text(json.dumps({"strategies": strategies}), encoding="utf-8")
    return tmp


BREAKEVEN_STRATEGY = {
    "strategy_id": "STRAT_CONFIGURED", "strategy_version": "V1", "enabled": True,
    "trade_management": {"policy": "tm-breakeven-trail.v1", "label": "TEST-LABEL",
                         "params": {"breakeven_trigger_r": 1.0, "trail_trigger_r": 1.5, "trail_distance_r": 0.5}},
}
UNCONFIGURED_STRATEGY = {"strategy_id": "STRAT_PLAIN", "strategy_version": "V1", "enabled": True}


class SeedTmBindingsTests(unittest.TestCase):
    def test_a_strategy_with_no_trade_management_block_is_skipped(self):
        path = write_config([UNCONFIGURED_STRATEGY])
        conn = FakeConnection()
        results = seed(config_path=path, valid_from="2026-09-24T00:00:00Z", decided_by="test",
                       connect_fn=lambda: conn)
        self.assertEqual(results, [])
        self.assertEqual(conn.versions, {})
        self.assertEqual(conn.bindings, {})

    def test_a_configured_strategy_gets_a_version_promotion_and_binding(self):
        path = write_config([BREAKEVEN_STRATEGY])
        conn = FakeConnection()
        results = seed(config_path=path, valid_from="2026-09-24T00:00:00Z", decided_by="test",
                       connect_fn=lambda: conn)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["strategy_id"], "STRAT_CONFIGURED")
        tm_version_id = results[0]["tm_version_id"]
        self.assertIn(tm_version_id, conn.versions)
        self.assertEqual(conn.versions[tm_version_id]["evaluator_id"], "tm-breakeven-trail.v1")
        self.assertIn(tm_version_id, conn.promotions)
        self.assertEqual(conn.promotions[tm_version_id]["publication_eligibility"], "SHADOW_ONLY")
        binding = conn.bindings[results[0]["binding_id"]]
        self.assertEqual(binding["strategy_id"], "STRAT_CONFIGURED")
        self.assertEqual(binding["tm_version_id"], tm_version_id)
        self.assertTrue(conn.committed)

    def test_identical_params_always_hash_to_the_same_tm_version_id(self):
        path_a = write_config([{**BREAKEVEN_STRATEGY, "strategy_id": "STRAT_A"}])
        path_b = write_config([{**BREAKEVEN_STRATEGY, "strategy_id": "STRAT_B"}])
        conn = FakeConnection()
        result_a = seed(config_path=path_a, valid_from="2026-09-24T00:00:00Z", decided_by="t", connect_fn=lambda: conn)
        result_b = seed(config_path=path_b, valid_from="2026-09-24T00:00:00Z", decided_by="t", connect_fn=lambda: conn)
        self.assertEqual(result_a[0]["tm_version_id"], result_b[0]["tm_version_id"])
        self.assertEqual(len(conn.versions), 1)  # one shared frozen version, two strategy bindings

    def test_different_params_produce_different_tm_version_ids(self):
        strict = {**BREAKEVEN_STRATEGY, "strategy_id": "STRAT_STRICT",
                 "trade_management": {**BREAKEVEN_STRATEGY["trade_management"],
                                      "params": {"breakeven_trigger_r": 0.5, "trail_trigger_r": 1.0, "trail_distance_r": 0.3}}}
        path = write_config([BREAKEVEN_STRATEGY, strict])
        conn = FakeConnection()
        results = seed(config_path=path, valid_from="2026-09-24T00:00:00Z", decided_by="t", connect_fn=lambda: conn)
        self.assertNotEqual(results[0]["tm_version_id"], results[1]["tm_version_id"])
        self.assertEqual(len(conn.versions), 2)

    def test_rerunning_is_idempotent(self):
        path = write_config([BREAKEVEN_STRATEGY])
        conn = FakeConnection()
        seed(config_path=path, valid_from="2026-09-24T00:00:00Z", decided_by="t", connect_fn=lambda: conn)
        seed(config_path=path, valid_from="2026-09-24T00:00:00Z", decided_by="t", connect_fn=lambda: conn)
        self.assertEqual(len(conn.versions), 1)
        self.assertEqual(len(conn.bindings), 1)

    def test_an_unknown_policy_name_raises_and_seeds_nothing_for_that_strategy(self):
        bad = {**BREAKEVEN_STRATEGY, "trade_management": {"policy": "tm-does-not-exist.v1", "params": {}}}
        path = write_config([bad])
        conn = FakeConnection()
        with self.assertRaises(SeedError):
            seed(config_path=path, valid_from="2026-09-24T00:00:00Z", decided_by="t", connect_fn=lambda: conn)
        self.assertEqual(conn.versions, {})

    def test_a_binding_is_always_scoped_to_the_whole_strategy_not_an_instance_or_instrument(self):
        path = write_config([BREAKEVEN_STRATEGY])
        conn = FakeConnection()
        results = seed(config_path=path, valid_from="2026-09-24T00:00:00Z", decided_by="t", connect_fn=lambda: conn)
        binding = conn.bindings[results[0]["binding_id"]]
        # legacy_stream_binding's own most-specific-match rule (binding.py) makes an
        # instance/instrument-scoped row take precedence when one exists - this script never
        # creates one at that granularity, only a strategy-wide default.
        self.assertNotIn("strategy_instance_id", " ".join(str(v) for v in binding.values()))


if __name__ == "__main__":
    unittest.main()
