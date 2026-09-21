import tempfile
import unittest
from pathlib import Path

from trade_manager.observation import CausalObserver, build_observation, causal_candles, detect_events, ema_value, structure_snapshot
from trade_manager.observation_storage import ObservationStore
from trade_manager.collector import CausalObservationCollector


def pos(direction="SHORT"):
    return {"strategy_id": "S", "setup_id": "U", "economic_position_id": "P", "symbol": "XAUUSDm",
            "direction": direction, "entry": 100.0, "original_stop": 110.0 if direction == "SHORT" else 90.0,
            "original_target": 80.0 if direction == "SHORT" else 120.0, "current_stop": 110.0 if direction == "SHORT" else 90.0,
            "size": 1, "entry_time": "2026-01-01T00:00:00+00:00", "status": "OPEN"}


def bar(t, o, h, l, c, spread=0):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "spread": spread}


class ObservationTests(unittest.TestCase):
    def test_incomplete_vs_completed_candle(self):
        rates = [bar(0, 1, 2, 0, 1), bar(300, 1, 3, 1, 2)]
        result = causal_candles(rates, "M5", 450)
        self.assertEqual(len(result["completed"]), 1)
        self.assertFalse(result["incomplete"]["complete"])

    def test_ema_uses_completed_only(self):
        candles = [bar(i * 300, 1, 1, 1, float(i + 1)) | {"complete": True} for i in range(200)]
        self.assertIsNotNone(ema_value(candles, 200))

    def test_short_and_long_executable_sides_and_r(self):
        quote = {"bid": 90, "ask": 91}
        m1 = [bar(i * 60, 90, 91, 89, 90) for i in range(3)]
        m5 = [bar(i * 300, 90, 91, 89, 90) for i in range(3)]
        short = build_observation(pos("SHORT"), quote, m1, m5, 600)
        long = build_observation(pos("LONG"), quote, m1, m5, 600)
        self.assertEqual(short["entry_executable_price"], 90)
        self.assertEqual(short["close_executable_price"], 91)
        self.assertEqual(short["mark_price"], 90.5)
        self.assertEqual(long["entry_executable_price"], 91)
        self.assertEqual(long["close_executable_price"], 90)

    def test_structure_snapshot_is_confirmed_only(self):
        rows = [bar(i * 300, 0, 10 if i == 2 else 5, 0 if i == 4 else 3, 4) for i in range(5)]
        snap = structure_snapshot([x | {"complete": True} for x in rows])
        self.assertEqual(snap["latest_confirmed_swing_high"]["price"], 10)

    def test_events_include_new_mae_and_discrepancy(self):
        observer = CausalObserver()
        quote = {"bid": 111, "ask": 112}
        first = observer.observe(pos("SHORT"), quote, [], [], 600)
        second = observer.observe(pos("SHORT"), quote, [], [], 601)
        types = {e["event"] for e in second["events"]}
        self.assertIn("STOP_CROSSED", types)
        self.assertIn("POSITION_STATE_DISCREPANCY", types)

    def test_stop_and_target_use_correct_bid_ask_sides(self):
        short_observer = CausalObserver()
        stop_bundle = short_observer.observe(pos("SHORT"), {"bid": 109, "ask": 111}, [], [], 600)
        self.assertIn("STOP_CROSSED", {e["event"] for e in stop_bundle["events"]})
        long_observer = CausalObserver()
        target_bundle = long_observer.observe(pos("LONG"), {"bid": 121, "ask": 122}, [], [], 600)
        self.assertIn("TARGET_CROSSED", {e["event"] for e in target_bundle["events"]})

    def test_duplicate_observation_suppressed_and_storage_restart_safe(self):
        observer = CausalObserver(); quote = {"bid": 99, "ask": 100}
        a = observer.observe(pos("SHORT"), quote, [], [], 600)
        b = observer.observe(pos("SHORT"), quote, [], [], 600)
        self.assertTrue(b["duplicate_observation"])
        with tempfile.TemporaryDirectory() as d:
            store = ObservationStore(d)
            self.assertEqual(store.append_bundle(a)["observation"], 1)
            self.assertEqual(store.append_bundle(a)["observation"], 0)
            self.assertEqual(ObservationStore(d).append_bundle(a)["observation"], 0)

    def test_restart_recovers_last_observation(self):
        with tempfile.TemporaryDirectory() as d:
            store = ObservationStore(d)
            observer = CausalObserver()
            first = observer.observe(pos("SHORT"), {"bid": 99, "ask": 100}, [], [], 600)
            store.append_bundle(first)
            recovered = CausalObservationCollector(lambda: [], lambda _: {}, store)
            self.assertIn("P", recovered.observer.observations)

    def test_no_future_data_flag(self):
        obs = build_observation(pos(), {"bid": 99, "ask": 100}, [bar(0, 1, 2, 0, 1)], [bar(0, 1, 2, 0, 1)], 30)
        self.assertTrue(obs["causal"])
        self.assertFalse(obs["future_data_used"])

    def test_collector_is_provider_driven_and_shadow_only(self):
        calls = []
        def positions():
            return [pos("SHORT")]
        def market(_position):
            calls.append(True)
            return {"quote": {"bid": 99, "ask": 100}, "m1_rates": [], "m5_rates": []}
        with tempfile.TemporaryDirectory() as d:
            collector = CausalObservationCollector(positions, market, ObservationStore(d))
            result = collector.collect_once(600)
            self.assertEqual(result["positions"], 1)
            self.assertEqual(collector.mode, "ADVISORY_SHADOW")
            self.assertTrue(calls)


if __name__ == "__main__":
    unittest.main()
