import importlib
import unittest


class VariantATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.variant = importlib.import_module("liquidity_displacement_v1_variant_a")

    def test_variant_changes_only_expiry_and_target_grid(self):
        self.assertEqual(self.variant.VERSION, "LIQUIDITY_DISPLACEMENT_SCALP_V1_VARIANT_A")
        self.assertEqual(self.variant.VA_CFG.max_retrace_candles, 5)
        self.assertEqual(self.variant.VA_CFG.target_r, 1.25)
        self.assertEqual(self.variant.VA_CFG.max_hold_minutes, 120)
        self.assertEqual(self.variant.manifest_variant()["target_grid"], [1.0, 1.25, 1.5])

    def test_variant_persistence_is_isolated(self):
        self.variant.configure()
        self.assertNotEqual(self.variant.base.STATE.name, "liquidity_displacement_forward_state.json")
        self.assertNotEqual(self.variant.base.EVENTS.name, "liquidity_displacement_forward.jsonl")
        self.assertNotEqual(self.variant.base.PIDFILE.name, "liquidity_displacement_forward.pid")

    def test_strategy_hash_unchanged(self):
        self.assertEqual(self.variant.base.source_hash(), "4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea")


if __name__ == "__main__":
    unittest.main()
