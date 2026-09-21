import unittest
from pathlib import Path


class MultiValidationTests(unittest.TestCase):
    def test_requested_validator_and_fetcher_exist(self):
        self.assertTrue(Path("liquidity_displacement_multi_validate.py").exists())
        self.assertTrue(Path("historical_fetch_symbol.py").exists())

    def test_requested_symbols_are_predeclared(self):
        text = Path("liquidity_displacement_multi_validate.py").read_text()
        for symbol in ("EURUSDm", "GBPUSDm", "USDJPYm", "USTEC_x100m", "USTECm"):
            self.assertIn(symbol, text)


if __name__ == "__main__":
    unittest.main()
