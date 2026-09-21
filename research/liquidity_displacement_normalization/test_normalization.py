import unittest
import hashlib
from pathlib import Path

from .normalization import normalize_candidates


def row(ts, extreme, fill=10, setup=None, entry=75620.01):
    return {
        "symbol": "BTCUSDm", "direction": "LONG", "sweep_level": 75542.94,
        "sweep_timestamp": ts, "sweep_extreme": extreme,
        "displacement_index": 20, "displacement_timestamp": 1789513200,
        "entry_theoretical": entry, "entry_realistic": entry + 5.0,
        "stop_loss": extreme - 18.0, "risk": 100.0,
        "fill_index": fill, "fill_timestamp": 1789514100,
        "setup_id": setup or f"setup-{ts}", "outcome": "WIN", "r": 1.25,
    }


class NormalizationTests(unittest.TestCase):
    def test_frozen_strategy_hash_is_unchanged(self):
        digest = hashlib.sha256(Path("liquidity_displacement.py").read_bytes()).hexdigest()
        self.assertEqual(digest, "4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea")

    def test_btc_nested_sweeps_are_one_opportunity(self):
        rows = [row(1789511400, 75418.35, setup="btc-1"), row(1789511700, 75467.37, setup="btc-2"), row(1789512000, 75503.11, setup="btc-3")]
        result = normalize_candidates(rows)
        self.assertEqual(len(result["market_events"]), 1)
        self.assertEqual(len(result["sweep_sequences"]), 1)
        self.assertEqual(len(result["sweep_sequences"][0]["nested_sweeps"]), 3)
        self.assertEqual(len(result["entry_opportunities"]), 1)
        self.assertEqual(len(result["entry_attempts"]), 1)
        self.assertEqual(len(result["entry_opportunities"][0]["stop_hypotheses"]), 4)
        self.assertEqual(len(result["entry_opportunities"][0]["candidate_observation_ids"]), 3)

    def test_departure_and_return_is_a_reentry(self):
        bars = [{"high": 100.0, "low": 99.0} for _ in range(20)]
        bars[4] = {"high": 105.0, "low": 99.5}
        rows = [row(1, 98.0, fill=2, setup="first", entry=100.0), row(2, 99.0, fill=8, setup="second", entry=100.0)]
        result = normalize_candidates(rows, bars=bars, atr_by_index={2: 1.0, 8: 1.0}, departure_atr=2.0)
        self.assertEqual(len(result["entry_opportunities"]), 2)
        self.assertFalse(result["entry_opportunities"][0]["is_reentry"])
        self.assertTrue(result["entry_opportunities"][1]["is_reentry"])
        self.assertTrue(result["entry_opportunities"][1]["potential_scale_in"])


if __name__ == "__main__":
    unittest.main()
