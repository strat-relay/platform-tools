import json
import unittest
from pathlib import Path


class BTCValidationArtifactTests(unittest.TestCase):
    def test_btc_validator_is_separate(self):
        self.assertTrue(Path("liquidity_displacement_btc_validate.py").exists())
        self.assertTrue(Path("historical_fetch_btc.py").exists())

    def test_v1_hash_constant_is_preserved(self):
        text = Path("liquidity_displacement_btc_validate.py").read_text()
        self.assertIn("4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea", text)


if __name__ == "__main__":
    unittest.main()
