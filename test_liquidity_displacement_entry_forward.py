import unittest

import liquidity_displacement_entry_forward as runner


class FrozenEntryRunnerTests(unittest.TestCase):
    def test_variants_are_frozen(self):
        self.assertEqual(runner.VARIANTS["usdjpy25"]["symbol"], "USDJPYm")
        self.assertEqual(runner.VARIANTS["usdjpy25"]["fraction"], 0.25)
        self.assertEqual(runner.VARIANTS["xau33"]["symbol"], "XAUUSDm")
        self.assertAlmostEqual(runner.VARIANTS["xau33"]["fraction"], 1 / 3)
        self.assertEqual(runner.VARIANTS["btc25"]["symbol"], "BTCUSDm")
        self.assertEqual(runner.VARIANTS["btc25"]["fraction"], 0.25)
        self.assertEqual(runner.VARIANTS["ustec_x100m"]["symbol"], "USTEC_x100m")
        self.assertEqual(runner.VARIANTS["ustec_x100m"]["fraction"], 0.25)
        self.assertEqual(runner.VARIANTS["ustecm"]["symbol"], "USTECm")
        self.assertEqual(runner.VARIANTS["ustecm"]["fraction"], 0.25)

    def test_configure_manifest_keeps_strategy_hash(self):
        runner.configure("usdjpy25")
        manifest = runner.base.manifest()
        self.assertEqual(manifest["source_sha256"], "4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea")
        self.assertEqual(manifest["max_retrace_candles"], 5)
        self.assertEqual(manifest["target_r"], 1.25)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["live_order_endpoints"])


if __name__ == "__main__":
    unittest.main()
