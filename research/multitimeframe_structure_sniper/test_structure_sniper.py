import unittest
from datetime import datetime, timedelta, timezone

from .engine import (Bar, H1Scenario, assert_completed_alignment, build_structure_map,
                     candidate_from_context, candle_patterns, classify_h1_scenario,
                     Compatibility, classify_h4_context, classify_h1_context,
                     compression_geometry, ema_context, fill_cost, m15_confirmations,
                     pip_size, psychological_levels, sniper_trigger,
                     structural_targets, support_resistance_levels)


def bars(tf, values, start=None):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    step = {"M5":5,"M15":15,"H1":60,"H4":240}[tf]
    return [Bar(start+timedelta(minutes=i*step), v[0], v[1], v[2], v[3], tf, spread_points=2, digits=5, point=.00001) for i,v in enumerate(values)]


class StructureSniperTests(unittest.TestCase):
    def test_confirmed_swings_use_only_closed_data(self):
        data = bars("H1", [(1,2,0,1.5),(1.5,3,1,2.5),(2.5,2.7,1.8,2.2),(2.2,2.4,1.2,1.5),(1.5,2,1,1.8)])
        as_of = data[3].close_timestamp
        m = build_structure_map(data, as_of)
        self.assertTrue(all(s.timestamp + timedelta(hours=1) <= as_of for s in m.swings))

    def test_compression_and_support_pressure(self):
        data = bars("H1", [(10,12,9,11),(11,11.8,9.8,10.5),(10.5,11.4,9.9,10.2),(10.2,11.0,9.95,10.4),(10.4,10.7,9.98,10.1),(10.1,10.4,9.99,10.0),(10,10.2,9.7,9.6)])
        c = compression_geometry(data, data[-1].close_timestamp)
        self.assertIn("support_tests", c); self.assertIn("width_atr", c)

    def test_scenario_families_remain_explicit(self):
        h4 = build_structure_map(bars("H4", [(1,2,0,1),(1,2.2,.2,2),(2,3,1,2.5),(2.5,3.2,2,3)]), datetime(2026,1,3,tzinfo=timezone.utc))
        h1 = h4
        c = {"bearish": True, "bullish": False}
        self.assertEqual(classify_h1_scenario(h4,h1,c,broke_level=True), H1Scenario.COMPRESSION_BREAKOUT)
        self.assertEqual(classify_h1_scenario(h4,h1,c,reversal_confirmation=True), H1Scenario.STRUCTURAL_REVERSAL)

    def test_m15_confirmation_is_completed_only(self):
        data = bars("M15", [(1,1.1,.9,1.05),(1.05,1.3,1,1.25),(1.25,1.4,1.2,1.35)])
        result = m15_confirmations(data, data[1].close_timestamp)
        self.assertTrue(all(x["timestamp"] + timedelta(minutes=15) <= data[1].close_timestamp for x in result))

    def test_m5_requires_context_at_call_site_and_mirrors_direction(self):
        data = bars("M5", [(1,1.1,.9,1.02),(1.02,1.2,1,1.15),(1.15,1.3,1.1,1.25)])
        long_trigger = sniper_trigger(data, data[-1].close_timestamp, "LONG", "MICRO_BOS")
        short_trigger = sniper_trigger(data, data[-1].close_timestamp, "SHORT", "MICRO_BOS")
        self.assertIsNotNone(long_trigger)
        self.assertIsNone(short_trigger)
        self.assertIsNone(candidate_from_context(h4=None, h1=None, scenario=None,
                                                 m15_confirmation=None, m5=data,
                                                 as_of=data[-1].close_timestamp,
                                                 direction="LONG", family="MICRO_BOS"))

    def test_context_gate_requires_m15_confirmation(self):
        data = bars("M5", [(1,1.1,.9,1.02),(1.02,1.2,1,1.15),(1.15,1.3,1.1,1.25)])
        h4 = build_structure_map(data, data[-1].close_timestamp)
        h1 = h4
        self.assertIsNone(candidate_from_context(h4=h4, h1=h1, scenario=H1Scenario.TREND_CONTINUATION,
                                                 m15_confirmation=None, m5=data,
                                                 as_of=data[-1].close_timestamp,
                                                 direction="LONG", family="MICRO_BOS"))

    def test_context_and_scenario_semantics_are_distinct(self):
        data = bars("H1", [(1,2,0,1),(1,2.2,.2,2),(2,3,1,2.5),(2.5,3.2,2,3),(3,3.4,2.8,3.2),(3.2,3.6,3,3.5)])
        structure = build_structure_map(data, data[-1].close_timestamp)
        h4 = classify_h4_context(structure, "LONG")
        h1 = classify_h1_context(structure, {"bullish": False}, trade_direction="LONG")
        self.assertIn(h4.compatibility, {Compatibility.ALIGNED, Compatibility.NEUTRAL, Compatibility.OPPOSING})
        self.assertEqual(h1.scenario.value, "UNCLASSIFIED")
        self.assertEqual(h1.compatibility, h4.compatibility)

    def test_alignment_assertion_rejects_partial_htf(self):
        h4 = bars("H4", [(1,2,0,1)])[0]
        h1 = bars("H1", [(1,2,0,1)])[0]
        m15 = bars("M15", [(1,2,0,1)])[0]
        m5 = bars("M5", [(1,2,0,1)])[0]
        with self.assertRaises(AssertionError):
            assert_completed_alignment(m5.timestamp + timedelta(minutes=5), h4=h4, h1=h1, m15=m15, m5=m5)
        assert_completed_alignment(h4.close_timestamp + timedelta(minutes=1), h4=h4, h1=h1, m15=m15, m5=m5)

    def test_structural_targets_and_pips(self):
        t = structural_targets([{"price": 1.2}], 1.1, "LONG", 1.05)
        self.assertEqual(t["structural_target"], 1.2)
        self.assertEqual(pip_size("USDJPYm", 3, .001), .01)
        self.assertEqual(pip_size("EURUSDm", 5, .00001), .0001)

    def test_structural_diagnostics_and_suffix_normalization(self):
        data = bars("H1", [(1,2,0,1),(1,2.2,.2,2),(2,3,1,2.5),(2.5,3.2,2,3)])
        levels = support_resistance_levels(data, data[-1].close_timestamp)
        self.assertIsInstance(levels, list)
        context = ema_context(data, data[-1].close_timestamp, periods=(2,))
        self.assertIn("ema2", context)
        self.assertIn("nearest_50pip", psychological_levels(1.3558, .0001))
        self.assertIn("strong_body_close", candle_patterns(None, data[-1]))

    def test_fill_spread_must_be_at_fill_timestamp(self):
        t = datetime(2026,1,1,tzinfo=timezone.utc)
        good = fill_cost("EURUSDm", fill_timestamp=t, spread_timestamp=t, bid=1.1, ask=1.1002, stop_distance_price=.001, digits=5, point=.00001)
        bad = fill_cost("EURUSDm", fill_timestamp=t, spread_timestamp=t-timedelta(minutes=5), bid=1.1, ask=1.1002, stop_distance_price=.001, digits=5, point=.00001)
        self.assertEqual(good["status"], "OK"); self.assertFalse(good["spread_double_counted"])
        self.assertEqual(bad["status"], "FILL_COST_UNAVAILABLE")

    def test_external_cases_are_observations_only(self):
        from pathlib import Path
        rows = [x for x in (Path(__file__).with_name("external_cases.jsonl")).read_text().splitlines() if x]
        self.assertEqual(len(rows), 2)
        self.assertIn("EXAMPLE_001_USDJPY_LONG", rows[0]); self.assertIn("EXAMPLE_002_GBPUSD_SHORT", rows[1])


if __name__ == "__main__": unittest.main()
