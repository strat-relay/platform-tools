import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from paper_engine import PaperEngine, Signal, StrategyConfig, size_position


class PaperEngineTests(unittest.TestCase):
    def test_minimum_lot_is_never_rounded_up(self):
        result = size_position(10, 100.0, 90.0, 0.5, 1.0, 1.0, 0.01, 200.0, 0.01)
        self.assertEqual(result.status, "TRADE_SKIPPED_MINIMUM_LOT_EXCEEDS_RISK")

    def test_disabled_engine_never_enters(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PaperEngine(StrategyConfig(engine_enabled=False), str(Path(directory) / "audit.jsonl"))
            signal = Signal("XAUUSDm", "LONG", "SWEEP", 100, 99, 102, .8, "M5", ["test"], None, "now", "fingerprint")
            result = engine.evaluate(signal, 1000, {"tick_size": .01, "tick_value": 1, "min_lot": .01, "max_lot": 200, "lot_step": .01}, {"bid": 100, "ask": 100.1})
            self.assertEqual(result["decision"], "NO_TRADE")
            self.assertEqual(len(engine.positions), 0)

    def test_duplicate_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PaperEngine(StrategyConfig(engine_enabled=True, max_open_scalps=2), str(Path(directory) / "audit.jsonl"))
            signal = Signal("XAUUSDm", "LONG", "SWEEP", 100, 99, 102, .8, "M5", ["test"], None, "now", "fingerprint")
            contract = {"tick_size": .01, "tick_value": 1, "min_lot": .01, "max_lot": 200, "lot_step": .01}
            self.assertEqual(engine.evaluate(signal, 1000, contract, {})["decision"], "PAPER_ENTRY")
            self.assertIn("duplicate_signal", engine.evaluate(signal, 1000, contract, {})["reasons"])

    def test_same_candle_sl_wins_conservatively(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = PaperEngine(StrategyConfig(engine_enabled=True), str(Path(directory) / "audit.jsonl"))
            signal = Signal("XAUUSDm", "LONG", "SWEEP", 100, 99, 101, .8, "M5", ["test"], None, datetime.fromtimestamp(0, timezone.utc).isoformat(), "fingerprint")
            engine.evaluate(signal, 1000, {"tick_size": .01, "tick_value": 1, "min_lot": .01, "max_lot": 200, "lot_step": .01}, {})
            engine.process_candle({"time": 1, "open": 100, "high": 102, "low": 98, "close": 101}, 1)
            self.assertEqual(engine.closed[0].exit_reason, "SL")


if __name__ == "__main__":
    unittest.main()
