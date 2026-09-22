from __future__ import annotations

import unittest

from trade_management.ids import (binding_id, decision_id, managed_trade_id, market_snapshot_id,
                                  observation_id, stable_id, tm_version_id)
from trade_management.versions import TM_NONE_1_MANIFEST, TmVersionManifest


class TmVersionManifestTests(unittest.TestCase):
    def test_manifest_hash_is_deterministic(self):
        h1 = TM_NONE_1_MANIFEST.manifest_hash()
        h2 = TM_NONE_1_MANIFEST.manifest_hash()
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)

    def test_golden_vector_for_tm_none_1(self):
        # Golden vector for this manifest at this commit. If this hash changes, the manifest
        # content changed - which must be a deliberate, reviewed decision (a new version),
        # never an accidental side effect of an unrelated edit.
        self.assertEqual(TM_NONE_1_MANIFEST.manifest_hash(),
                         "ecaca5f080f9f79bb18cc936fb3867420c416eb372f998f3ec7c175369b5ac0d")
        self.assertTrue(TM_NONE_1_MANIFEST.tm_version_id().startswith("TMV_"))
        self.assertEqual(len(TM_NONE_1_MANIFEST.tm_version_id()), len("TMV_") + 24)

    def test_any_manifest_member_change_changes_the_id(self):
        base_id = TM_NONE_1_MANIFEST.tm_version_id()
        changed = TmVersionManifest(evaluator_id=TM_NONE_1_MANIFEST.evaluator_id, label="irrelevant-label-change",
                                    observation_spec={**TM_NONE_1_MANIFEST.observation_spec, "max_market_age_ms": 61_000},
                                    price_semantics=TM_NONE_1_MANIFEST.price_semantics,
                                    arithmetic=TM_NONE_1_MANIFEST.arithmetic,
                                    code_manifest=TM_NONE_1_MANIFEST.code_manifest, scope=TM_NONE_1_MANIFEST.scope)
        self.assertNotEqual(base_id, changed.tm_version_id())

    def test_label_and_excluded_members_do_not_change_the_id(self):
        # `label`/`description` are explicitly excluded from the hash (A6 07 section 3).
        relabelled = TmVersionManifest(evaluator_id=TM_NONE_1_MANIFEST.evaluator_id, label="SOME-OTHER-LABEL",
                                       policy_bundle=TM_NONE_1_MANIFEST.policy_bundle,
                                       resolution_table=TM_NONE_1_MANIFEST.resolution_table,
                                       observation_spec=TM_NONE_1_MANIFEST.observation_spec,
                                       price_semantics=TM_NONE_1_MANIFEST.price_semantics,
                                       arithmetic=TM_NONE_1_MANIFEST.arithmetic,
                                       code_manifest=TM_NONE_1_MANIFEST.code_manifest, scope=TM_NONE_1_MANIFEST.scope)
        self.assertEqual(TM_NONE_1_MANIFEST.tm_version_id(), relabelled.tm_version_id())

    def test_tm_version_id_matches_the_ids_module_derivation(self):
        self.assertEqual(TM_NONE_1_MANIFEST.tm_version_id(), tm_version_id(TM_NONE_1_MANIFEST.manifest_hash()))


class IdentityGoldenVectorTests(unittest.TestCase):
    def test_managed_trade_id_golden_vector_and_determinism(self):
        first = managed_trade_id("SIG_fixedvalue000000000")
        second = managed_trade_id("SIG_fixedvalue000000000")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("MT_"))
        self.assertEqual(first, stable_id("MT", {"signal_id": "SIG_fixedvalue000000000"}))

    def test_managed_trade_id_depends_only_on_signal_id(self):
        # Independent of process, path, wall clock: calling it many times, from anywhere, with
        # the same signal_id, always produces the same id.
        ids = {managed_trade_id("SIG_A") for _ in range(50)}
        self.assertEqual(len(ids), 1)
        self.assertNotEqual(managed_trade_id("SIG_A"), managed_trade_id("SIG_B"))

    def test_market_snapshot_id_shares_across_identical_facts(self):
        a = market_snapshot_id(provider_id="p1", feed_id="f1", instrument="XAUUSD",
                               source_timestamp="2026-09-22T00:00:00Z", bid=100.0, ask=100.2)
        b = market_snapshot_id(provider_id="p1", feed_id="f1", instrument="XAUUSD",
                               source_timestamp="2026-09-22T00:00:00Z", bid=100.0, ask=100.2)
        self.assertEqual(a, b)
        c = market_snapshot_id(provider_id="p1", feed_id="f1", instrument="XAUUSD",
                               source_timestamp="2026-09-22T00:00:00Z", bid=100.0, ask=100.3)
        self.assertNotEqual(a, c)

    def test_observation_id_is_content_addressed_independent_of_sequence(self):
        kwargs = dict(managed_trade_id="MT_x", provider_id="p1", feed_id=None, instrument="XAUUSD",
                      source_timestamp="2026-09-22T00:00:00Z", bid=100.0, ask=100.2)
        self.assertEqual(observation_id(**kwargs), observation_id(**kwargs))

    def test_decision_id_scoped_to_trade_observation_and_version(self):
        base = dict(managed_trade_id="MT_x", observation_id="TOBS_y", tm_version_id="TMV_z")
        self.assertEqual(decision_id(**base), decision_id(**base))
        other_version = dict(base, tm_version_id="TMV_other")
        self.assertNotEqual(decision_id(**base), decision_id(**other_version))

    def test_binding_id_is_deterministic(self):
        kwargs = dict(strategy_id="STRAT", strategy_instance_id=None, instrument=None,
                      tm_version_id="TMV_z", resolution="DEFAULT_TM_NONE", valid_from="2026-09-22T00:00:00Z")
        self.assertEqual(binding_id(**kwargs), binding_id(**kwargs))


if __name__ == "__main__":
    unittest.main()
