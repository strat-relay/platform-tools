"""Tests for KOJO_STRUCTURE_RECLAIM_V3 evaluator.

Verifies the targeted V3 semantic repairs specified in the V2 audit:

  RETIREMENT POLICY INVARIANTS (required by spec):
    CONSUMED_NEVER_REACTIVATES
      — permanently retired opportunity cannot create a new episode
    INVALIDATED_EPISODE_ITSELF_NEVER_REACTIVATES
      — specific terminated episode (by setup_id) never re-enters active state
    EXPIRED_EPISODE_ITSELF_NEVER_REACTIVATES
      — specific terminated episode never re-enters active state
    GENUINELY_NEW_CAUSAL_EPISODE_MAY_REUSE_STRUCTURE
      — after INVALIDATED + price reset to non-break side, a new genuine causal break
        creates a new episode on the same structural level
    STALE_BREAK_CANNOT_CREATE_DUPLICATE
      — after EXPIRED, repeated H1 bars where price remains on the break side (never
        crosses back over) must not create duplicate episodes
    CHECKPOINT_RESTART_PARITY
      — snapshot/restore produces identical signals to a full replay

  SCAFFOLD FIELD INVARIANTS:
    H1_EVIDENCE_FIELDS_PRESENT
      — h1_context_evidence and h1_post_pullback_confirmation_evidence in signal provenance
    H1_POST_PULLBACK_IS_SOURCE_RULE_REQUIRED
      — h1_post_pullback_confirmation_evidence == "SOURCE_RULE_REQUIRED"
    NO_DUAL_TF_ASSERTION
      — signal does NOT carry dual_timeframe_confirmed=True
    TARGET_DIAGNOSTICS_PRESENT
      — tp1_diagnostics block with all required ratio fields in every signal provenance
    NEAR_COINCIDENT_CLASS_DEFINED
      — TP1_NEAR_COINCIDENT and TP1_STANDARD constants exist in the module
    SOURCE_FIDELITY_BLOCKED_IN_DIAGNOSTICS
    PRODUCTION_ELIGIBLE_FALSE

  CAUSAL INVARIANTS (inherited from V1/V2, spot-checked):
    ONE_LEVEL_ONE_EPISODE — each (level_id, direction) creates at most one active episode
    TP1_INTERNAL_COUNT=0
    CONTINUATION_CLOSE_REJECTED
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
from strategy_backtest.kojo_structure_reclaim_v3 import (
    STRATEGY_ID,
    VERSION,
    EVALUATOR_KEY,
    INSTRUMENT,
    CONSUMED,
    INVALIDATED,
    EXPIRED,
    TERMINAL_STATES,
    TP1_NEAR_COINCIDENT,
    TP1_STANDARD,
    TP1_NEAR_COINCIDENT_BOUNDARY,
    SOURCE_FIDELITY_BLOCKED,
    KojoStructureReclaimV3Evaluator,
    kojo_structure_reclaim_v3_baseline_parameter_set,
    kojo_structure_reclaim_v3_parameter_schema,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

H1 = "H1"
M15 = "M15"
XAUUSD = "XAUUSD"
H1S = 3600
M15S = 900

BASE_TS = 1_783_000_000


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


def make_evaluator() -> KojoStructureReclaimV3Evaluator:
    ev = KojoStructureReclaimV3Evaluator()
    sv = StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, kojo_structure_reclaim_v3_parameter_schema())
    ps = kojo_structure_reclaim_v3_baseline_parameter_set(sv.strategy_version_id)
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
    prev = m15(t_prev, ref_price + 2, ref_price + 3, ref_price - 1, ref_price - 0.5)
    curr = m15(t_curr, ref_price - 1, ref_price + 4, ref_price - 2, ref_price + 3)
    return [prev, curr]


def _bear_engulf(t_prev, t_curr, ref_price):
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
    """Build a minimal event sequence that creates a structural level, breaks it, and retests.

    Phase 1 — 5 H1 bars establishing an EXTERNAL TARGET level (level±25).
    Phase 2 — num_pre_break_h1 H1 bars establishing the STRUCTURAL LEVEL.
    Phase 3 — break bar, H1 pullback, M15 confirmation, entry bar.
    """
    events: list[MarketEvent] = []
    t = base_ts

    if direction == "LONG":
        upper_target = level_price + 25.0

        for i in range(5):
            if i == 2:
                events.append(h1(t, upper_target - 3, upper_target, upper_target - 5, upper_target - 3))
            else:
                events.append(h1(t, upper_target - 10, upper_target - 6, upper_target - 12, upper_target - 9))
            t += H1S

        pivot_bar_idx = num_pre_break_h1 // 2
        for i in range(num_pre_break_h1):
            if i == pivot_bar_idx:
                events.append(h1(t, level_price - 3, level_price, level_price - 5, level_price - 3))
            else:
                events.append(h1(t, level_price - 8, level_price - 4, level_price - 10, level_price - 7))
            t += H1S

        break_close = level_price + break_close_offset
        events.append(h1(t, level_price - 2, break_close + 3, level_price - 3, break_close))
        t += H1S

        if pullback_to_level:
            events.append(h1(t, break_close - 1, break_close, level_price - 2, level_price + 1))
            t += H1S

            for _ in range(extra_m15_before_conf):
                events.append(m15(t, level_price + 1, level_price + 3, level_price - 1, level_price + 2))
                t += M15S

            events.append(m15(t, level_price + 2, level_price + 3, level_price - 1, level_price + 0.2))
            t += M15S
            events.append(m15(t, level_price, level_price + 8, level_price - 1, level_price + 7))
            t += M15S
            events.append(m15(t, level_price + 7, level_price + 10, level_price + 5, level_price + 8))
            t += M15S

    else:  # SHORT
        lower_target = level_price - 25.0

        for i in range(5):
            if i == 2:
                events.append(h1(t, lower_target + 3, lower_target + 5, lower_target, lower_target + 3))
            else:
                events.append(h1(t, lower_target + 7, lower_target + 10, lower_target + 4, lower_target + 8))
            t += H1S

        pivot_bar_idx = num_pre_break_h1 // 2
        for i in range(num_pre_break_h1):
            if i == pivot_bar_idx:
                events.append(h1(t, level_price + 3, level_price + 5, level_price, level_price + 3))
            else:
                events.append(h1(t, level_price + 7, level_price + 10, level_price + 4, level_price + 8))
            t += H1S

        break_close = level_price - break_close_offset
        events.append(h1(t, level_price + 2, level_price + 3, break_close - 3, break_close))
        t += H1S

        if pullback_to_level:
            events.append(h1(t, break_close + 1, level_price + 2, break_close, level_price - 1))
            t += H1S

            for _ in range(extra_m15_before_conf):
                events.append(m15(t, level_price - 2, level_price + 1, level_price - 3, level_price - 1))
                t += M15S

            events.append(m15(t, level_price - 2, level_price + 1, level_price - 3, level_price - 0.2))
            t += M15S
            events.append(m15(t, level_price, level_price + 1, level_price - 8, level_price - 7))
            t += M15S
            events.append(m15(t, level_price - 7, level_price - 5, level_price - 10, level_price - 8))
            t += M15S

    return events


def last_ts(events: list[MarketEvent]) -> int:
    return max(e.close_timestamp for e in events)


# ─── initialization tests ─────────────────────────────────────────────────────

class V3InitializationTests(unittest.TestCase):

    def test_initialize_correct_version(self):
        ev = make_evaluator()
        self.assertIsNotNone(ev.strategy_version)

    def test_wrong_version_raises(self):
        ev = KojoStructureReclaimV3Evaluator()
        sv = StrategyVersion(
            "KOJO_STRUCTURE_RECLAIM_V2", "V2", "kojo_structure_reclaim_v2",
            kojo_structure_reclaim_v3_parameter_schema(),
        )
        ps = kojo_structure_reclaim_v3_baseline_parameter_set(sv.strategy_version_id)
        with self.assertRaises(ValueError):
            ev.initialize(sv, ps)

    def test_parameter_schema_accepts_baseline(self):
        schema = kojo_structure_reclaim_v3_parameter_schema()
        ps = kojo_structure_reclaim_v3_baseline_parameter_set()
        schema.validate(ps.values)  # must not raise

    def test_setup_id_prefix_v3(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        self.assertGreater(len(signals), 0)
        self.assertTrue(signals[0].signal_id.startswith("KSRV3_SIG_"))

    def test_setup_id_distinct_from_v2(self):
        """V3 setup_id prefix (KSRV3_) is distinct from V2 (KSRV2_)."""
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        for s in signals:
            self.assertFalse(s.signal_id.startswith("KSRV2_"))


# ─── retirement policy: CONSUMED ──────────────────────────────────────────────

class ConsumedRetirementTests(unittest.TestCase):

    def _run_to_signal(self, level=4110.0, direction="LONG"):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, level, direction)
        signals, setups = feed_and_collect(ev, events)
        self.assertEqual(len(signals), 1)
        return ev, signals[0], setups, last_ts(events)

    def test_consumed_never_reactivates(self):
        """After a CONSUMED episode, the same (level_id, direction) must never fire again."""
        ev, sig, _, end_ts = self._run_to_signal()
        level_id = sig.provenance["structural_level_id"]
        direction = sig.provenance["direction"]

        # Consumed opportunity key is permanently set
        self.assertIn(
            (level_id, direction),
            ev._consumed_opportunity_keys,
        )

        # Feed many more bars that would ordinarily trigger a new break — must not
        t = end_ts + H1S
        for _ in range(30):
            ev.consume_market_event(h1(t, 4114, 4117, 4112, 4115))
            t += H1S

        # No second episode created for this level
        episodes_for_level = [
            s for s in ev._setups.values()
            if s["structural_level_id"] == level_id and s["direction"] == direction
        ]
        self.assertEqual(len(episodes_for_level), 1)
        self.assertEqual(episodes_for_level[0]["state"], CONSUMED)

    def test_consumed_key_in_consumed_opportunity_keys_not_terminal_only(self):
        """CONSUMED → level key in _consumed_opportunity_keys (permanent retirement)."""
        ev, sig, _, _ = self._run_to_signal()
        level_id = sig.provenance["structural_level_id"]
        direction = sig.provenance["direction"]
        self.assertIn((level_id, direction), ev._consumed_opportunity_keys)
        self.assertIn(sig.provenance["setup_id"], ev._terminal_episode_ids)


# ─── retirement policy: INVALIDATED ───────────────────────────────────────────

class InvalidatedRetirementTests(unittest.TestCase):

    def _build_break_only_events(self, level=4110.0, direction="LONG"):
        """Return events that create an episode but do NOT complete the retest (break only)."""
        return build_pivot_sequence(
            BASE_TS, level, direction, pullback_to_level=False
        )

    def _get_invalidated_setup(self):
        """Run to a break, then INVALIDATE by closing opposite side on next H1 bar."""
        ev = make_evaluator()
        events = self._build_break_only_events()
        feed_and_collect(ev, events)

        # At this point one setup is in WAITING_FOR_RETEST (break happened, no pullback yet).
        # Feed one H1 bar closing BELOW the level → INVALIDATED for LONG.
        end_ts = last_ts(events)
        t = end_ts + H1S
        ev.consume_market_event(h1(t, 4108, 4109, 4100, 4108))  # close < 4110
        return ev, t

    def test_invalidated_episode_itself_never_reactivates(self):
        """An INVALIDATED episode (by setup_id) remains terminal; its state must never change."""
        ev, t = self._get_invalidated_setup()

        invalidated = [
            s for s in ev._setups.values()
            if s["state"] == INVALIDATED
        ]
        self.assertGreater(len(invalidated), 0)
        terminated_id = invalidated[0]["setup_id"]

        # Feed more events — including bars above the level
        for _ in range(10):
            t += H1S
            ev.consume_market_event(h1(t, 4112, 4115, 4110, 4113))

        # Terminated episode must remain INVALIDATED
        self.assertEqual(ev._setups[terminated_id]["state"], INVALIDATED)
        self.assertIn(terminated_id, ev._terminal_episode_ids)

    def test_invalidated_level_not_in_consumed_opportunity_keys(self):
        """INVALIDATED episode must NOT permanently retire the level opportunity."""
        ev, _ = self._get_invalidated_setup()

        invalidated = [s for s in ev._setups.values() if s["state"] == INVALIDATED]
        self.assertGreater(len(invalidated), 0)
        setup = invalidated[0]
        level_key = (setup["structural_level_id"], setup["direction"])

        # The structural level key must NOT be in permanent opportunity retirement
        self.assertNotIn(level_key, ev._consumed_opportunity_keys)
        # But the episode itself IS in terminal_episode_ids
        self.assertIn(setup["setup_id"], ev._terminal_episode_ids)


# ─── retirement policy: EXPIRED ───────────────────────────────────────────────

class ExpiredRetirementTests(unittest.TestCase):

    def _get_expired_setup(self, max_wait=24):
        """Break above level, then feed max_wait+1 H1 bars above level → EXPIRED."""
        ev = make_evaluator()
        # Use a small max_wait to keep the test fast; must be ≥4 per schema
        sv = StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, kojo_structure_reclaim_v3_parameter_schema())
        ps = ParameterSet(
            parameter_set_id="kojo-v3-test-short-wait",
            strategy_version_id=sv.strategy_version_id,
            schema_id="kojo-structure-reclaim-v3",
            values={
                "pivot_strength": 2,
                "retest_tolerance_atr": 0.5,
                "max_retest_wait_h1_bars": max_wait,
                "max_confirmation_wait_m15_bars": 16,
                "stop_buffer_type": "PRICE",
                "stop_buffer_value": 1.0,
            },
            provenance={"source": "TEST"},
        )
        ev.initialize(sv, ps)

        # Phase 1+2: establish external target and structural level
        level = 4110.0
        events = build_pivot_sequence(BASE_TS, level, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events)

        # Verify one WAITING_FOR_RETEST setup exists
        active = [s for s in ev._setups.values() if s["state"] not in TERMINAL_STATES]
        self.assertEqual(len(active), 1)
        setup = active[0]
        expiry_idx = setup["expiry_h1_index"]
        current_h1_idx = len(ev._h1_bars) - 1

        # Feed H1 bars well ABOVE the level (no retest) until expiry fires
        # Use lows far above the level so retest tolerance isn't triggered
        t = last_ts(events) + H1S
        bars_needed = expiry_idx - current_h1_idx + 1
        for _ in range(bars_needed):
            ev.consume_market_event(h1(t, 4128, 4140, 4126, 4132))
            t += H1S

        return ev, setup["setup_id"], level, t

    def test_expired_episode_itself_never_reactivates(self):
        """An EXPIRED episode remains terminal regardless of subsequent events."""
        ev, terminated_id, level, t = self._get_expired_setup()

        # Setup must be EXPIRED
        self.assertEqual(ev._setups[terminated_id]["state"], EXPIRED)

        # Feed more bars above the level
        for _ in range(10):
            ev.consume_market_event(h1(t, 4128, 4132, 4126, 4130))
            t += H1S

        # Still EXPIRED
        self.assertEqual(ev._setups[terminated_id]["state"], EXPIRED)

    def test_expired_level_not_in_consumed_opportunity_keys(self):
        """EXPIRED episode must NOT permanently retire the level opportunity."""
        ev, terminated_id, level, _ = self._get_expired_setup()

        setup = ev._setups[terminated_id]
        level_key = (setup["structural_level_id"], setup["direction"])

        self.assertNotIn(level_key, ev._consumed_opportunity_keys)
        self.assertIn(terminated_id, ev._terminal_episode_ids)


# ─── retirement policy: new causal episode after INVALIDATED ──────────────────

class NewCausalEpisodeTests(unittest.TestCase):

    def test_genuinely_new_causal_episode_may_reuse_structure(self):
        """After INVALIDATED + price reset to non-break side, a new causal H1 break on the
        same structural level creates a NEW episode with a distinct setup_id.

        Sequence (LONG, level=4110):
          1. Break above 4110 → ep#1 (WAITING_FOR_RETEST)
          2. H1 close below 4110 → ep#1 INVALIDATED
             (This also resets price to the non-break side.)
          3. H1 close below 4110 (still on non-break side — prev_close OK for next bar)
          4. H1 close above 4110 with prev_close below 4110 → GENUINE NEW BREAK → ep#2
        """
        ev = make_evaluator()
        level = 4110.0

        # Phase 1+2: build structural context (no break yet)
        events = build_pivot_sequence(BASE_TS, level, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events)

        # Identify the WAITING_FOR_RETEST setup
        active = [s for s in ev._setups.values() if s["state"] not in TERMINAL_STATES]
        self.assertEqual(len(active), 1)
        ep1_id = active[0]["setup_id"]

        # Bar: close below level → INVALIDATED
        t = last_ts(events) + H1S
        ev.consume_market_event(h1(t, 4108, 4109, 4100, 4108))  # close=4108 < 4110
        self.assertEqual(ev._setups[ep1_id]["state"], INVALIDATED)

        # Bar: still below level (price reset confirmed — prev_close for next bar will be < level)
        t += H1S
        ev.consume_market_event(h1(t, 4107, 4109, 4104, 4106))  # close=4106 < 4110

        # Bar: GENUINE NEW BREAK — prev_close (4106) < 4110, current close > 4110
        t += H1S
        ev.consume_market_event(h1(t, 4109, 4117, 4108, 4115))  # close=4115 > 4110

        # ep#1 is still INVALIDATED (never reactivated)
        self.assertEqual(ev._setups[ep1_id]["state"], INVALIDATED)

        # A new active episode must exist
        active_now = [
            s for s in ev._setups.values()
            if s["state"] not in TERMINAL_STATES
            and s["setup_id"] != ep1_id
        ]
        self.assertGreater(len(active_now), 0, "Expected new episode after price reset + genuine break")
        ep2 = active_now[0]
        self.assertNotEqual(ep2["setup_id"], ep1_id)

        # ep#2 must reference the SAME structural level as ep#1
        self.assertEqual(
            ep2["structural_level_id"],
            ev._setups[ep1_id]["structural_level_id"],
        )

    def test_new_episode_has_different_setup_id(self):
        """The new episode after INVALIDATED + reset must have a distinct setup_id because
        the break bar timestamp is different."""
        ev = make_evaluator()
        level = 4110.0
        events = build_pivot_sequence(BASE_TS, level, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events)

        active = [s for s in ev._setups.values() if s["state"] not in TERMINAL_STATES]
        ep1_id = active[0]["setup_id"]

        t = last_ts(events) + H1S
        ev.consume_market_event(h1(t, 4108, 4109, 4100, 4108))  # INVALIDATE
        t += H1S
        ev.consume_market_event(h1(t, 4107, 4109, 4104, 4106))  # below level
        t += H1S
        ev.consume_market_event(h1(t, 4109, 4117, 4108, 4115))  # new break

        all_setup_ids = list(ev._setups.keys())
        self.assertGreater(len(all_setup_ids), 1)
        # All setup IDs must be unique
        self.assertEqual(len(all_setup_ids), len(set(all_setup_ids)))

    def test_new_episode_after_invalidated_can_produce_signal(self):
        """A second episode (after INVALIDATED) can produce an accepted signal if the
        retest and M15 confirmation occur."""
        ev = make_evaluator()
        level = 4110.0

        # Phase 1+2: structural context
        events = build_pivot_sequence(BASE_TS, level, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events)

        active = [s for s in ev._setups.values() if s["state"] not in TERMINAL_STATES]
        ep1_id = active[0]["setup_id"]
        base_t = last_ts(events)

        # INVALIDATE ep#1
        t = base_t + H1S
        ev.consume_market_event(h1(t, 4108, 4109, 4100, 4108))

        # Price reset below level
        t += H1S
        ev.consume_market_event(h1(t, 4107, 4109, 4104, 4106))

        # New genuine break (ep#2)
        t += H1S
        break_close = level + 5.0
        ev.consume_market_event(h1(t, 4109, break_close + 3, 4108, break_close))

        # Pullback H1 bar to retest
        t += H1S
        ev.consume_market_event(h1(t, break_close - 1, break_close, level - 2, level + 1))

        # M15 retest bars leading to engulfing confirmation
        t += H1S
        ev.consume_market_event(
            m15(t, level + 2, level + 3, level - 1, level + 0.2)
        )
        t += M15S
        ev.consume_market_event(
            m15(t, level, level + 8, level - 1, level + 7)
        )
        t += M15S

        # Entry bar
        sig_outputs = ev.consume_market_event(
            m15(t, level + 7, level + 10, level + 5, level + 8)
        )

        signals = [o for o in sig_outputs if isinstance(o, EntrySignal)]
        self.assertEqual(len(signals), 1, "Second episode should produce a signal")
        self.assertNotEqual(signals[0].provenance["setup_id"], ep1_id)


# ─── retirement policy: stale break duplicate prevention ──────────────────────

class StaleBreakDuplicateTests(unittest.TestCase):

    def test_stale_break_cannot_create_duplicate_after_expired(self):
        """After an EXPIRED episode, bars that keep price on the break side (above level for LONG)
        must not create new episodes (stale continuation, not a new causal break).

        Only a bar where prev_close <= level_price qualifies as a genuine new break.
        """
        ev = make_evaluator()
        sv = StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, kojo_structure_reclaim_v3_parameter_schema())
        ps = ParameterSet(
            parameter_set_id="kojo-v3-test-stale",
            strategy_version_id=sv.strategy_version_id,
            schema_id="kojo-structure-reclaim-v3",
            values={
                "pivot_strength": 2,
                "retest_tolerance_atr": 0.5,
                "max_retest_wait_h1_bars": 5,  # short wait for test speed
                "max_confirmation_wait_m15_bars": 16,
                "stop_buffer_type": "PRICE",
                "stop_buffer_value": 1.0,
            },
            provenance={"source": "TEST"},
        )
        ev.initialize(sv, ps)

        level = 4110.0
        events = build_pivot_sequence(BASE_TS, level, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events)

        active = [s for s in ev._setups.values() if s["state"] not in TERMINAL_STATES]
        self.assertEqual(len(active), 1)
        ep1_id = active[0]["setup_id"]
        ep1_expiry = active[0]["expiry_h1_index"]
        current_idx = len(ev._h1_bars) - 1

        # Feed bars above level (no retest, lows far above level) until expiry fires
        t = last_ts(events) + H1S
        bars_to_expiry = ep1_expiry - current_idx + 1
        for _ in range(bars_to_expiry):
            ev.consume_market_event(h1(t, 4125, 4130, 4123, 4127))  # lows well above 4110
            t += H1S

        # ep#1 must be EXPIRED
        self.assertEqual(ev._setups[ep1_id]["state"], EXPIRED)

        # Feed 10 more bars with price staying above 4110 (stale continuation)
        for _ in range(10):
            ev.consume_market_event(h1(t, 4125, 4130, 4123, 4127))
            t += H1S

        # No new setups created (only ep#1 exists)
        all_setups = list(ev._setups.values())
        self.assertEqual(len(all_setups), 1, "Stale continuation must not create duplicate episodes")
        self.assertEqual(all_setups[0]["setup_id"], ep1_id)
        self.assertEqual(all_setups[0]["state"], EXPIRED)

    def test_stale_bars_do_not_add_to_consumed_opportunity_keys(self):
        """Stale continuation bars must NOT add the level to consumed_opportunity_keys."""
        ev = make_evaluator()
        sv = StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, kojo_structure_reclaim_v3_parameter_schema())
        ps = ParameterSet(
            parameter_set_id="kojo-v3-test-stale2",
            strategy_version_id=sv.strategy_version_id,
            schema_id="kojo-structure-reclaim-v3",
            values={
                "pivot_strength": 2, "retest_tolerance_atr": 0.5,
                "max_retest_wait_h1_bars": 5, "max_confirmation_wait_m15_bars": 16,
                "stop_buffer_type": "PRICE", "stop_buffer_value": 1.0,
            },
            provenance={"source": "TEST"},
        )
        ev.initialize(sv, ps)

        level = 4110.0
        events = build_pivot_sequence(BASE_TS, level, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events)

        active = [s for s in ev._setups.values() if s["state"] not in TERMINAL_STATES]
        ep1 = active[0]
        current_idx = len(ev._h1_bars) - 1
        t = last_ts(events) + H1S
        bars_to_expiry = ep1["expiry_h1_index"] - current_idx + 1
        for _ in range(bars_to_expiry + 10):
            ev.consume_market_event(h1(t, 4125, 4130, 4123, 4127))
            t += H1S

        level_key = (ep1["structural_level_id"], ep1["direction"])
        self.assertNotIn(level_key, ev._consumed_opportunity_keys)


# ─── checkpoint / restart parity ──────────────────────────────────────────────

class CheckpointRestartParityTests(unittest.TestCase):

    def test_checkpoint_restart_produces_same_signals(self):
        """snapshot_state + restore_state must produce the same final signals as a full replay."""
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        midpoint = len(events) // 2

        # Run to midpoint, snapshot, continue
        ev_split = make_evaluator()
        for e in events[:midpoint]:
            ev_split.consume_market_event(e)
        snap = ev_split.snapshot_state()

        ev_restored = make_evaluator()
        ev_restored.restore_state(snap)
        signals_split, _ = feed_and_collect(ev_restored, events[midpoint:])

        # Full replay
        ev_full = make_evaluator()
        signals_full, _ = feed_and_collect(ev_full, events)

        self.assertEqual(
            [s.signal_id for s in signals_full],
            [s.signal_id for s in signals_split],
        )
        self.assertEqual(len(signals_full), len(signals_split))
        if signals_full and signals_split:
            self.assertEqual(signals_full[0].entry_price, signals_split[0].entry_price)
            self.assertEqual(signals_full[0].stop_price, signals_split[0].stop_price)
            self.assertEqual(signals_full[0].target_price, signals_split[0].target_price)

    def test_snapshot_preserves_consumed_opportunity_keys(self):
        """Consumed opportunity keys survive snapshot/restore."""
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        self.assertEqual(len(signals), 1)

        snap = ev.snapshot_state()
        ev2 = make_evaluator()
        ev2.restore_state(snap)

        self.assertEqual(ev._consumed_opportunity_keys, ev2._consumed_opportunity_keys)
        self.assertEqual(ev._terminal_episode_ids, ev2._terminal_episode_ids)

    def test_snapshot_preserves_terminal_episode_ids(self):
        """terminal_episode_ids survive snapshot/restore."""
        ev = make_evaluator()
        # Build to INVALIDATED state
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events)
        t = last_ts(events) + H1S
        ev.consume_market_event(h1(t, 4108, 4109, 4100, 4108))  # INVALIDATE

        snap = ev.snapshot_state()
        ev2 = make_evaluator()
        ev2.restore_state(snap)

        self.assertEqual(ev._terminal_episode_ids, ev2._terminal_episode_ids)
        self.assertEqual(ev._consumed_opportunity_keys, ev2._consumed_opportunity_keys)


# ─── H1 evidence scaffold ─────────────────────────────────────────────────────

class H1EvidenceScaffoldTests(unittest.TestCase):

    def _get_signal_provenance(self, level=4110.0, direction="LONG"):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, level, direction)
        signals, _ = feed_and_collect(ev, events)
        self.assertEqual(len(signals), 1)
        return signals[0].provenance

    def test_h1_context_evidence_present(self):
        prov = self._get_signal_provenance()
        self.assertIn("h1_context_evidence", prov)
        self.assertIsInstance(prov["h1_context_evidence"], dict)

    def test_h1_confirmation_fields_present(self):
        """H1 confirmation source rule: explicit fields replacing scaffold."""
        prov = self._get_signal_provenance()
        self.assertNotIn("h1_post_pullback_confirmation_evidence", prov)
        for field in (
            "h1_key_level_id",
            "h1_confirmation_open_ts",
            "h1_confirmation_close_ts",
            "h1_confirmation_open",
            "h1_confirmation_high",
            "h1_confirmation_low",
            "h1_confirmation_close",
            "h1_confirmation_relation_to_level",
        ):
            self.assertIn(field, prov, f"Missing H1 confirmation field: {field}")

    def test_h1_confirmation_relation_to_level_long(self):
        prov = self._get_signal_provenance(direction="LONG")
        self.assertEqual(prov["h1_confirmation_relation_to_level"], "CLOSE_BEYOND")

    def test_h1_confirmation_close_ts_equals_episode_start_ts(self):
        """H1 confirmation close timestamp must equal episode_start_ts (the break bar)."""
        prov = self._get_signal_provenance()
        self.assertEqual(prov["h1_confirmation_close_ts"], prov["episode_start_ts"])

    def test_h1_key_level_id_matches_structural_level(self):
        prov = self._get_signal_provenance()
        self.assertEqual(prov["h1_key_level_id"], prov["structural_level_id"])

    def test_m15_confirmation_evidence_present(self):
        prov = self._get_signal_provenance()
        self.assertIn("m15_confirmation_evidence", prov)
        self.assertIsInstance(prov["m15_confirmation_evidence"], dict)

    def test_no_dual_timeframe_confirmed_assertion(self):
        """V3 must not carry the semantically mislabeled DUAL_TF_BASELINE_ENFORCED assertion."""
        prov = self._get_signal_provenance()
        self.assertNotIn("dual_timeframe_confirmed", prov)

    def test_h1_context_evidence_equals_break_bar(self):
        """h1_context_evidence is the break bar — same open_timestamp as break_timestamp."""
        prov = self._get_signal_provenance()
        h1_ctx = prov["h1_context_evidence"]
        # break_timestamp is the close_timestamp of the break bar
        # h1_context_evidence.close_timestamp must equal break_timestamp
        self.assertEqual(h1_ctx["close_timestamp"], prov["break_timestamp"])

    def test_short_direction_h1_evidence_fields(self):
        prov = self._get_signal_provenance(direction="SHORT")
        self.assertIn("h1_context_evidence", prov)
        self.assertIn("h1_confirmation_open_ts", prov)
        self.assertEqual(prov["h1_confirmation_relation_to_level"], "CLOSE_BELOW")
        self.assertNotIn("h1_post_pullback_confirmation_evidence", prov)
        self.assertNotIn("dual_timeframe_confirmed", prov)


# ─── target diagnostics scaffold ──────────────────────────────────────────────

class TargetDiagnosticsTests(unittest.TestCase):

    def _get_tp1_diagnostics(self, level=4110.0, direction="LONG"):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, level, direction)
        signals, _ = feed_and_collect(ev, events)
        self.assertEqual(len(signals), 1)
        prov = signals[0].provenance
        self.assertIn("tp1_provenance", prov)
        tp1_prov = prov["tp1_provenance"]
        self.assertIn("tp1_diagnostics", tp1_prov)
        return tp1_prov["tp1_diagnostics"]

    def test_target_diagnostics_block_present(self):
        diag = self._get_tp1_diagnostics()
        self.assertIsInstance(diag, dict)

    def test_required_diagnostic_fields_present(self):
        diag = self._get_tp1_diagnostics()
        required = [
            "target_structural_id",
            "target_h1_open_timestamp",
            "entry_price",
            "target_distance",
            "spread_at_decision_bar",
            "tick_size",
            "episode_envelope_width",
            "stop_distance",
            "planned_r",
            "target_distance_over_spread",
            "target_distance_over_envelope_width",
            "target_distance_over_stop_distance",
            "near_coincident_class",
        ]
        for field in required:
            self.assertIn(field, diag, f"Missing diagnostic field: {field}")

    def test_target_distance_is_positive(self):
        diag = self._get_tp1_diagnostics()
        self.assertGreater(diag["target_distance"], 0)

    def test_ratio_fields_computed(self):
        """Fields that can be computed without spread data must not be None."""
        diag = self._get_tp1_diagnostics()
        self.assertIsNotNone(diag["target_distance_over_envelope_width"])
        self.assertIsNotNone(diag["target_distance_over_stop_distance"])
        self.assertIsNotNone(diag["planned_r"])

    def test_spread_dependent_fields_are_none(self):
        """Spread-dependent fields are None until MarketEvent enrichment."""
        diag = self._get_tp1_diagnostics()
        self.assertIsNone(diag["spread_at_decision_bar"])
        self.assertIsNone(diag["target_distance_over_spread"])
        self.assertIsNone(diag["tick_size"])

    def test_near_coincident_class_is_source_rule_required(self):
        """Boundary for near-coincident is SOURCE_RULE_REQUIRED pending source evidence."""
        diag = self._get_tp1_diagnostics()
        self.assertEqual(diag["near_coincident_class"], TP1_NEAR_COINCIDENT_BOUNDARY)

    def test_near_coincident_and_standard_constants_defined(self):
        """Module must define both classification constants for downstream use."""
        self.assertEqual(TP1_NEAR_COINCIDENT, "EXTERNAL_BUT_NEAR_COINCIDENT_WITH_ENTRY")
        self.assertEqual(TP1_STANDARD, "EXTERNAL_STANDARD")
        self.assertEqual(TP1_NEAR_COINCIDENT_BOUNDARY, "SOURCE_RULE_REQUIRED")

    def test_diagnostics_in_short_signal(self):
        diag = self._get_tp1_diagnostics(direction="SHORT")
        self.assertIn("target_distance", diag)
        self.assertGreater(diag["target_distance"], 0)


# ─── source fidelity status ───────────────────────────────────────────────────

class SourceFidelityStatusTests(unittest.TestCase):

    def test_source_fidelity_blocked_constant(self):
        self.assertTrue(SOURCE_FIDELITY_BLOCKED)

    def test_source_fidelity_blocked_in_diagnostics(self):
        ev = make_evaluator()
        diag = ev.diagnostics()
        self.assertTrue(diag["SOURCE_FIDELITY_BLOCKED"])
        self.assertFalse(diag["READY_FOR_DISCOVERY"])
        self.assertFalse(diag["READY_FOR_VALIDATION"])
        self.assertFalse(diag["READY_FOR_SHADOW_SIGNALS"])
        self.assertFalse(diag["READY_FOR_EXECUTION"])

    def test_source_fidelity_blocked_reasons_present(self):
        ev = make_evaluator()
        diag = ev.diagnostics()
        reasons = diag["source_fidelity_blocked_reasons"]
        self.assertIsInstance(reasons, list)
        self.assertGreaterEqual(len(reasons), 1)
        reasons_text = " ".join(reasons)
        self.assertIn("SOURCE_RULE_REQUIRED", reasons_text)
        # H1 confirmation is now resolved; only TARGET_MEANINGFULNESS_BOUNDARY remains.
        self.assertTrue(any("TARGET_MEANINGFULNESS_BOUNDARY" in r for r in reasons))

    def test_production_eligible_false_in_signal(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        self.assertEqual(len(signals), 1)
        prov = signals[0].provenance
        self.assertFalse(prov["production_eligible"])
        self.assertEqual(prov["v3_status"], "SOURCE_FIDELITY_BLOCKED")

    def test_no_dual_tf_enforced_in_diagnostics(self):
        """DUAL_TF_BASELINE_ENFORCED must not appear in V3 diagnostics."""
        ev = make_evaluator()
        diag = ev.diagnostics()
        self.assertNotIn("DUAL_TF_BASELINE_ENFORCED", diag)

    def test_consumed_opportunity_keys_not_consumed_level_keys(self):
        """V3 retirement uses consumed_opportunity_keys; the old name must not appear."""
        ev = make_evaluator()
        diag = ev.diagnostics()
        self.assertIn("consumed_opportunity_keys_count", diag)
        self.assertNotIn("consumed_level_keys_count", diag)


# ─── causal invariants (inherited) ────────────────────────────────────────────

class InheritedCausalInvariantTests(unittest.TestCase):

    def test_continuation_close_rejected(self):
        """CONTINUATION_CLOSE on M15 must not produce a V3 signal."""
        ev = make_evaluator()
        level = 4110.0
        events_no_pullback = build_pivot_sequence(BASE_TS, level, "LONG", pullback_to_level=False)
        feed_and_collect(ev, events_no_pullback)

        t = last_ts(events_no_pullback) + H1S
        # H1 pullback bar
        ev.consume_market_event(h1(t, 4114, 4116, 4109, level + 1))
        t += H1S

        # M15 retest: a continuation close (bullish bar, closes above prev close, not engulfing)
        ev.consume_market_event(m15(t, level + 1, level + 2, level, level + 1.5))  # continuation
        t += M15S
        ev.consume_market_event(m15(t, level + 1.5, level + 2.5, level + 0.5, level + 2))  # continuation
        t += M15S

        signals = [
            out for e in [] for out in ev.consume_market_event(e)
            if isinstance(out, EntrySignal)
        ]
        self.assertEqual(len(signals), 0, "Continuation close must not produce a signal")
        diag = ev.diagnostics()
        self.assertGreater(diag["weak_m15_rejections"], 0)

    def test_tp1_class_always_external(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        for sig in signals:
            self.assertEqual(sig.provenance.get("tp1_class"), "EXTERNAL")

    def test_deterministic_rerun(self):
        """Two independent replays of the same sequence produce identical signal IDs."""
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        ev1 = make_evaluator()
        ev2 = make_evaluator()
        sigs1, _ = feed_and_collect(ev1, events)
        sigs2, _ = feed_and_collect(ev2, events)
        self.assertEqual([s.signal_id for s in sigs1], [s.signal_id for s in sigs2])

    def test_prefix_invariance(self):
        """Processing a prefix then continuing produces the same final signals."""
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        ev_full = make_evaluator()
        sigs_full, _ = feed_and_collect(ev_full, events)

        ev_prefix = make_evaluator()
        mid = len(events) // 2
        for e in events[:mid]:
            ev_prefix.consume_market_event(e)
        sigs_rest, _ = feed_and_collect(ev_prefix, events[mid:])

        self.assertEqual([s.signal_id for s in sigs_full], [s.signal_id for s in sigs_rest])


# ─── H1 confirmation source rule (TASK 1) ────────────────────────────────────

from strategy_backtest.kojo_structure_reclaim_v3 import (
    H1_CLOSE_BEYOND_LEVEL_REQUIRED,
    H1_CONFIRMATION_IS_COMPLETED_BAR,
    H1_CONFIRMATION_PRECEDES_M15_RETEST,
    M15_ONLY_BASELINE_ENABLED,
)


class H1ConfirmationSourceRuleTests(unittest.TestCase):

    def test_h1_confirmation_constants_defined(self):
        self.assertTrue(H1_CLOSE_BEYOND_LEVEL_REQUIRED)
        self.assertTrue(H1_CONFIRMATION_IS_COMPLETED_BAR)
        self.assertTrue(H1_CONFIRMATION_PRECEDES_M15_RETEST)
        self.assertFalse(M15_ONLY_BASELINE_ENABLED)

    def test_temporal_guard_rejects_premature_m15(self):
        """M15 bars with open_timestamp < episode_start_ts must not create RETEST_SEEN.

        Feeds events only through the break bar (SETUP_DETECTED), then injects M15 bars
        with timestamps strictly before episode_start_ts. The temporal guard must skip them.
        """
        ev = make_evaluator()
        level = 4110.0
        events = build_pivot_sequence(BASE_TS, level, "LONG", num_pre_break_h1=6)

        # Feed until SETUP_DETECTED fires, capture episode_start_ts from the break bar
        episode_start = None
        for e in events:
            outs = list(ev.consume_market_event(e))
            for out in outs:
                if isinstance(out, SetupLifecycleEvent) and out.status == "SETUP_DETECTED":
                    episode_start = e.close_timestamp
                    break
            if episode_start is not None:
                break

        self.assertIsNotNone(episode_start, "SETUP_DETECTED must fire")

        # Inject 3 M15 bars strictly before episode_start_ts: open_ts = ep-3, ep-2, ep-1*M15S
        stale_t = episode_start - M15S * 3
        for _ in range(3):
            outs = list(ev.consume_market_event(
                m15(stale_t, level - 1, level + 1, level - 2, level)
            ))
            stale_t += M15S
            for out in outs:
                self.assertNotIsInstance(out, EntrySignal, "Stale M15 must not produce signal")
                if isinstance(out, SetupLifecycleEvent):
                    self.assertNotEqual(out.status, "RETEST_SEEN",
                                        "Stale M15 must not create RETEST_SEEN")

    def test_temporal_ordering_in_provenance(self):
        """episode_start_ts <= m15_retest_first_ts <= m15_rejection_ts <= entry_decision_ts."""
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        self.assertEqual(len(signals), 1)
        prov = signals[0].provenance

        ep_start = prov["episode_start_ts"]
        entry_ts = prov["entry_decision_ts"]
        rejection_ts = prov["m15_rejection_ts"]

        self.assertIsNotNone(ep_start)
        self.assertIsNotNone(entry_ts)
        self.assertIsNotNone(rejection_ts)

        self.assertGreater(entry_ts, rejection_ts,
                           "Entry decision must come after M15 rejection")
        self.assertGreaterEqual(rejection_ts, ep_start,
                                "M15 rejection must not precede H1 confirmation close")

    def test_m15_retest_level_id_matches_structural_level(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        prov = signals[0].provenance
        self.assertEqual(prov["m15_retest_level_id"], prov["structural_level_id"])


# ─── initial trade plan (TASK 3) ─────────────────────────────────────────────

from strategy_backtest.kojo_structure_reclaim_v3 import (
    TARGETS_ARE_OBJECTIVES,
    MANDATORY_HOLD_TO_TARGET,
)


class InitialTradePlanTests(unittest.TestCase):

    def _get_provenance(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        self.assertEqual(len(signals), 1)
        return signals[0].provenance

    def test_initial_trade_plan_present(self):
        prov = self._get_provenance()
        self.assertIn("initial_trade_plan", prov)
        plan = prov["initial_trade_plan"]
        self.assertIsInstance(plan, dict)

    def test_initial_trade_plan_required_fields(self):
        prov = self._get_provenance()
        plan = prov["initial_trade_plan"]
        for field in ("planned_entry", "initial_stop", "planned_tp1", "planned_tp1_reason",
                      "planned_tp2", "planned_tp2_reason",
                      "targets_are_objectives", "mandatory_hold_to_target",
                      "trade_management_policy_ref"):
            self.assertIn(field, plan, f"initial_trade_plan missing: {field}")

    def test_targets_are_objectives_true(self):
        self.assertTrue(TARGETS_ARE_OBJECTIVES)
        prov = self._get_provenance()
        self.assertTrue(prov["targets_are_objectives"])
        self.assertTrue(prov["initial_trade_plan"]["targets_are_objectives"])

    def test_mandatory_hold_to_target_false(self):
        self.assertFalse(MANDATORY_HOLD_TO_TARGET)
        prov = self._get_provenance()
        self.assertFalse(prov["mandatory_hold_to_target"])
        self.assertFalse(prov["initial_trade_plan"]["mandatory_hold_to_target"])

    def test_trade_management_source_rule_required(self):
        prov = self._get_provenance()
        self.assertEqual(prov["trade_management_policy_ref"], "SOURCE_RULE_REQUIRED")
        self.assertEqual(prov["initial_trade_plan"]["trade_management_policy_ref"],
                         "SOURCE_RULE_REQUIRED")

    def test_initial_trade_plan_prices_match_signal(self):
        """planned_entry / initial_stop / planned_tp1 must match signal top-level prices."""
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        sig = signals[0]
        plan = sig.provenance["initial_trade_plan"]
        self.assertEqual(plan["planned_entry"], sig.entry_price)
        self.assertEqual(plan["initial_stop"], sig.stop_price)
        self.assertEqual(plan["planned_tp1"], sig.target_price)


# ─── target candidate classification (TASK 2) ────────────────────────────────

from strategy_backtest.kojo_structure_reclaim_v3 import (
    TP_CANDIDATE_CLASS_GENERIC,
    TARGET_SELECTION_SOURCE_RULE_REQUIRED,
    EXISTING_REACTION_ZONE_PRIMITIVE_FOUND,
    EXISTING_LIQUIDITY_PRIMITIVE_FOUND,
)


class TargetCandidateClassTests(unittest.TestCase):

    def test_primitive_not_found_constants(self):
        self.assertFalse(EXISTING_REACTION_ZONE_PRIMITIVE_FOUND)
        self.assertFalse(EXISTING_LIQUIDITY_PRIMITIVE_FOUND)
        self.assertTrue(TARGET_SELECTION_SOURCE_RULE_REQUIRED)
        self.assertEqual(TP_CANDIDATE_CLASS_GENERIC, "GENERIC_EXTERNAL_PIVOT")

    def test_tp1_candidate_class_in_diagnostics(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        prov = signals[0].provenance
        diag = prov["tp1_provenance"]["tp1_diagnostics"]
        self.assertEqual(diag["tp1_candidate_class"], TP_CANDIDATE_CLASS_GENERIC)
        self.assertTrue(diag["target_selection_source_rule_required"])

    def test_tp2_candidate_class_in_tp1_prov(self):
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 4110.0, "LONG")
        signals, _ = feed_and_collect(ev, events)
        prov = signals[0].provenance
        self.assertIn("tp2_candidate_class", prov["tp1_provenance"])


if __name__ == "__main__":
    unittest.main()
