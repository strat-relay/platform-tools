import unittest

from dataclasses import replace
from datetime import datetime, timezone

from liquidity_displacement import LiquidityDisplacementConfig, LiquidityDisplacementStrategy
from liquidity_market_data import LiveMarketSnapshot
from orchestration.liquidity_live import LiquidityLiveEvaluator, PARAMETER_SETS


def fixture_snapshot(symbol="XAUUSDm", count=43):
    bars = [{"time": n * 300, "open": 100, "high": 101, "low": 100, "close": 100.5}
            for n in range(40)]
    bars += [
        {"time": 40 * 300, "open": 100.5, "high": 101.2, "low": 99, "close": 100.8},
        {"time": 41 * 300, "open": 100.8, "high": 103, "low": 100.5, "close": 102.8},
        {"time": 42 * 300, "open": 102.8, "high": 103, "low": 100, "close": 102.5},
    ][:count - 40]
    return LiveMarketSnapshot(tuple(bars), tuple(
        {"time": n * 900, "open": 100, "high": 101, "low": 100, "close": 100.5}
        for n in range(30)), {"bid": 100, "ask": 100.2},
        {"tick_size": 0.01, "stops_level": 0, "point": 0.01},
        symbol.rstrip("m"), symbol, "2026-09-26T12:00:00Z")


class LiquidityParityTests(unittest.TestCase):
    def test_incremental_live_matches_authoritative_replay_for_all_four_variants(self):
        for instance_id, params in PARAMETER_SETS.items():
            with self.subTest(instance_id=instance_id):
                base_strategy = LiquidityDisplacementStrategy(LiquidityDisplacementConfig(
                    symbol=params.broker_symbol, max_retrace_candles=params.max_retrace_candles))

                class VariantStrategy(LiquidityDisplacementStrategy):
                    def find_candidate(self, m15, m5, i, quote, contract, timestamp):
                        candidate = super().find_candidate(m15, m5, i, quote, contract, timestamp)
                        if not candidate:
                            return None
                        bar = m5[candidate["displacement_index"]]
                        depth = params.entry_fraction
                        low, high = float(bar["low"]), float(bar["high"])
                        candidate["entry"] = high - (high - low) * depth if candidate["direction"] == "LONG" else low + (high - low) * depth
                        candidate["risk"] = candidate["entry"] - candidate["stop_loss"] if candidate["direction"] == "LONG" else candidate["stop_loss"] - candidate["entry"]
                        return candidate

                authoritative = VariantStrategy(base_strategy.config)
                full = fixture_snapshot(params.broker_symbol, 43)
                expected = authoritative.evaluate(list(full.M15), list(full.M5), full.quote, full.contract,
                                                  "2026-09-26T12:00:00Z", 40)
                live = LiquidityLiveEvaluator(params)
                self.assertIsNone(live.evaluate(fixture_snapshot(params.broker_symbol, 41),
                                                evaluation_time="2026-09-26T12:00:00Z"))
                self.assertIsNone(live.evaluate(fixture_snapshot(params.broker_symbol, 42),
                                                evaluation_time="2026-09-26T12:00:00Z"))
                actual = live.evaluate(full, evaluation_time="2026-09-26T12:00:00Z")
                self.assertIsNotNone(expected)
                self.assertIsNotNone(actual)
                self.assertEqual((expected["status"], expected["fill_index"]), ("FILLED", 42))
                self.assertEqual((actual.direction, actual.entry_price, actual.stop_price, actual.target_price),
                                 (expected["signal"].direction, expected["signal"].entry, expected["signal"].stop_loss,
                                  expected["signal"].take_profit))


if __name__ == "__main__":
    unittest.main()
