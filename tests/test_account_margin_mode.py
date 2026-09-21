import unittest

from trade_manager.central import normalize_account_margin_mode


class AccountMarginModeTests(unittest.TestCase):
    def test_hedging(self):
        self.assertEqual(normalize_account_margin_mode(2, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING"), ("2", "HEDGING"))

    def test_retail_netting(self):
        self.assertEqual(normalize_account_margin_mode(0, "ACCOUNT_MARGIN_MODE_RETAIL_NETTING"), ("0", "NETTING"))

    def test_exchange(self):
        self.assertEqual(normalize_account_margin_mode(1, "ACCOUNT_MARGIN_MODE_EXCHANGE"), ("1", "NETTING"))

    def test_unknown(self):
        self.assertEqual(normalize_account_margin_mode(99, "UNRECOGNIZED"), ("99", "UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
