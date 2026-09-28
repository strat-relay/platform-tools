"""TM-STRUCTURE-1 (trade_management/tm_structure.py) and its decision-engine dispatch.

LONG trade: entry 100, stop 90 (risk 10), target 120 unless stated. Bars are synthetic and completed
before `as_of` unless a test is about causality.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from trade_management.binding import ChainedResolver, DefaultTmNoneResolver, LegacyStaticResolver
from trade_management.decision_engine import record_decision
from trade_management.fakes import FakeConnection
from trade_management.managed_trade import create_managed_trade
from trade_management.tm_structure import StructurePolicy, TmStructureEvaluator, confirmed_swings
from trade_management.versions import ACTION_VOCABULARY_V2, TM_NONE_1_MANIFEST, tm_structure_manifest
from test_trade_management_decision_engine import (BREAKEVEN_ID, NOW, TM_NONE_ID, make_breakeven_trail_trade,
                                                   observe, parameters_of)

ENTRY_TIME = 1_790_000_000.0
AS_OF = ENTRY_TIME + 6 * 3600
POLICY = StructurePolicy()


def bars(tf: str, lows_highs_closes: list[tuple[float, float, float]], *, start: float, step: int) -> list[dict]:
    return [{"time": start + i * step, "open": c, "high": h, "low": l, "close": c}
            for i, (l, h, c) in enumerate(lows_highs_closes)]


def flat(n: int, price: float) -> list[tuple[float, float, float]]:
    return [(price - 0.5, price + 0.5, price)] * n


def m5_with_swing_low(low: float, *, level: float = 110.0, start: float = ENTRY_TIME + 600) -> list[dict]:
    rows = flat(3, level) + [(level - 1, level + 0.5, level), (low, level, level), (level - 1, level + 0.5, level)] + flat(3, level)
    return bars("M5", rows, start=start, step=300)


def m15(rows: list[tuple[float, float, float]], start: float = AS_OF - 900 * 20) -> list[dict]:
    return bars("M15", rows, start=start, step=900)


def evaluate(*, bid, current_stop=90.0, current_target=120.0, m5=None, m15_rows=None, h1=None, direction="LONG",
             entry=100.0, risk=10.0, spread=0.2, as_of=AS_OF, policy=POLICY):
    data = None if m5 is None and m15_rows is None else {"M5": m5 or [], "M15": m15(m15_rows) if m15_rows else [], "H1": h1 or []}
    return TmStructureEvaluator().evaluate(direction=direction, entry=entry, risk_distance=risk, current_stop=current_stop,
                                           current_target=current_target, bid=bid, ask=bid + spread, trade_state="OPEN",
                                           entry_time=ENTRY_TIME, as_of=as_of, bars=data, policy=policy)


QUIET_M15 = flat(20, 104.0)


class SwingTests(unittest.TestCase):
    def test_swings_need_two_closed_bars_each_side(self):
        rows = [{"time": i, "low": v, "high": v + 1} for i, v in enumerate([5, 4, 3, 4, 5, 2, 9])]
        self.assertEqual(confirmed_swings(rows, "low"), [(2.0, 3.0)])      # the 2 at index 5 is not confirmed yet


class BreakevenTests(unittest.TestCase):
    def test_holds_below_triggers(self):
        action, reasons, _ = evaluate(bid=102.0, m5=m5_with_swing_low(95.0), m15_rows=QUIET_M15)
        self.assertEqual((action, reasons), ("HOLD", ("BREAKEVEN_TRIGGER_NOT_REACHED",)))

    def test_breakeven_at_one_r_includes_the_spread(self):
        action, reasons, params = evaluate(bid=110.0, m5=m5_with_swing_low(95.0), m15_rows=QUIET_M15)
        self.assertEqual((action, reasons), ("MOVE_TO_BREAKEVEN", ("BREAKEVEN_TRIGGER_REACHED",)))
        self.assertAlmostEqual(params["new_stop"], 100.2)

    def test_early_breakeven_needs_a_fresh_swing_above_entry(self):
        action, reasons, _ = evaluate(bid=106.0, m5=m5_with_swing_low(103.0), m15_rows=QUIET_M15)
        self.assertEqual((action, reasons), ("MOVE_TO_BREAKEVEN", ("BREAKEVEN_STRUCTURE_CONFIRMED",)))
        before_entry = m5_with_swing_low(103.0, start=ENTRY_TIME - 6000)
        self.assertEqual(evaluate(bid=106.0, m5=before_entry, m15_rows=QUIET_M15)[0], "HOLD")

    def test_short_mirrors(self):
        action, _, params = evaluate(direction="SHORT", entry=100.0, bid=89.8, current_stop=110.0, current_target=80.0,
                                     m5=m5_with_swing_low(95.0, level=90.0), m15_rows=flat(20, 96.0))
        self.assertEqual(action, "MOVE_TO_BREAKEVEN")
        self.assertAlmostEqual(params["new_stop"], 99.8)                   # entry - spread


class TargetAndExitTests(unittest.TestCase):
    AGAINST = flat(14, 107.0) + [(106.0, 108.0, 107.0), (105.0, 107.0, 106.0), (104.0, 106.0, 105.0),
                                 (105.0, 107.0, 106.0), (106.0, 107.0, 106.5), (103.5, 104.8, 103.8)]

    def test_weakness_before_breakeven_pulls_the_target_in(self):
        action, reasons, params = evaluate(bid=106.0, m5=m5_with_swing_low(95.0), m15_rows=self.AGAINST)
        self.assertEqual((action, reasons), ("MOVE_TARGET", ("TARGET_TIGHTENED_ON_WEAKNESS",)))
        self.assertAlmostEqual(params["new_target"], 108.5)                 # price + 0.25R

    def test_structure_break_against_a_protected_trade_exits(self):
        action, reasons, _ = evaluate(bid=103.9, current_stop=100.2, m5=m5_with_swing_low(95.0), m15_rows=self.AGAINST)
        self.assertEqual((action, reasons), ("EXIT", ("STRUCTURE_BREAK_AGAINST",)))

    def test_never_exits_a_trade_below_breakeven(self):
        self.assertNotEqual(evaluate(bid=103.9, current_stop=90.0, current_target=None, m5=m5_with_swing_low(95.0),
                                     m15_rows=self.AGAINST)[0], "EXIT")

    def test_strength_extends_the_target_to_the_next_h1_liquidity_and_locks_profit(self):
        m15_up = flat(14, 108.0) + [(107.0, 109.0, 108.0), (107.5, 110.0, 109.0), (107.0, 109.0, 108.0),
                                    (106.5, 108.5, 107.0), (107.0, 108.8, 108.0), (108.0, 111.5, 111.0)]
        h1 = bars("H1", flat(3, 110.0) + [(110, 114, 112), (111, 116, 113), (110, 114, 112)] + flat(3, 110.0)
                  + [(125, 128, 126), (126, 131, 128), (125, 128, 126)] + flat(3, 110.0), start=AS_OF - 3600 * 20, step=3600)
        action, reasons, params = evaluate(bid=111.5, current_stop=100.2, current_target=112.0,
                                           m5=m5_with_swing_low(95.0, level=111.0), m15_rows=m15_up, h1=h1)
        self.assertEqual((action, reasons), ("MOVE_TARGET", ("TARGET_EXTENDED_TO_NEXT_LIQUIDITY",)))
        self.assertEqual(params["new_target"], 116.0)                      # nearest H1 swing high beyond 112
        self.assertAlmostEqual(params["new_stop"], 105.75)                 # entry + min(1R, 0.575R)


class TrailTests(unittest.TestCase):
    def test_trails_behind_the_latest_m5_swing(self):
        action, reasons, params = evaluate(bid=112.0, current_stop=100.2, m5=m5_with_swing_low(108.0, level=112.0),
                                           m15_rows=flat(20, 111.0))
        self.assertEqual((action, reasons), ("TRAIL_STOP", ("TRAIL_STOP_TO_SWING",)))
        self.assertAlmostEqual(params["new_stop"], 107.0)                  # swing - 0.1R

    def test_never_trails_too_close_to_price_or_backwards(self):
        close = evaluate(bid=112.0, current_stop=100.2, m5=m5_with_swing_low(111.0, level=112.5), m15_rows=flat(20, 111.0))
        self.assertEqual(close[0], "HOLD")                                  # 110 is < 0.25R below 112
        backwards = evaluate(bid=112.0, current_stop=108.0, m5=m5_with_swing_low(108.0, level=112.0), m15_rows=flat(20, 111.0))
        self.assertEqual(backwards[0], "HOLD")                              # 107 would loosen the stop


class DegradedAndCausalTests(unittest.TestCase):
    def test_without_bars_only_r_breakeven(self):
        self.assertEqual(evaluate(bid=110.0)[:2], ("MOVE_TO_BREAKEVEN", ("BREAKEVEN_TRIGGER_REACHED", "BARS_UNAVAILABLE")))
        self.assertEqual(evaluate(bid=112.0, current_stop=100.2)[:2], ("HOLD", ("BARS_UNAVAILABLE",)))

    def test_bars_not_closed_by_the_observation_are_ignored(self):
        future = m5_with_swing_low(108.0, level=112.0, start=AS_OF - 300 * 5)
        self.assertEqual(evaluate(bid=112.0, current_stop=100.2, m5=future, m15_rows=flat(20, 111.0))[0], "HOLD")

    def test_policy_validation(self):
        for bad in ({"early_breakeven_r": 2.0}, {"min_trail_gap_r": 0}, {"extend_progress": 1.5}):
            with self.subTest(bad), self.assertRaises(ValueError):
                StructurePolicy(**bad)


class ManifestTests(unittest.TestCase):
    def test_structure_version_declares_its_own_vocabulary_and_existing_ids_are_unchanged(self):
        manifest = tm_structure_manifest()
        self.assertEqual(manifest.identity_manifest()["action_vocabulary_version"], ACTION_VOCABULARY_V2)
        self.assertEqual(TM_NONE_1_MANIFEST.tm_version_id(), TM_NONE_ID)
        self.assertEqual(TM_NONE_ID, "TMV_ecaca5f080f9f79bb18cc936")     # the production TM-NONE-1 id


class EngineTests(unittest.TestCase):
    def test_a_hold_after_breakeven_never_resets_the_stop(self):
        conn = FakeConnection()
        trade = make_breakeven_trail_trade(conn)                            # entry 100, stop 99, trail at 1.5R
        first = record_decision(conn, observation_id=observe(conn, trade, bid=101.0, ask=101.2, ts="2026-09-22T12:00:00.000Z"),
                                event_id="e1", now_utc=NOW)
        hold = record_decision(conn, observation_id=observe(conn, trade, bid=100.5, ask=100.7, ts="2026-09-22T12:05:00.000Z"),
                               event_id="e2", now_utc=NOW)
        again = record_decision(conn, observation_id=observe(conn, trade, bid=101.0, ask=101.2, ts="2026-09-22T12:10:00.000Z"),
                                event_id="e3", now_utc=NOW)
        self.assertEqual((first.action, hold.action, again.action), ("MOVE_TO_BREAKEVEN", "HOLD", "HOLD"))
        self.assertIn("TRAIL_TRIGGER_NOT_REACHED", again.reason_codes)      # evaluated from the breakeven stop

    def test_structure_version_dispatch_uses_injected_bars(self):
        conn = FakeConnection()
        manifest = tm_structure_manifest()
        version_id = manifest.tm_version_id()
        conn.seed_tm_version(tm_version_id=version_id, manifest_hash=manifest.manifest_hash(),
                             evaluator_id="tm-structure.v1", manifest=json.loads(json.dumps(manifest.identity_manifest())))
        conn.seed_entry_signal(signal_id="SIG_S", strategy_id="STRAT_S", strategy_version="V1", strategy_ref="STRAT_S@V1",
                               parameter_set_ref=None, parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID",
                               strategy_instance_id="inst-1", instrument="XAUUSD", direction="LONG",
                               decision_time="2026-09-22T11:00:00Z", entry_price=100.0, stop_price=90.0,
                               risk_distance=10.0, target_price=120.0, entry_signal_hash="HASH_S")
        conn.seed_legacy_binding(binding_id="BIND_S", strategy_id="STRAT_S", tm_version_id=version_id,
                                 valid_from="2026-09-01T00:00:00Z", binding_hash="h")
        created = create_managed_trade(conn, event_id="evt-S", signal_id="SIG_S", now_utc=NOW,
                                       resolver=ChainedResolver([LegacyStaticResolver(), DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)]))
        self.assertEqual(created.tm_version_id, version_id)
        seen = []

        def provider(instrument, timeframe):
            seen.append((instrument, timeframe))
            return []                                                       # no bars: R-only breakeven
        result = record_decision(conn, observation_id=observe(conn, created.managed_trade_id, bid=110.0, ask=110.2),
                                 event_id="e1", now_utc=NOW, bars_provider=provider)
        self.assertEqual(result.action, "MOVE_TO_BREAKEVEN")
        self.assertEqual({tf for _, tf in seen}, {"M5", "M15", "H1"})
        row = conn.tables["trade_management.trade_manager_decision"][result.decision_id]
        self.assertAlmostEqual(parameters_of(row)["new_stop"], 100.2)


if __name__ == "__main__":
    unittest.main()
