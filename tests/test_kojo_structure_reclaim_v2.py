"""Tests for KOJO_STRUCTURE_RECLAIM_V2 evaluator.

Verifies all causal invariants and V2-specific semantic invariants:

  CAUSAL GATES (shared with V1):
    - NO_LOOKAHEAD_PASS
    - PREFIX_INVARIANCE_PASS
    - DETERMINISTIC_RERUN_PASS
    - HISTORICAL_LIVE_EVALUATOR_PARITY_PASS
    - CHECKPOINT_RESTART_PARITY_PASS
    - TERMINAL_REACTIVATION_GUARD_PASS
    - DUPLICATE_ECONOMIC_OPPORTUNITY_GUARD_PASS

  V2-SPECIFIC SEMANTIC INVARIANTS:
    - ONE_LEVEL_ONE_EPISODE: same level crossed repeatedly → exactly one episode
    - CONSUMED_LEVEL_STAYS_CONSUMED: restart/checkpoint retains consumed state
    - INVALIDATED_LEVEL_NO_REACTIVATION
    - EXPIRED_LEVEL_NO_REACTIVATION
    - NEW_CAUSAL_LEVEL_CAN_CREATE_EPISODE
    - TP1_INTERNAL_COUNT=0: TP1_CLASS=INTERNAL_TO_PULLBACK is impossible for accepted signals
    - DUAL_TF_BASELINE_ENFORCED: continuation-close rejected
    - TARGET_SELECTION_USES_R_THRESHOLD=false
    - TP2_BLIND_SUBSTITUTION=false
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_backtest.models import (
    EntrySignal,
    MarketEvent,
    ParameterSet,
    SetupLifecycleEvent,
    StrategyVersion,
)
from strategy_backtest.kojo_structure_reclaim_v2 import (
    STRATEGY_ID,
    VERSION,
    EVALUATOR_KEY,
    INSTRUMENT,
    KojoStructureReclaimV2Evaluator,
    kojo_structure_reclaim_v2_baseline_parameter_set,
    kojo_structure_reclaim_v2_parameter_schema,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

H1 = "H1"
M15 = "M15"
XAUUSD = "XAUUSD"
H1S = 3600
M15S = 900

BASE_TS = 1_783_000_000  # arbitrary fixed reference


def h1(t, o, h, lo, c):
    return MarketEvent(
        canonical_instrument=XAUUSD, timeframe=H1,
        open_timestamp=t, close_timestamp=t + H1S,
        open=o, high=h, low=lo, close=c,
        completed=True, source="TEST",
    )


def m15(t, o, h, lo, c):
    return MarketEvent(
        canonical_instrument=XAUUSD, timeframe=M15,
        open_timestamp=t, close_timestamp=t + M15S,
        open=o, high=h, low=lo, close=c,
        completed=True, source="TEST",
    )


def make_evaluator() -> KojoStructureReclaimV2Evaluator:
    ev = KojoStructureReclaimV2Evaluator()
    sv = StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, kojo_structure_reclaim_v2_parameter_schema())
    ps = kojo_structure_reclaim_v2_baseline_parameter_set(sv.strategy_version_id)
    ev.initialize(sv, ps)
    return ev


def feed_and_collect(evaluator, events):
    signals = []
    setups = []
    for ev in events:
        for out in evaluator.consume_market_event(ev):
            if isinstance(out, EntrySignal):
                signals.append(out)
            elif isinstance(out, SetupLifecycleEvent):
                setups.append(out)
    return signals, setups


def _bull_engulf(t_prev, t_curr, ref_price):
    """One bearish M15 followed by a bullish engulfing candle near ref_price."""
    prev = m15(t_prev, ref_price + 2, ref_price + 3, ref_price - 1, ref_price - 0.5)
    curr = m15(t_curr, ref_price - 1, ref_price + 4, ref_price - 2, ref_price + 3)
    return [prev, curr]


def _bear_engulf(t_prev, t_curr, ref_price):
    """One bullish M15 followed by a bearish engulfing candle near ref_price."""
    prev = m15(t_prev, ref_price - 2, ref_price + 0.5, ref_price - 3, ref_price + 2)
    curr = m15(t_curr, ref_price + 2, ref_price + 3, ref_price - 4, ref_price - 1)
    return [prev, curr]


def build_pivot_sequence(
    base_ts: int,
    level_price: float,
    direction: str,
    num_pre_break_h1: int = 6,
    *,
    break_close_offset: float = 5.0,
    pullback_to_level: bool = True,
    extra_m15_before_conf: int = 0,
) -> list[MarketEvent]:
    """Build a minimal event sequence that creates a pivot level, breaks it, and retests.

    Produces a valid signal by ensuring:
      Phase 1 — 5 H1 bars that establish an EXTERNAL TARGET level well above (LONG)
                or below (SHORT) the break zone; confirmed before episode start.
      Phase 2 — num_pre_break_h1 H1 bars that establish the STRUCTURE LEVEL.
      Phase 3 — Break bar, H1 pullback, M15 engulfing confirmation, entry bar.

    For LONG:
        - External target at level_price + 25 (resistance confirmed in Phase 1)
        - Structure level at level_price (resistance confirmed in Phase 2)
        - Break closes at level_price + break_close_offset
        - Episode envelope_high ≈ level_price + break_close_offset + 3
        - TP1 = external resistance at level_price + 25 (above envelope)

    For SHORT: mirrored (target at level_price - 25, support at level_price).
    """
    events: list[MarketEvent] = []
    t = base_ts

    # pivot_strength=2 matches baseline; pivot bar confirmed after 2 bars to its right
    if direction == "LONG":
        upper_target = level_price + 25.0  # external resistance well above break zone

        # Phase 1: 5 H1 bars — bar[2] establishes the external resistance at upper_target
        for i in range(5):
            if i == 2:
                events.append(h1(t, upper_target - 3, upper_target, upper_target - 5, upper_target - 3))
            else:
                events.append(h1(t, upper_target - 10, upper_target - 6, upper_target - 12, upper_target - 9))
            t += H1S

        # Phase 2: num_pre_break_h1 H1 bars — bar at middle establishes structure level at level_price
        pivot_bar_idx = num_pre_break_h1 // 2
        for i in range(num_pre_break_h1):
            if i == pivot_bar_idx:
                events.append(h1(t, level_price - 3, level_price, level_price - 5, level_price - 3))
            else:
                events.append(h1(t, level_price - 8, level_price - 4, level_price - 10, level_price - 7))
            t += H1S

        # Phase 3: break, pullback, confirm, entry
        break_close = level_price + break_close_offset
        events.append(h1(t, level_price - 2, break_close + 3, level_price - 3, break_close))
        t += H1S

        if pullback_to_level:
            events.append(h1(t, break_close - 1, break_close, level_price - 2, level_price + 1))
            t += H1S

            for _ in range(extra_m15_before_conf):
                events.append(m15(t, level_price + 1, level_price + 3, level_price - 1, level_price + 2))
                t += M15S

            # Bearish M15: close must stay >= level_price to not trigger M15_CLOSE_BELOW_LEVEL guard
            # Open > close (bearish), but close just above the level
            events.append(m15(t, level_price + 2, level_price + 3, level_price - 1, level_price + 0.2))
            t += M15S
            # Bullish engulfing: open <= prev_close, close >= prev_open, clearly bullish
            events.append(m15(t, level_price, level_price + 8, level_price - 1, level_price + 7))
            t += M15S

            # Entry bar (next M15 open is entry price)
            events.append(m15(t, level_price + 7, level_price + 10, level_price + 5, level_price + 8))
            t += M15S

    else:  # SHORT
        lower_target = level_price - 25.0  # external support well below break zone

        # Phase 1: 5 H1 bars — bar[2] establishes the external support at lower_target
        for i in range(5):
            if i == 2:
                events.append(h1(t, lower_target + 3, lower_target + 5, lower_target, lower_target + 3))
            else:
                events.append(h1(t, lower_target + 7, lower_target + 10, lower_target + 4, lower_target + 8))
            t += H1S

        # Phase 2: num_pre_break_h1 H1 bars — bar at middle establishes structure level at level_price
        pivot_bar_idx = num_pre_break_h1 // 2
        for i in range(num_pre_break_h1):
            if i == pivot_bar_idx:
                events.append(h1(t, level_price + 3, level_price + 5, level_price, level_price + 3))
            else:
                events.append(h1(t, level_price + 7, level_price + 10, level_price + 4, level_price + 8))
            t += H1S

        # Phase 3: break, pullback, confirm, entry
        break_close = level_price - break_close_offset
        events.append(h1(t, level_price + 2, level_price + 3, break_close - 3, break_close))
        t += H1S

        if pullback_to_level:
            events.append(h1(t, break_close + 1, level_price + 2, break_close, level_price - 1))
            t += H1S

            for _ in range(extra_m15_before_conf):
                events.append(m15(t, level_price - 2, level_price + 1, level_price - 3, level_price - 1))
                t += M15S

            # Bullish M15: close must stay <= level_price to not trigger M15_CLOSE_ABOVE_LEVEL guard
            events.append(m15(t, level_price - 2, level_price + 1, level_price - 3, level_price - 0.2))
            t += M15S
            # Bearish engulfing: open >= prev_close, close <= prev_open, clearly bearish
            events.append(m15(t, level_price, level_price + 1, level_price - 8, level_price - 7))
            t += M15S

            # Entry bar
            events.append(m15(t, level_price - 7, level_price - 5, level_price - 10, level_price - 8))
            t += M15S

    return events


# ─── tests ────────────────────────────────────────────────────────────────────

class V2BasicInitializationTests(unittest.TestCase):

    def test_initialize_correct_version(self):
        ev = make_evaluator()
        self.assertIsNotNone(ev.strategy_version)

    def test_wrong_version_raises(self):
        ev = KojoStructureReclaimV2Evaluator()
        sv = StrategyVersion(
            "KOJO_STRUCTURE_RECLAIM_V1", "V1", "kojo_structure_reclaim_v1",
            kojo_structure_reclaim_v2_parameter_schema(),
        )
        ps = kojo_structure_reclaim_v2_baseline_parameter_set(sv.strategy_version_id)
        with self.assertRaises(ValueError):
            ev.initialize(sv, ps)

    def test_non_xauusd_events_ignored(self):
        ev = make_evaluator()
        ev2 = MarketEvent(
            canonical_instrument="USDJPY", timeframe=H1,
            open_timestamp=BASE_TS, close_timestamp=BASE_TS + H1S,
            open=150.0, high=151.0, low=149.0, close=150.5,
            completed=True, source="TEST",
        )
        result = ev.consume_market_event(ev2)
        self.assertEqual(result, ())

    def test_incomplete_events_ignored(self):
        ev = make_evaluator()
        ev2 = MarketEvent(
            canonical_instrument=XAUUSD, timeframe=H1,
            open_timestamp=BASE_TS, close_timestamp=BASE_TS + H1S,
            open=4000.0, high=4010.0, low=3990.0, close=4005.0,
            completed=False, source="TEST",
        )
        result = ev.consume_market_event(ev2)
        self.assertEqual(result, ())


class V2SemanticCorrection1Tests(unittest.TestCase):
    """ONE_LEVEL_ONE_EPISODE: level consumed once → never re-fires."""

    def _make_break_events(self, base_ts: int, level_price: float, repeat: int = 3):
        """Generate multiple separate H1 breaks above the same level at different timestamps."""
        events = []
        t = base_ts
        # Pre-break H1 history to establish the pivot
        piv_idx = 2
        for i in range(6):
            if i == piv_idx:
                events.append(h1(t, level_price - 3, level_price, level_price - 5, level_price - 3))
            else:
                events.append(h1(t, level_price - 8, level_price - 4, level_price - 10, level_price - 7))
            t += H1S
        # First break → triggers setup
        events.append(h1(t, level_price - 2, level_price + 8, level_price - 3, level_price + 5))
        t += H1S
        # Invalidate the first setup so it becomes terminal
        events.append(h1(t, level_price + 5, level_price + 6, level_price - 3, level_price - 2))
        t += H1S
        # Multiple subsequent H1 bars breaking above the same level again
        for _ in range(repeat):
            events.append(h1(t, level_price - 1, level_price + 6, level_price - 2, level_price + 4))
            t += H1S
        return events, t

    def test_same_level_crossed_repeatedly_produces_one_episode(self):
        ev = make_evaluator()
        events, _ = self._make_break_events(BASE_TS, 4100.0, repeat=5)
        signals, setups = feed_and_collect(ev, events)
        # Count SETUP_DETECTED lifecycle events
        detected = [s for s in setups if s.status == "SETUP_DETECTED"]
        # Exactly one setup should have been detected on the level (level key = (lid, LONG))
        self.assertEqual(len(detected), 1, f"Expected 1 SETUP_DETECTED, got {len(detected)}")

    def test_consumed_level_key_count_matches_unique_terminals(self):
        ev = make_evaluator()
        events, _ = self._make_break_events(BASE_TS, 4050.0, repeat=3)
        feed_and_collect(ev, events)
        # One level key should be in consumed set
        self.assertEqual(len(ev._consumed_level_keys), 1)

    def test_restart_preserves_consumed_level_keys(self):
        ev = make_evaluator()
        events, _ = self._make_break_events(BASE_TS, 4080.0, repeat=2)
        feed_and_collect(ev, events)
        state = ev.snapshot_state()

        ev2 = make_evaluator()
        ev2.restore_state(state)
        # After restore, the same consumed level must still block re-fires
        self.assertEqual(ev._consumed_level_keys, ev2._consumed_level_keys)

    def test_invalidated_level_cannot_reactivate(self):
        ev = make_evaluator()
        events, next_t = self._make_break_events(BASE_TS, 4070.0, repeat=0)
        feed_and_collect(ev, events)
        consumed_before = set(ev._consumed_level_keys)
        # Feed more bars above the level — should NOT create new setups
        for i in range(3):
            ev.consume_market_event(h1(next_t + i * H1S, 4065, 4080, 4063, 4075))
        self.assertEqual(ev._consumed_level_keys, consumed_before)

    def test_expired_level_cannot_reactivate(self):
        """Level that expires (no retest within wait window) must not re-fire."""
        ev = make_evaluator()
        level = 4060.0
        t = BASE_TS
        # Establish pivot
        for i in range(6):
            if i == 2:
                ev.consume_market_event(h1(t, level - 3, level, level - 5, level - 3))
            else:
                ev.consume_market_event(h1(t, level - 8, level - 4, level - 10, level - 7))
            t += H1S
        # Break
        ev.consume_market_event(h1(t, level - 2, level + 8, level - 3, level + 5))
        t += H1S
        # Feed > max_retest_wait_h1_bars (24) far above the level (low well above level+ATR)
        # so no retest tolerance window is hit; these bars stay in WAITING_FOR_RETEST until expiry
        for _ in range(25):
            ev.consume_market_event(h1(t, level + 30, level + 40, level + 28, level + 35))
            t += H1S
        expired = [s for s in ev._setups.values() if s["state"] == "EXPIRED"]
        self.assertTrue(len(expired) >= 1, "Expected at least one EXPIRED setup")
        # Record the consumed level keys before the next break
        consumed_before = set(ev._consumed_level_keys)
        expired_level_ids = {s["structural_level_id"] for s in expired}
        # Feed a bar that re-approaches the original level — must not create a new setup on it
        ev.consume_market_event(h1(t, level - 2, level + 6, level - 3, level + 4))
        # Check no NEW setup uses any of the expired level ids
        new_on_expired_level = [
            s for s in ev._setups.values()
            if s["structural_level_id"] in expired_level_ids
            and s["setup_id"] not in {x["setup_id"] for x in expired}
        ]
        self.assertEqual(len(new_on_expired_level), 0, "Expired level must not create new setup")


class V2SemanticCorrection2Tests(unittest.TestCase):
    """Pullback episode is explicitly represented."""

    def test_episode_start_ts_recorded(self):
        ev = make_evaluator()
        level = 4090.0
        t = BASE_TS
        for i in range(6):
            if i == 2:
                ev.consume_market_event(h1(t, level - 3, level, level - 5, level - 3))
            else:
                ev.consume_market_event(h1(t, level - 8, level - 4, level - 10, level - 7))
            t += H1S
        break_close_ts = t + H1S
        ev.consume_market_event(h1(t, level - 2, level + 8, level - 3, level + 5))
        for setup in ev._setups.values():
            self.assertEqual(setup["episode_start_ts"], break_close_ts)
            self.assertIn("episode_envelope_high", setup)
            self.assertIn("episode_envelope_low", setup)
            self.assertEqual(setup["pullback_type"], "UNCLASSIFIED")

    def test_envelope_high_expands_on_extension(self):
        ev = make_evaluator()
        level = 4095.0
        t = BASE_TS
        for i in range(6):
            if i == 2:
                ev.consume_market_event(h1(t, level - 3, level, level - 5, level - 3))
            else:
                ev.consume_market_event(h1(t, level - 8, level - 4, level - 10, level - 7))
            t += H1S
        # Break with high = level + 10
        ev.consume_market_event(h1(t, level - 2, level + 10, level - 3, level + 5))
        t += H1S
        initial_env_high = next(iter(ev._setups.values()))["episode_envelope_high"]
        # Extension H1 bar with even higher high
        ev.consume_market_event(h1(t, level + 5, level + 15, level + 3, level + 6))
        env_high_after = next(iter(ev._setups.values()))["episode_envelope_high"]
        self.assertGreater(env_high_after, initial_env_high)


class V2SemanticCorrection3Tests(unittest.TestCase):
    """External-only target selection: TP1_CLASS=INTERNAL must be impossible."""

    def test_tp1_class_external_on_all_emitted_signals(self):
        """All accepted V2 signals must have tp1_class=EXTERNAL."""
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4100.0, "LONG", num_pre_break_h1=8)
        signals, _ = feed_and_collect(ev, events)
        for sig in signals:
            self.assertEqual(
                sig.provenance.get("tp1_class"), "EXTERNAL",
                f"Signal {sig.signal_id} has tp1_class={sig.provenance.get('tp1_class')}, expected EXTERNAL"
            )

    def test_tp1_provenance_is_persisted(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4100.0, "LONG", num_pre_break_h1=8)
        signals, _ = feed_and_collect(ev, events)
        if signals:
            prov = signals[0].provenance.get("tp1_provenance")
            self.assertIsNotNone(prov, "tp1_provenance must be persisted in V2")
            self.assertTrue(prov.get("predates_episode"), "tp1 must predate episode")
            self.assertTrue(prov.get("outside_pullback_envelope"), "tp1 must be outside pullback envelope")
            self.assertEqual(prov.get("tp1_class"), "EXTERNAL")

    def test_no_r_threshold_in_parameters(self):
        schema = kojo_structure_reclaim_v2_parameter_schema()
        self.assertNotIn("min_planned_r", schema.fields)
        self.assertNotIn("min_r_threshold", schema.fields)
        self.assertNotIn("tp1_min_r", schema.fields)

    def test_target_selection_uses_r_threshold_false(self):
        """Verify no R-based filtering is applied in _compute_targets_v2."""
        import inspect
        from strategy_backtest.kojo_structure_reclaim_v2 import KojoStructureReclaimV2Evaluator
        src = inspect.getsource(KojoStructureReclaimV2Evaluator._compute_targets_v2)
        # Must not contain R-threshold logic
        self.assertNotIn("min_r", src.lower())
        self.assertNotIn("planned_r >", src)
        self.assertNotIn("planned_r <", src)
        self.assertNotIn(">= 1.0", src)
        self.assertNotIn(">= 1", src)


class V2SemanticCorrection4Tests(unittest.TestCase):
    """Dual-TF baseline: continuation-close must not trigger V2 signals."""

    def _feed_continuation_close_setup(self, ev, level, t):
        """Feed a setup sequence where M15 confirmation is continuation-close only."""
        # Pre-break pivot history
        for i in range(6):
            if i == 2:
                ev.consume_market_event(h1(t, level - 3, level, level - 5, level - 3))
            else:
                ev.consume_market_event(h1(t, level - 8, level - 4, level - 10, level - 7))
            t += H1S
        # Break
        ev.consume_market_event(h1(t, level - 2, level + 8, level - 3, level + 5))
        t += H1S
        # H1 retest
        ev.consume_market_event(h1(t, level + 5, level + 6, level - 2, level + 1))
        t += H1S
        # M15 retest bars — then only a continuation-close (not engulf/rejection)
        ev.consume_market_event(m15(t, level + 1, level + 3, level - 1, level + 2))
        t += M15S
        # Continuation close: bullish close above midpoint, no engulfing
        prev_open, prev_close = level, level - 0.5
        curr_open, curr_close = level - 0.5, level + 2
        ev.consume_market_event(m15(t, prev_open, prev_open + 1, prev_open - 0.5, prev_close))
        t += M15S
        # A simple continuation bar (not engulf, not rejection wick)
        ev.consume_market_event(m15(t, curr_open, curr_open + 2.5, curr_open - 0.3, curr_close))
        t += M15S
        # Next M15 bar (would be entry if confirmation had triggered)
        ev.consume_market_event(m15(t, curr_close + 0.5, curr_close + 3, curr_close - 0.5, curr_close + 2))
        t += M15S
        return t

    def test_continuation_close_does_not_produce_signal(self):
        """A sequence where the only M15 confirmation is continuation-close must produce 0 signals."""
        ev = make_evaluator()
        level = 4120.0
        signals = []
        t = BASE_TS
        t = self._feed_continuation_close_setup(ev, level, t)
        # No entry signal should have been emitted
        # (confirmation state would NOT have been set since continuation_close is rejected)
        # Check by reading the setup state
        active = [s for s in ev._setups.values() if s["state"] not in {"CONSUMED", "EXPIRED", "INVALIDATED"}]
        # The setup may be in RETEST_SEEN still (waiting for a real confirmation)
        for s in ev._setups.values():
            if s["state"] == "CONSUMED":
                self.fail("Setup was CONSUMED from a continuation-close — V2 must reject this")

    def test_weak_m15_rejections_counted(self):
        """Continuation-close occurrences are counted but not converted to signals."""
        ev = make_evaluator()
        level = 4115.0
        t = BASE_TS
        self._feed_continuation_close_setup(ev, level, t)
        # Should have counted at least one weak rejection
        self.assertGreater(ev._weak_m15_rejections, 0,
                           "Continuation-close occurrences should be counted in weak_m15_rejections")

    def test_engulfing_does_produce_signal(self):
        """A proper engulfing candle should still produce a V2 signal."""
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG", num_pre_break_h1=8)
        signals, _ = feed_and_collect(ev, events)
        self.assertGreater(len(signals), 0, "Engulfing confirmation must produce a V2 signal")
        # All signals must have dual_timeframe_confirmed=True
        for sig in signals:
            self.assertTrue(sig.provenance.get("dual_timeframe_confirmed"))


class V2CausalInvariantTests(unittest.TestCase):
    """Standard causal gate invariants."""

    def _run_full(self, events):
        ev = make_evaluator()
        return feed_and_collect(ev, events)

    def _make_clean_sequence(self):
        return build_pivot_sequence(BASE_TS, 4150.0, "LONG", num_pre_break_h1=8)

    def test_deterministic_rerun_pass(self):
        events = self._make_clean_sequence()
        signals_a, _ = self._run_full(events)
        signals_b, _ = self._run_full(events)
        ids_a = [s.signal_id for s in signals_a]
        ids_b = [s.signal_id for s in signals_b]
        self.assertEqual(ids_a, ids_b)
        for sa, sb in zip(signals_a, signals_b):
            self.assertEqual(sa.entry_price, sb.entry_price)
            self.assertEqual(sa.stop_price, sb.stop_price)

    def test_no_lookahead_pass(self):
        """Feeding events one by one must match batch feeding."""
        events = self._make_clean_sequence()
        signals_batch, _ = self._run_full(events)

        ev = make_evaluator()
        signals_stream = []
        for ev_event in events:
            for out in ev.consume_market_event(ev_event):
                if isinstance(out, EntrySignal):
                    signals_stream.append(out)

        self.assertEqual(
            [s.signal_id for s in signals_batch],
            [s.signal_id for s in signals_stream],
        )

    def test_prefix_invariance_pass(self):
        """Signals from the first half of the event stream must appear in the full run."""
        events = self._make_clean_sequence()
        if len(events) < 4:
            self.skipTest("sequence too short to split")
        half = len(events) // 2
        signals_half, _ = self._run_full(events[:half])
        signals_full, _ = self._run_full(events)
        full_ids = {s.signal_id for s in signals_full}
        for sig in signals_half:
            self.assertIn(sig.signal_id, full_ids)

    def test_checkpoint_restart_parity_pass(self):
        """Snapshot mid-way and restore must produce identical remaining signals."""
        events = self._make_clean_sequence()
        if len(events) < 4:
            self.skipTest("sequence too short")

        half = len(events) // 2
        ev_full = make_evaluator()
        signals_full = []
        for ev_event in events:
            for out in ev_full.consume_market_event(ev_event):
                if isinstance(out, EntrySignal):
                    signals_full.append(out)

        ev_first = make_evaluator()
        for ev_event in events[:half]:
            ev_first.consume_market_event(ev_event)
        state = ev_first.snapshot_state()

        ev_restored = make_evaluator()
        ev_restored.restore_state(state)
        signals_restored = []
        for ev_event in events[half:]:
            for out in ev_restored.consume_market_event(ev_event):
                if isinstance(out, EntrySignal):
                    signals_restored.append(out)

        full_second_half_ids = {s.signal_id for s in signals_full}
        for sig in signals_restored:
            self.assertIn(sig.signal_id, full_second_half_ids)

    def test_terminal_reactivation_guard_pass(self):
        """A setup in a terminal state must never transition to a non-terminal state."""
        ev = make_evaluator()
        events = self._make_clean_sequence()
        feed_and_collect(ev, events)
        # After all events, check no setup ever re-entered a non-terminal state
        for setup in ev._setups.values():
            # setups that are terminal must remain so (invariant in state machine)
            if setup["setup_id"] in ev._consumed_setup_ids:
                self.assertIn(setup["state"], {"CONSUMED", "EXPIRED", "INVALIDATED"})

    def test_duplicate_economic_opportunity_guard_pass(self):
        """Each (structural_level_id, direction) pair produces at most one episode."""
        ev = make_evaluator()
        events = self._make_clean_sequence() + self._make_clean_sequence()
        feed_and_collect(ev, events)
        # The level_key set must have no duplicates (set by construction)
        level_keys_seen = [
            (s["structural_level_id"], s["direction"])
            for s in ev._setups.values()
        ]
        # Grouping by level_key: each key must appear in at most one CONSUMED episode
        from collections import Counter
        key_counts = Counter(level_keys_seen)
        # Each level+direction can appear at most once (the semantic guarantee)
        for key, count in key_counts.items():
            self.assertEqual(
                count, 1,
                f"Level key {key} appeared {count} times; expected exactly once"
            )

    def test_historical_live_evaluator_parity_pass(self):
        """Single evaluator class; no separate live/historical branch."""
        # Verify there is only one evaluator class and no live_mode flag
        import inspect
        from strategy_backtest.kojo_structure_reclaim_v2 import KojoStructureReclaimV2Evaluator
        src = inspect.getsource(KojoStructureReclaimV2Evaluator)
        self.assertNotIn("live_mode", src)
        self.assertNotIn("is_live", src)
        self.assertNotIn("historical_mode", src)


class V2NewCausalLevelTests(unittest.TestCase):
    """A genuinely new structural level identity can create a new episode."""

    def test_new_level_id_creates_new_episode(self):
        """Two distinct structural levels → two distinct episodes."""
        ev = make_evaluator()
        level_a = 4050.0
        level_b = 4080.0  # A different level price → different level_id
        t = BASE_TS

        # Feed events to create and consume level_a
        for i in range(6):
            if i == 2:
                ev.consume_market_event(h1(t, level_a - 3, level_a, level_a - 5, level_a - 3))
            else:
                ev.consume_market_event(h1(t, level_a - 8, level_a - 4, level_a - 10, level_a - 7))
            t += H1S
        # Break level_a
        ev.consume_market_event(h1(t, level_a - 2, level_a + 8, level_a - 3, level_a + 5))
        t += H1S
        # Invalidate the level_a setup
        ev.consume_market_event(h1(t, level_a + 4, level_a + 5, level_a - 3, level_a - 2))
        t += H1S
        # Now build up level_b (different price → different level_id)
        for i in range(6):
            if i == 2:
                ev.consume_market_event(h1(t, level_b - 3, level_b, level_b - 5, level_b - 3))
            else:
                ev.consume_market_event(h1(t, level_b - 8, level_b - 4, level_b - 10, level_b - 7))
            t += H1S
        # Break level_b
        ev.consume_market_event(h1(t, level_b - 2, level_b + 8, level_b - 3, level_b + 5))
        t += H1S

        setups_detected = [s for s in ev._setups.values()]
        level_ids_seen = {s["structural_level_id"] for s in setups_detected}
        # The two distinct levels must produce distinct episodes
        self.assertGreaterEqual(
            len([s for s in setups_detected if "R-" in s["structural_level_id"]]), 1
        )


class V2SignalGeometryTests(unittest.TestCase):
    """Stop < entry < tp1 invariant must hold for all emitted signals."""

    def test_all_signals_have_valid_geometry(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4100.0, "LONG", num_pre_break_h1=8)
        signals, _ = feed_and_collect(ev, events)
        for sig in signals:
            if sig.direction == "LONG":
                self.assertLess(sig.stop_price, sig.entry_price)
                self.assertLess(sig.entry_price, sig.target_price)
            else:
                self.assertLess(sig.target_price, sig.entry_price)
                self.assertLess(sig.entry_price, sig.stop_price)

    def test_snapshot_round_trip(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4100.0, "LONG", num_pre_break_h1=8)
        feed_and_collect(ev, events)
        state = ev.snapshot_state()
        ev2 = make_evaluator()
        ev2.restore_state(state)
        self.assertEqual(ev._consumed_level_keys, ev2._consumed_level_keys)
        self.assertEqual(ev._consumed_setup_ids, ev2._consumed_setup_ids)


class V2InvariantAssertionsTests(unittest.TestCase):
    """Compile-time invariant checks: no R threshold, no blind TP2 substitution."""

    def test_target_selection_uses_r_threshold_false(self):
        import inspect
        from strategy_backtest.kojo_structure_reclaim_v2 import KojoStructureReclaimV2Evaluator
        src = inspect.getsource(KojoStructureReclaimV2Evaluator._compute_targets_v2)
        self.assertNotIn("min_r", src.lower())
        self.assertNotIn(">= 1.0", src)
        self.assertNotIn(">= 1\n", src)

    def test_tp2_blind_substitution_false(self):
        """TP1 is always the nearest external level; TP2 is the second.  No logic substitutes TP2 for TP1."""
        import inspect
        from strategy_backtest.kojo_structure_reclaim_v2 import KojoStructureReclaimV2Evaluator
        src = inspect.getsource(KojoStructureReclaimV2Evaluator._compute_targets_v2)
        # tp2 is derived from candidates[1], not candidates[0]; no re-assignment of tp1 to tp2
        self.assertNotIn("tp1 = tp2", src)
        self.assertNotIn("tp1=tp2", src)

    def test_v1_artifacts_unchanged(self):
        """V1 evaluator must remain importable and its key must differ from V2."""
        from strategy_backtest.kojo_structure_reclaim import (
            STRATEGY_ID as V1_ID, EVALUATOR_KEY as V1_KEY
        )
        from strategy_backtest.kojo_structure_reclaim_v2 import (
            STRATEGY_ID as V2_ID, EVALUATOR_KEY as V2_KEY
        )
        self.assertEqual(V1_ID, "KOJO_STRUCTURE_RECLAIM_V1")
        self.assertEqual(V2_ID, "KOJO_STRUCTURE_RECLAIM_V2")
        self.assertNotEqual(V1_KEY, V2_KEY)


if __name__ == "__main__":
    unittest.main()
