import unittest

from research.multitimeframe_liquidity_sniper.engine import (
    CONTROL_GROUPS, align_completed_timeframes, chronological_splits,
    cost_bucket, inspect_candidate, next_candle_hold, pip_size,
    spread_cost_diagnostic, state_machine_trace,
)
from research.multitimeframe_liquidity_sniper.validation import bounded_replay, validate_alignment


def candle(ts, o, h, l, c, atr=1.0):
    return {"time": ts, "open": o, "high": h, "low": l, "close": c, "atr": atr}


class MultiTimeframeSniperTests(unittest.TestCase):
    def test_only_completed_timeframes_are_aligned(self):
        data = {tf: [candle(0, 1, 2, 0, 1.5), candle(3600, 1, 2, 0, 1.5)] for tf in ("H4", "H1")}
        data["H4"] = [candle(0, 1, 2, 0, 1.5), candle(14400, 1, 2, 0, 1.5)]
        aligned = align_completed_timeframes(data, 4500)
        self.assertEqual(aligned["H1"]["time"], 0)
        self.assertIsNone(aligned["H4"])

    def test_no_lookahead_excludes_forming_m15_and_m5(self):
        data = {
            "H4": [candle(0, 1, 2, 0, 1.5)],
            "H1": [candle(0, 1, 2, 0, 1.5)],
            "M15": [candle(0, 10, 12, 9, 11), candle(900, 11, 15, 10, 14)],
            "M5": [candle(0, 10, 11, 9, 10.5), candle(300, 10.5, 12, 10, 11.5)],
        }
        out = inspect_candidate("EURUSD", data, 899, {
            "liquidity_lookback": 3, "sweep_depth_atr": 0, "reclaim_delay": 0,
            "displacement_body_atr": .25, "bos_delay": 0,
            "structure_lookback": 3,
        })
        self.assertIsNone(out["m15_source_candle_timestamp"])
        self.assertIsNone(out["m5_trigger_timestamp"])

    def test_m5_trigger_cannot_become_valid_without_m15_setup(self):
        data = {"H4": [], "H1": [], "M15": [], "M5": [candle(0, 1, 2, 0, 2)]}
        out = inspect_candidate("EURUSD", data, 1000, {
            "liquidity_lookback": 3, "sweep_depth_atr": 0, "reclaim_delay": 0,
            "displacement_body_atr": .25, "bos_delay": 0, "structure_lookback": 3,
        })
        self.assertEqual(out["status"], "NO_VALID_M15_SETUP_OR_M5_TRIGGER")
        self.assertFalse(any(out["controls"].values()))

    def test_cost_requires_fill_time_spread_and_jpy_pips(self):
        self.assertEqual(pip_size("EURUSD"), .0001)
        self.assertEqual(pip_size("USDJPY"), .01)
        good = spread_cost_diagnostic("USDJPY", {"spread_timestamp": 100, "fill_timestamp": 100, "bid": 150.00, "ask": 150.02}, 149.90, 150.01)
        self.assertEqual(good["status"], "OK")
        self.assertAlmostEqual(good["spread_pips"], 2.0, places=10)
        bad = spread_cost_diagnostic("EURUSD", {"spread_timestamp": 99, "fill_timestamp": 100, "bid": 1.0, "ask": 1.0002}, .999, 1.0)
        self.assertEqual(bad["status"], "FILL_COST_UNAVAILABLE")

    def test_cost_buckets_are_closed_and_explicit(self):
        self.assertEqual(cost_bucket(None), "FILL_COST_UNAVAILABLE")
        self.assertEqual(cost_bucket(.05), "<=0.05R")
        self.assertEqual(cost_bucket(.30), "0.20-0.30R")
        self.assertEqual(cost_bucket(.31), ">0.30R")

    def test_control_groups_are_explicit(self):
        controls = {x: True for x in CONTROL_GROUPS}
        self.assertEqual(tuple(controls), CONTROL_GROUPS)

    def test_chronological_final_split_is_not_used_for_selection(self):
        rows = [{"decision_timestamp": x} for x in range(10)]
        parts = chronological_splits(rows)
        self.assertEqual([x["decision_timestamp"] for x in parts["final_untouched_test"]], [7, 8, 9])

    def test_next_candle_hold_is_strictly_t_plus_one(self):
        touch = {"time": 1000, "level": 1.0}
        self.assertTrue(next_candle_hold(touch, {"time": 1300, "close": 1.1}, "LONG"))
        self.assertFalse(next_candle_hold(touch, {"time": 1600, "close": 1.1}, "LONG"))
        self.assertFalse(next_candle_hold(touch, {"time": 1000, "close": 1.1}, "LONG"))
        self.assertTrue(next_candle_hold(touch, {"time": 1300, "close": .9}, "SHORT"))

    def test_state_machine_has_no_trade_without_m15(self):
        trace = state_machine_trace("EURUSD", None, None)
        self.assertEqual(trace[-1]["state"], "NO_TRADE")
        self.assertEqual(trace[-1]["reason"], "MISSING_M15_SETUP")

    def test_alignment_validator_and_ledger_are_explicit(self):
        data = {tf: [candle(0, 1, 2, 0, 1.5)] for tf in ("H4", "H1", "M15", "M5")}
        self.assertTrue(validate_alignment(data, 14400)["pass"])
        result = bounded_replay({"EURUSD": data}, {"EURUSD": [14400]}, {
            "liquidity_lookback": 3, "sweep_depth_atr": 0, "reclaim_delay": 0,
            "displacement_body_atr": .25, "bos_delay": 0, "structure_lookback": 3,
        })
        self.assertEqual(result["broker_writes"], 0)
        self.assertEqual(result["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
