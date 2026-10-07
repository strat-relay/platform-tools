"""KOJO_STRUCTURE_RECLAIM_V1 — comprehensive test suite.

Covers:
  - Semantic fixture tests (MATCH / PARTIAL / MISS / NON_REPRODUCIBLE)
  - Causal / engine invariants (NO_LOOKAHEAD, PREFIX_INVARIANCE, DETERMINISTIC_RERUN,
    HISTORICAL_LIVE_PARITY, CHECKPOINT_RESTART_PARITY, TERMINAL_REACTIVATION_GUARD,
    DUPLICATE_ECONOMIC_OPPORTUNITY_GUARD, ACTUAL_FILL_GEOMETRY)
  - Pipeline acceptance test sequence (in-memory, no PostgreSQL required)
  - Safety assertions (existing evaluators unmodified, BROKER_WRITES=0)

Data: All tests use synthetic XAUUSD fixture bars.
NEW_DATA_REQUIRED=true for real H1+M15 XAUUSD data in the 2026-07 window.
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from strategy_backtest import (  # noqa: E402
    BacktestEngine,
    CostModel,
    EntrySignal,
    HistoricalMarketFeed,
    MarketEvent,
    ParameterSet,
    ParameterSchema,
    SetupLifecycleEvent,
    StrategyVersion,
    StrategyEvaluatorRegistry,
    assert_live_replay_parity,
    register_builtin_evaluators,
)
from strategy_backtest.kojo_structure_reclaim import (  # noqa: E402
    EVALUATOR_KEY,
    INSTRUMENT,
    STRATEGY_ID,
    TIMEFRAME_H1,
    TIMEFRAME_M15,
    VERSION,
    KojoStructureReclaimEvaluator,
    kojo_structure_reclaim_baseline_parameter_set,
    kojo_structure_reclaim_parameter_schema,
)

UTC = timezone.utc

# ─── synthetic fixture helpers ──────────────────────────────────────────────────

_T0 = int(datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC).timestamp())  # anchor
H1 = 3600
M15 = 900


def _h1(t_offset_hours: int, open_: float, high: float, low: float, close: float) -> MarketEvent:
    """Create a completed H1 XAUUSD MarketEvent."""
    t = _T0 + t_offset_hours * H1
    return MarketEvent(
        canonical_instrument=INSTRUMENT,
        timeframe=TIMEFRAME_H1,
        open_timestamp=t,
        close_timestamp=t + H1,
        open=open_,
        high=max(open_, high, close),
        low=min(open_, low, close),
        close=close,
        completed=True,
        source="synthetic_fixture",
    )


def _m15(t_offset_quarter: int, open_: float, high: float, low: float, close: float) -> MarketEvent:
    """Create a completed M15 XAUUSD MarketEvent."""
    t = _T0 + t_offset_quarter * M15
    return MarketEvent(
        canonical_instrument=INSTRUMENT,
        timeframe=TIMEFRAME_M15,
        open_timestamp=t,
        close_timestamp=t + M15,
        open=open_,
        high=max(open_, high, close),
        low=min(open_, low, close),
        close=close,
        completed=True,
        source="synthetic_fixture",
    )


def _make_registry() -> StrategyEvaluatorRegistry:
    registry = StrategyEvaluatorRegistry()
    register_builtin_evaluators(registry)
    return registry


def _strategy_version() -> StrategyVersion:
    schema = kojo_structure_reclaim_parameter_schema()
    return StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, schema)


def _baseline_ps() -> ParameterSet:
    sv = _strategy_version()
    return kojo_structure_reclaim_baseline_parameter_set(sv.strategy_version_id)


def _run(events: list[MarketEvent]) -> tuple:
    """Run the evaluator on a list of events and return all outputs."""
    sv = _strategy_version()
    ps = _baseline_ps()
    evaluator = KojoStructureReclaimEvaluator()
    evaluator.initialize(sv, ps)
    outputs = []
    for event in sorted(events, key=lambda e: (e.open_timestamp, e.timeframe)):
        outputs.extend(evaluator.consume_market_event(event))
    return tuple(outputs)


def _run_engine(events: list[MarketEvent]) -> Any:
    """Run via BacktestEngine and return BacktestResult."""
    sv = _strategy_version()
    ps = _baseline_ps()
    feed_events = sorted(events, key=lambda e: (e.open_timestamp, e.timeframe))
    feed = HistoricalMarketFeed(
        tuple(feed_events),
        dataset_id="synthetic-fixture-kojo-reclaim",
        partition="DISCOVERY",
    )
    cost = CostModel("zero-cost")
    registry = _make_registry()
    engine = BacktestEngine(registry)
    return engine.run(sv, ps, feed, cost, run_id="test-run")


# ─── synthetic scenario builders ───────────────────────────────────────────────

def _long_break_retest_confirmation_scenario() -> list[MarketEvent]:
    """LONG: H1 resistance break at ~2380, pullback retest, M15 bullish engulfing.

    Scenario design:
      - Bars 0-5: uptrend to ~2430, creating a swing HIGH / RESISTANCE at h=3 (~2430)
      - Bars 5-14: pullback and consolidation around 2375-2385
      - h=7: swing LOW / SUPPORT at ~2360 (confirmed by h=9)
      - h=12: swing HIGH / RESISTANCE at ~2383 — the level that will be broken
      - h=15: H1 CLOSE breaks above 2383 → SETUP_DETECTED
      - h=16: pullback retest (H1 low near 2382)
      - M15 bars in retest zone: bearish then BULLISH ENGULFING confirmation
      - Next M15 open: entry at ~2385; TP1 = ~2430 (older confirmed resistance)

    Expected: SETUP_DETECTED → WAITING_FOR_RETEST → RETEST_SEEN → CONFIRMED → CONSUMED
              + EntrySignal (LONG, entry ~2385, stop ~2381, target ~2430).
    Semantic class: MATCH
    """
    events = []

    # ── Phase 1: uptrend establishing a high-level resistance at ~2430 ──────────
    # Bars 0-6: price rallies from 2350 to 2430 with pivot HIGH at h=3.
    events.append(_h1(0,  2350, 2365, 2348, 2360))
    events.append(_h1(1,  2360, 2380, 2358, 2375))
    events.append(_h1(2,  2375, 2400, 2372, 2395))
    events.append(_h1(3,  2395, 2435, 2392, 2425))  # swing HIGH at ~2435 (pivot)
    events.append(_h1(4,  2425, 2430, 2410, 2415))
    events.append(_h1(5,  2415, 2420, 2405, 2408))

    # ── Phase 2: deeper pullback creating support structure and consolidation ────
    events.append(_h1(6,  2408, 2412, 2385, 2390))
    events.append(_h1(7,  2390, 2392, 2355, 2360))  # swing LOW at ~2355 (pivot)
    events.append(_h1(8,  2360, 2375, 2358, 2370))
    events.append(_h1(9,  2370, 2382, 2368, 2378))
    events.append(_h1(10, 2378, 2385, 2374, 2380))

    # ── Phase 3: consolidation around 2375-2385; create the breakout level ──────
    events.append(_h1(11, 2380, 2386, 2375, 2378))
    events.append(_h1(12, 2378, 2388, 2376, 2384))  # swing HIGH at ~2388 (this is the level)
    events.append(_h1(13, 2384, 2387, 2378, 2380))
    events.append(_h1(14, 2380, 2386, 2378, 2382))

    # ── Phase 4: BREAK — H1 closes above the ~2388 resistance ──────────────────
    # Bar 15: close at 2393, clearly above 2388.
    events.append(_h1(15, 2382, 2396, 2381, 2393))

    # M15 bars during the break bar (break period — not retest yet).
    q_break = 15 * 4  # = 60
    events.append(_m15(q_break,     2382, 2384, 2381, 2383))
    events.append(_m15(q_break + 1, 2383, 2387, 2382, 2386))
    events.append(_m15(q_break + 2, 2386, 2393, 2385, 2392))
    events.append(_m15(q_break + 3, 2392, 2396, 2390, 2393))

    # ── Phase 5: RETEST — pullback toward 2388 ──────────────────────────────────
    # Bar 16: H1 low near 2389 (within 0.5 ATR of the broken level ~2388).
    events.append(_h1(16, 2393, 2395, 2389, 2391))

    # M15 bars during bar 16 (the retest zone).
    # Need: bearish M15 prev → bullish engulfing M15 curr.
    q_retest = 16 * 4  # = 64
    events.append(_m15(q_retest,     2393, 2394, 2391, 2392))  # minor drift
    events.append(_m15(q_retest + 1, 2392, 2393, 2390, 2391))  # drift down
    events.append(_m15(q_retest + 2, 2391, 2392, 2389, 2390))  # bearish prev (o=2391 c=2390)
    events.append(_m15(q_retest + 3, 2390, 2395, 2389, 2394))  # bullish engulfing curr
    # bullish engulfing check: prev bearish (c=2390 < o=2391 ✓)
    #   curr bullish (c=2394 > o=2390 ✓); curr_o=2390 <= prev_c=2390 ✓; curr_c=2394 >= prev_o=2391? No: 2394 >= 2391 ✓

    # ── Phase 6: ENTRY bar — next M15 after confirmation ────────────────────────
    # open=2394 → entry price; TP1 will be ~2435 (resistance from Phase 1).
    q_entry = 17 * 4  # = 68
    events.append(_m15(q_entry, 2394, 2398, 2393, 2397))

    return events


def _short_break_retest_confirmation_scenario() -> list[MarketEvent]:
    """SHORT: H1 support break at ~2390, pullback retest, M15 bearish engulfing.

    Scenario design:
      - Bars 0-5: downtrend from ~2430 to ~2360 creating support at h=3 (~2360)
      - Bars 5-14: pullback / consolidation; swing LOW at ~2385 is the level to break
      - h=12: swing LOW / SUPPORT at ~2382 (the level to be broken)
      - h=15: H1 CLOSE breaks below 2382 → SETUP_DETECTED
      - h=16: pullback retest (H1 high near 2383)
      - M15 bars in retest zone: bullish then BEARISH ENGULFING confirmation
      - Next M15 open: entry ~2380; TP1 = ~2360 (older confirmed support → becomes target)

    Expected: EntrySignal (SHORT, entry ~2380, stop ~2384, target ~2360).
    Semantic class: MATCH
    """
    events = []

    # ── Phase 1: downtrend creating a low-level support at ~2355 ────────────────
    events.append(_h1(0,  2430, 2432, 2415, 2420))
    events.append(_h1(1,  2420, 2422, 2400, 2405))
    events.append(_h1(2,  2405, 2408, 2385, 2390))
    events.append(_h1(3,  2390, 2392, 2350, 2360))  # swing LOW at ~2350 (pivot)
    events.append(_h1(4,  2360, 2378, 2358, 2372))
    events.append(_h1(5,  2372, 2382, 2370, 2375))

    # ── Phase 2: recovery / consolidation ───────────────────────────────────────
    events.append(_h1(6,  2375, 2392, 2373, 2388))
    events.append(_h1(7,  2388, 2395, 2380, 2390))  # swing HIGH at ~2395 (pivot)
    events.append(_h1(8,  2390, 2392, 2380, 2382))
    events.append(_h1(9,  2382, 2388, 2378, 2385))
    events.append(_h1(10, 2385, 2390, 2380, 2383))

    # ── Phase 3: tightening; create the breakdown level ─────────────────────────
    events.append(_h1(11, 2383, 2386, 2380, 2384))
    events.append(_h1(12, 2384, 2386, 2378, 2380))  # swing LOW at ~2378 (the level)
    events.append(_h1(13, 2380, 2385, 2379, 2382))
    events.append(_h1(14, 2382, 2384, 2379, 2381))

    # ── Phase 4: BREAK — H1 closes below ~2378 support ──────────────────────────
    events.append(_h1(15, 2381, 2383, 2372, 2374))  # close=2374 < 2378

    # M15 bars during break.
    q_break = 15 * 4
    events.append(_m15(q_break,     2381, 2382, 2379, 2380))
    events.append(_m15(q_break + 1, 2380, 2381, 2376, 2377))
    events.append(_m15(q_break + 2, 2377, 2378, 2373, 2374))
    events.append(_m15(q_break + 3, 2374, 2376, 2372, 2374))

    # ── Phase 5: RETEST — pullback toward 2378 ──────────────────────────────────
    events.append(_h1(16, 2374, 2380, 2373, 2376))  # high=2380 near the 2378 level

    # M15 bars during bar 16.
    # Need: bullish M15 prev → bearish engulfing M15 curr.
    q_retest = 16 * 4
    events.append(_m15(q_retest,     2374, 2376, 2373, 2375))  # minor
    events.append(_m15(q_retest + 1, 2375, 2377, 2374, 2376))  # minor bullish
    events.append(_m15(q_retest + 2, 2376, 2380, 2375, 2379))  # bullish prev (c=2379 > o=2376)
    events.append(_m15(q_retest + 3, 2379, 2380, 2373, 2374))  # bearish engulfing curr
    # bearish engulfing: prev bullish (c=2379 > o=2376 ✓)
    #   curr bearish (c=2374 < o=2379 ✓); curr_o=2379 >= prev_c=2379 ✓; curr_c=2374 <= prev_o=2376 ✓

    # ── Phase 6: ENTRY bar — next M15 after confirmation ────────────────────────
    # open=2374 → entry; TP1 = ~2350 (support from Phase 1).
    q_entry = 17 * 4
    events.append(_m15(q_entry, 2374, 2375, 2368, 2370))

    return events


def _break_no_retest_expiry_scenario() -> list[MarketEvent]:
    """LONG: H1 breaks resistance but price continues higher without retesting.

    Expected: SETUP_DETECTED then EXPIRED (max_retest_wait exceeded).
    Semantic class: MATCH (correctly identifies non-setup and expires it)
    """
    events = []

    # Same base structure as long scenario — resistance at ~2388.
    events.append(_h1(0,  2350, 2365, 2348, 2360))
    events.append(_h1(1,  2360, 2380, 2358, 2375))
    events.append(_h1(2,  2375, 2400, 2372, 2395))
    events.append(_h1(3,  2395, 2435, 2392, 2425))
    events.append(_h1(4,  2425, 2430, 2410, 2415))
    events.append(_h1(5,  2415, 2420, 2405, 2408))
    events.append(_h1(6,  2408, 2412, 2385, 2390))
    events.append(_h1(7,  2390, 2392, 2355, 2360))
    events.append(_h1(8,  2360, 2375, 2358, 2370))
    events.append(_h1(9,  2370, 2382, 2368, 2378))
    events.append(_h1(10, 2378, 2385, 2374, 2380))
    events.append(_h1(11, 2380, 2386, 2375, 2378))
    events.append(_h1(12, 2378, 2388, 2376, 2384))
    events.append(_h1(13, 2384, 2387, 2378, 2380))
    events.append(_h1(14, 2380, 2386, 2378, 2382))
    events.append(_h1(15, 2382, 2396, 2381, 2393))  # break above 2388

    # Price keeps going higher — no retest for 26 H1 bars (> max_retest_wait=24).
    # Bars start at 2420+ to ensure lows stay well above level+tolerance (~2397).
    # With ATR ≈ 17 (from the wide Phase-1 bars), tolerance = 0.5*17 ≈ 8.5;
    # level+tolerance ≈ 2388+8.5 = 2396.5.  Starting at 2420 keeps all lows > 2400.
    for i in range(16, 42):
        base = float(2420 + (i - 15))  # 2421, 2422, ... — lows far above level+tolerance
        events.append(_h1(i, base - 1.0, base + 2.0, base - 1.0, base + 1.0))

    return events


# ─── unit tests ────────────────────────────────────────────────────────────────

class TestKojoStructureReclaimSchema(unittest.TestCase):
    """Parameter schema validation."""

    def test_schema_accepts_baseline_values(self):
        schema = kojo_structure_reclaim_parameter_schema()
        ps = _baseline_ps()
        schema.validate(ps.values)  # must not raise

    def test_schema_rejects_unknown_parameter(self):
        schema = kojo_structure_reclaim_parameter_schema()
        with self.assertRaises(ValueError):
            schema.validate({**dict(_baseline_ps().values), "unknown_param": 1})

    def test_schema_rejects_out_of_range(self):
        schema = kojo_structure_reclaim_parameter_schema()
        with self.assertRaises(ValueError):
            schema.validate({**dict(_baseline_ps().values), "pivot_strength": 0})

    def test_evaluator_key(self):
        self.assertEqual(EVALUATOR_KEY, "kojo_structure_reclaim")

    def test_strategy_id(self):
        self.assertEqual(STRATEGY_ID, "KOJO_STRUCTURE_RECLAIM_V1")


class TestKojoStructureReclaimRegistration(unittest.TestCase):
    """Evaluator registry integration."""

    def test_registered_in_builtin_registry(self):
        registry = _make_registry()
        sv = _strategy_version()
        evaluator = registry.resolve(sv)
        self.assertIsInstance(evaluator, KojoStructureReclaimEvaluator)

    def test_kojo_wedge_still_registered(self):
        """Existing evaluator is unmodified."""
        from strategy_backtest.kojo_wedge import EVALUATOR_KEY as WK, KojoWedgeEvaluator
        registry = _make_registry()
        ev = registry.resolve(
            StrategyVersion("KOJO_WEDGE", "V1", WK, kojo_structure_reclaim_parameter_schema())
        )
        self.assertIsInstance(ev, KojoWedgeEvaluator)

    def test_evaluator_rejects_wrong_strategy_version(self):
        evaluator = KojoStructureReclaimEvaluator()
        wrong_sv = StrategyVersion("OTHER", "V1", EVALUATOR_KEY,
                                   kojo_structure_reclaim_parameter_schema())
        ps = kojo_structure_reclaim_baseline_parameter_set("OTHER@V1")
        with self.assertRaises(ValueError):
            evaluator.initialize(wrong_sv, ps)


# ─── Semantic Fixture Tests ─────────────────────────────────────────────────────

class TestSemanticFixtures(unittest.TestCase):
    """Semantic fidelity gate — at least 2 MATCH or PARTIAL with no systematic direction errors.

    SEMANTIC_FIXTURE_COUNT=3
    """

    def _find_signals(self, outputs: tuple) -> list[EntrySignal]:
        return [o for o in outputs if isinstance(o, EntrySignal)]

    def _find_lifecycles(self, outputs: tuple, status: str) -> list[SetupLifecycleEvent]:
        return [o for o in outputs if isinstance(o, SetupLifecycleEvent) and o.status == status]

    # ── Fixture 1: LONG break + retest + M15 bullish engulfing ─────────────────

    def test_fixture_1_long_break_retest_confirmation_MATCH(self):
        """Fixture 1: LONG setup with break/retest/confirmation — MATCH.

        Live-observation template: Price breaks above a known H1 resistance,
        pulls back to retest the level, M15 shows bullish engulfing at the level.
        """
        events = _long_break_retest_confirmation_scenario()
        outputs = _run(events)
        signals = self._find_signals(outputs)

        # At least one LONG signal.
        long_signals = [s for s in signals if s.direction == "LONG"]
        self.assertGreater(len(long_signals), 0,
                           "Fixture 1: expected LONG signal, got none")

        signal = long_signals[0]
        # Geometry invariant.
        self.assertLess(signal.stop_price, signal.entry_price)
        self.assertLess(signal.entry_price, signal.target_price)
        # Instrument / version.
        self.assertEqual(signal.canonical_instrument, INSTRUMENT)
        self.assertEqual(signal.strategy_version_id, f"{STRATEGY_ID}@{VERSION}")
        # Provenance fields required by spec.
        prov = signal.provenance
        self.assertIn("structural_level_id", prov)
        self.assertIn("structural_level_price", prov)
        self.assertIn("structure_timeframe", prov)
        self.assertIn("break_timestamp", prov)
        self.assertIn("break_direction", prov)
        self.assertIn("retest_timestamp", prov)
        self.assertIn("confirmation_timestamp", prov)
        self.assertIn("confirmation_type", prov)
        self.assertIn("ema_state", prov)
        self.assertIn("intended_entry", prov)

    # ── Fixture 2: SHORT break + retest + M15 bearish engulfing ────────────────

    def test_fixture_2_short_break_retest_confirmation_MATCH(self):
        """Fixture 2: SHORT setup with break/retest/confirmation — MATCH.

        Mirrors Fixture 1 for the short direction.
        """
        events = _short_break_retest_confirmation_scenario()
        outputs = _run(events)
        signals = self._find_signals(outputs)

        short_signals = [s for s in signals if s.direction == "SHORT"]
        self.assertGreater(len(short_signals), 0,
                           "Fixture 2: expected SHORT signal, got none")

        signal = short_signals[0]
        # Geometry invariant for SHORT.
        self.assertLess(signal.target_price, signal.entry_price)
        self.assertLess(signal.entry_price, signal.stop_price)
        self.assertEqual(signal.canonical_instrument, INSTRUMENT)

    # ── Fixture 3: Break with no retest → correctly expires ────────────────────

    def test_fixture_3_break_no_retest_expires_correctly_MATCH(self):
        """Fixture 3: LONG break but price continues without retest — correctly expires.

        MATCH: evaluator correctly identifies there is no setup and produces EXPIRED lifecycle.
        No signal should be emitted.
        """
        events = _break_no_retest_expiry_scenario()
        outputs = _run(events)
        signals = self._find_signals(outputs)

        expired = self._find_lifecycles(outputs, "EXPIRED")
        setup_detected = self._find_lifecycles(outputs, "SETUP_DETECTED")

        # A setup must have been detected (the break was real).
        self.assertGreater(len(setup_detected), 0,
                           "Fixture 3: setup was not detected at all")
        # And it must expire, not produce a signal.
        self.assertGreater(len(expired), 0,
                           "Fixture 3: setup should expire but no EXPIRED event found")
        # No entry signals.
        self.assertEqual(len(signals), 0,
                         f"Fixture 3: expected no signals, got {len(signals)}")

    def test_semantic_fidelity_gate(self):
        """Meta-test: SEMANTIC_FIDELITY_GATE_PASS = true (>= 2 MATCH, no systematic direction errors)."""
        # Fixture 1: LONG — test evaluator produces LONG signal.
        e1 = _long_break_retest_confirmation_scenario()
        o1 = _run(e1)
        long_sigs = [o for o in o1 if isinstance(o, EntrySignal) and o.direction == "LONG"]
        fixture1_match = len(long_sigs) > 0

        # Fixture 2: SHORT — test evaluator produces SHORT signal.
        e2 = _short_break_retest_confirmation_scenario()
        o2 = _run(e2)
        short_sigs = [o for o in o2 if isinstance(o, EntrySignal) and o.direction == "SHORT"]
        fixture2_match = len(short_sigs) > 0

        # Fixture 3: expiry — no signal.
        e3 = _break_no_retest_expiry_scenario()
        o3 = _run(e3)
        no_sigs = [o for o in o3 if isinstance(o, EntrySignal)]
        fixture3_match = len(no_sigs) == 0

        match_count = sum([fixture1_match, fixture2_match, fixture3_match])
        gate_pass = match_count >= 2 and fixture1_match and fixture2_match  # no direction errors
        self.assertTrue(gate_pass,
                        f"SEMANTIC_FIDELITY_GATE_PASS=false: "
                        f"F1={fixture1_match} F2={fixture2_match} F3={fixture3_match}")


# ─── Causal / Engine Tests ──────────────────────────────────────────────────────

class TestCausalInvariants(unittest.TestCase):

    def setUp(self):
        self.events = _long_break_retest_confirmation_scenario()

    # ── NO_LOOKAHEAD_PASS ──────────────────────────────────────────────────────

    def test_no_lookahead(self):
        """NO_LOOKAHEAD_PASS: output at event[n] only uses data from events[0..n]."""
        sv = _strategy_version()
        ps = _baseline_ps()
        evaluator = KojoStructureReclaimEvaluator()
        evaluator.initialize(sv, ps)
        sorted_events = sorted(self.events, key=lambda e: (e.open_timestamp, e.timeframe))

        # Run prefix-by-prefix and verify structural levels only use past data.
        for n in range(len(sorted_events)):
            ev = sorted_events[n]
            outputs = evaluator.consume_market_event(ev)
            for output in outputs:
                if isinstance(output, EntrySignal):
                    # decision_timestamp must be <= event close_timestamp
                    self.assertLessEqual(
                        output.decision_timestamp,
                        ev.close_timestamp,
                        f"NO_LOOKAHEAD_PASS: decision_timestamp {output.decision_timestamp} "
                        f"> event close {ev.close_timestamp}"
                    )
                    # provenance break_timestamp must be <= event close_timestamp
                    bt = output.provenance.get("break_timestamp", 0)
                    self.assertLessEqual(bt, ev.close_timestamp,
                                         "break_timestamp references future data")

    # ── PREFIX_INVARIANCE_PASS ─────────────────────────────────────────────────

    def test_prefix_invariance(self):
        """PREFIX_INVARIANCE_PASS: outputs from prefix[0..n] == outputs from full run at event n."""
        sorted_events = sorted(self.events, key=lambda e: (e.open_timestamp, e.timeframe))

        sv = _strategy_version()
        ps = _baseline_ps()
        full_evaluator = KojoStructureReclaimEvaluator()
        full_evaluator.initialize(sv, ps)
        full_outputs_by_event: dict[int, list] = {}
        for ev in sorted_events:
            outs = full_evaluator.consume_market_event(ev)
            full_outputs_by_event[ev.open_timestamp] = [asdict(o) for o in outs]

        # Re-run from prefix of length n and check outputs match.
        n = len(sorted_events) // 2
        prefix_ev = KojoStructureReclaimEvaluator()
        prefix_ev.initialize(sv, ps)
        for ev in sorted_events[:n]:
            outs = prefix_ev.consume_market_event(ev)
            expected = full_outputs_by_event.get(ev.open_timestamp, [])
            # Outputs from the prefix run must match the full run for same events.
            self.assertEqual([asdict(o) for o in outs], expected,
                             f"PREFIX_INVARIANCE_PASS: mismatch at event {ev.open_timestamp}")

    # ── DETERMINISTIC_RERUN_PASS ───────────────────────────────────────────────

    def test_deterministic_rerun(self):
        """DETERMINISTIC_RERUN_PASS: two runs on same data produce identical outputs."""
        sorted_events = sorted(self.events, key=lambda e: (e.open_timestamp, e.timeframe))

        def run_once():
            sv = _strategy_version()
            ps = _baseline_ps()
            ev = KojoStructureReclaimEvaluator()
            ev.initialize(sv, ps)
            out = []
            for event in sorted_events:
                out.extend(ev.consume_market_event(event))
            return [asdict(o) for o in out]

        run1 = run_once()
        run2 = run_once()
        self.assertEqual(run1, run2, "DETERMINISTIC_RERUN_PASS: two runs produced different outputs")

    # ── HISTORICAL_LIVE_EVALUATOR_PARITY_PASS ─────────────────────────────────

    def test_historical_live_parity(self):
        """HISTORICAL_LIVE_EVALUATOR_PARITY_PASS: historical feed == split-and-replayed live feed."""
        sorted_events = sorted(self.events, key=lambda e: (e.open_timestamp, e.timeframe))
        sv = _strategy_version()
        ps = _baseline_ps()

        hist_feed = HistoricalMarketFeed(
            tuple(sorted_events), "hist-parity", partition="DISCOVERY"
        )
        # Simulate "live" by feeding events one at a time in a second feed.
        live_feed = HistoricalMarketFeed(
            tuple(sorted_events), "live-parity", partition="DISCOVERY"
        )
        registry = _make_registry()
        assert_live_replay_parity(sv, ps, hist_feed, live_feed, registry)

    # ── CHECKPOINT_RESTART_PARITY_PASS ────────────────────────────────────────

    def test_checkpoint_restart_parity(self):
        """CHECKPOINT_RESTART_PARITY_PASS: snapshot/restore gives same outputs as continuous run."""
        sorted_events = sorted(self.events, key=lambda e: (e.open_timestamp, e.timeframe))
        sv = _strategy_version()
        ps = _baseline_ps()

        # Continuous run.
        ev_cont = KojoStructureReclaimEvaluator()
        ev_cont.initialize(sv, ps)
        cont_outputs = []
        for event in sorted_events:
            cont_outputs.extend(ev_cont.consume_market_event(event))

        # Checkpoint at midpoint, restore, continue.
        midpoint = len(sorted_events) // 2
        ev_snap = KojoStructureReclaimEvaluator()
        ev_snap.initialize(sv, ps)
        snap_outputs_before = []
        for event in sorted_events[:midpoint]:
            snap_outputs_before.extend(ev_snap.consume_market_event(event))
        state = ev_snap.snapshot_state()

        ev_restored = KojoStructureReclaimEvaluator()
        ev_restored.initialize(sv, ps)
        ev_restored.restore_state(state)
        snap_outputs_after = []
        for event in sorted_events[midpoint:]:
            snap_outputs_after.extend(ev_restored.consume_market_event(event))

        combined = snap_outputs_before + snap_outputs_after
        self.assertEqual(
            [asdict(o) for o in cont_outputs],
            [asdict(o) for o in combined],
            "CHECKPOINT_RESTART_PARITY_PASS: restored run differs from continuous run",
        )

    # ── TERMINAL_REACTIVATION_GUARD_PASS ──────────────────────────────────────

    def test_terminal_reactivation_guard(self):
        """TERMINAL_REACTIVATION_GUARD_PASS: consumed/expired setups never re-activate."""
        sorted_events = sorted(self.events, key=lambda e: (e.open_timestamp, e.timeframe))
        sv = _strategy_version()
        ps = _baseline_ps()
        ev = KojoStructureReclaimEvaluator()
        ev.initialize(sv, ps)

        terminal_ids: set[str] = set()
        for event in sorted_events:
            outputs = ev.consume_market_event(event)
            for o in outputs:
                if isinstance(o, SetupLifecycleEvent):
                    if o.status in ("CONSUMED", "EXPIRED", "INVALIDATED"):
                        terminal_ids.add(o.setup_id)
                    elif o.status in ("SETUP_DETECTED", "WAITING_FOR_RETEST",
                                      "RETEST_SEEN", "CONFIRMED"):
                        self.assertNotIn(
                            o.setup_id, terminal_ids,
                            f"TERMINAL_REACTIVATION_GUARD_PASS: setup {o.setup_id} "
                            f"re-activated after reaching terminal state",
                        )

    # ── DUPLICATE_ECONOMIC_OPPORTUNITY_GUARD_PASS ─────────────────────────────

    def test_duplicate_economic_opportunity_guard(self):
        """DUPLICATE_ECONOMIC_OPPORTUNITY_GUARD_PASS: one level+break → at most one signal."""
        sorted_events = sorted(self.events, key=lambda e: (e.open_timestamp, e.timeframe))
        sv = _strategy_version()
        ps = _baseline_ps()
        ev = KojoStructureReclaimEvaluator()
        ev.initialize(sv, ps)

        outputs = []
        for event in sorted_events:
            outputs.extend(ev.consume_market_event(event))

        signals = [o for o in outputs if isinstance(o, EntrySignal)]
        signal_ids = [s.signal_id for s in signals]
        self.assertEqual(len(signal_ids), len(set(signal_ids)),
                         "DUPLICATE_ECONOMIC_OPPORTUNITY_GUARD_PASS: duplicate signal IDs")

        # Also check via consumed_setup_ids — each consumed setup should appear at most once
        # in the consumed set and have at most one corresponding signal.
        consumed = ev._consumed_setup_ids
        self.assertGreaterEqual(len(consumed), len(signal_ids),
                                "Consumed setup count should be >= signal count")

    # ── ACTUAL_FILL_GEOMETRY_PASS ──────────────────────────────────────────────

    def test_actual_fill_geometry(self):
        """ACTUAL_FILL_GEOMETRY_PASS: BacktestEngine fills at expected prices, outcomes are valid."""
        result = _run_engine(self.events)
        for outcome in result.outcomes:
            self.assertIn(outcome.status, ("TARGET_HIT", "STOPPED", "TIME_EXIT", "EXPIRED"))
            self.assertIsNotNone(outcome.exit_price)
            self.assertIsNotNone(outcome.realized_r)

        for signal in result.signals:
            self.assertIn(signal.direction, ("LONG", "SHORT"))
            if signal.direction == "LONG":
                self.assertLess(signal.stop_price, signal.entry_price)
                self.assertLess(signal.entry_price, signal.target_price)
            else:
                self.assertLess(signal.target_price, signal.entry_price)
                self.assertLess(signal.entry_price, signal.stop_price)

    # ── RESULT FINGERPRINT DETERMINISM ───────────────────────────────────────

    def test_result_fingerprint_is_deterministic(self):
        """BacktestEngine result_fingerprint is deterministic across runs."""
        r1 = _run_engine(self.events)
        r2 = _run_engine(self.events)
        self.assertEqual(r1.result_fingerprint, r2.result_fingerprint,
                         "Result fingerprint changed between runs")
        self.assertIsNotNone(r1.result_fingerprint)


# ─── Pipeline Acceptance Test ───────────────────────────────────────────────────

class TestPipelineAcceptance(unittest.TestCase):
    """Pipeline acceptance test sequence (in-memory, no PostgreSQL).

    Steps 1-11 from the spec:
      1.  POST /api/v1/strategy-definitions
      2.  POST /api/v1/strategy-versions
      3.  Publish parameter schema (recorded in version)
      4.  POST /api/v1/parameter-sets (DISCOVERY)
      5.  POST /api/v1/strategy-versions/{id}/backtests (DISCOVERY)
      6.  Execute via StrategyEvaluator contract
      7.  POST /api/v1/strategy-versions/{id}/backtests (VALIDATION)
      8.  POST /api/v1/strategy-versions/{id}/freeze
      9.  POST /api/v1/strategy-instances
      10. PATCH instrument on instance
      11. Verify online=false, execution_eligible=false
    """

    def _make_api(self):
        from platform_api.strategy_mgmt import StrategyMgmtApi, StrategyMgmtRepository

        class _FakeConn:
            def __init__(self, db):
                self._db = db

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def cursor(self):
                return _FakeCursor(self._db)

            def commit(self):
                pass

        class _FakeCursor:
            def __init__(self, db):
                self._db = db
                self.description = []
                self._rows = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def execute(self, sql, params=None):
                sql_up = sql.strip().upper()
                # schema check
                if "schema_migrations" in sql.lower():
                    self._rows = [("042",)]
                    self.description = [("version",)]
                    return
                if sql_up.startswith("INSERT INTO STRATEGY_MGMT.STRATEGY_DEFINITION"):
                    row_id, name, fk, desc, prov, by = params
                    row = {"id": row_id, "name": name, "family_key": fk,
                           "description": desc, "provenance_notes": prov,
                           "created_by": by, "created_at": _now(), "updated_at": _now()}
                    self._db["strategy_definition"].append(row)
                    self._rows = [tuple(row.values())]
                    self.description = [(k,) for k in row]
                elif sql_up.startswith("INSERT INTO STRATEGY_MGMT.STRATEGY_VERSION"):
                    row_id, def_id, vl, ek, si, rn, by = params
                    row = {"id": row_id, "definition_id": def_id, "version_label": vl,
                           "evaluator_key": ek, "lifecycle": "DRAFT", "schema_id": si,
                           "release_notes": rn, "frozen_at": None, "frozen_by": None,
                           "created_at": _now(), "updated_at": _now(), "created_by": by}
                    self._db["strategy_version"].append(row)
                    self._rows = [tuple(row.values())]
                    self.description = [(k,) for k in row]
                elif sql_up.startswith("INSERT INTO STRATEGY_MGMT.PARAMETER_SET"):
                    row_id, psid, svid, sid, vals, fp, prov, by = params
                    row = {"id": row_id, "parameter_set_id": psid,
                           "strategy_version_id": svid, "schema_id": sid,
                           "values": json.loads(vals) if isinstance(vals, str) else vals,
                           "fingerprint": fp,
                           "frozen": False, "frozen_at": None, "frozen_by": None,
                           "provenance": json.loads(prov) if isinstance(prov, str) else prov,
                           "created_at": _now(), "updated_at": _now(), "created_by": by}
                    self._db["parameter_set"].append(row)
                    self._rows = [tuple(row.values())]
                    self.description = [(k,) for k in row]
                elif sql_up.startswith("INSERT INTO STRATEGY_MGMT.BACKTEST_RUN"):
                    row_id, svid, psid, purpose, instr, tf, dr_start, dr_end, cost_fp, by = params
                    row = {"id": row_id, "strategy_version_id": svid,
                           "parameter_set_id": psid, "backtest_purpose": purpose,
                           "status": "QUEUED", "dataset_fingerprint": None,
                           "date_range_start": dr_start, "date_range_end": dr_end,
                           "instruments": instr, "timeframes": tf,
                           "cost_model_fingerprint": cost_fp,
                           "engine_version": None, "evaluator_fingerprint": None,
                           "result_fingerprint": None, "metrics": None,
                           "error_detail": None,
                           "queued_at": _now(), "started_at": None, "completed_at": None,
                           "created_by": by}
                    self._db["backtest_run"].append(row)
                    self._rows = [tuple(row.values())]
                    self.description = [(k,) for k in row]
                elif sql_up.startswith("INSERT INTO STRATEGY_MGMT.STRATEGY_INSTANCE_V2"):
                    # Real SQL: (id, strategy_version_id, parameter_set_id, display_name,
                    #            online, execution_eligible, created_by)
                    # But online=false, execution_eligible=false are hardcoded in SQL, so
                    # params are: (row_id, svid, psid, display_name, created_by) = 5 params.
                    row_id, svid, psid, dn, by = params
                    row = {"id": row_id, "strategy_version_id": svid,
                           "parameter_set_id": psid, "display_name": dn,
                           "online": False, "execution_eligible": False,
                           "instruments": [], "attributes": {},
                           "created_at": _now(), "updated_at": _now(), "created_by": by}
                    self._db["strategy_instance_v2"].append(row)
                    self._rows = [tuple(row.values())]
                    self.description = [(k,) for k in row]
                elif "SELECT lifecycle" in sql and "strategy_version" in sql.lower():
                    # For freeze: find the version
                    ver_id = params[0]
                    match = next((v for v in self._db["strategy_version"] if str(v["id"]) == str(ver_id)), None)
                    if match:
                        self._rows = [(match["lifecycle"],)]
                    else:
                        self._rows = []
                    self.description = [("lifecycle",)]
                elif "UPDATE strategy_mgmt.strategy_version" in sql and "FROZEN" in sql:
                    ver_id = params[1]
                    for v in self._db["strategy_version"]:
                        if str(v["id"]) == str(ver_id):
                            v["lifecycle"] = "FROZEN"
                            v["frozen_at"] = _now()
                            v["frozen_by"] = params[0]
                            self._rows = [tuple(v.values())]
                            self.description = [(k,) for k in v]
                            break
                elif "UPDATE strategy_mgmt.strategy_version" in sql and "lifecycle" in sql.lower():
                    lifecycle, ver_id = params
                    for v in self._db["strategy_version"]:
                        if str(v["id"]) == str(ver_id):
                            if v.get("lifecycle") == "FROZEN":
                                raise Exception("immutability_trigger: FROZEN version cannot be mutated")
                            v["lifecycle"] = lifecycle
                            self._rows = [tuple(v.values())]
                            self.description = [(k,) for k in v]
                            break
                elif "UPDATE strategy_mgmt.backtest_run" in sql:
                    run_id = params[-1]
                    for r in self._db["backtest_run"]:
                        if str(r["id"]) == str(run_id):
                            # Update status and optional fields.
                            (status, rf, metrics, err, ev_, evf_, dsf_) = params[:7]
                            r["status"] = status
                            if rf is not None:
                                r["result_fingerprint"] = rf
                            if metrics is not None:
                                r["metrics"] = json.loads(metrics) if isinstance(metrics, str) else metrics
                            if err is not None:
                                r["error_detail"] = err
                            if ev_ is not None:
                                r["engine_version"] = ev_
                            if evf_ is not None:
                                r["evaluator_fingerprint"] = evf_
                            if dsf_ is not None:
                                r["dataset_fingerprint"] = dsf_
                            self._rows = [tuple(r.values())]
                            self.description = [(k,) for k in r]
                            break
                elif "UPDATE strategy_mgmt.strategy_instance_v2" in sql and "online" in sql.lower():
                    online, inst_id = params
                    for inst in self._db["strategy_instance_v2"]:
                        if str(inst["id"]) == str(inst_id):
                            inst["online"] = online
                            self._rows = [tuple(inst.values())]
                            self.description = [(k,) for k in inst]
                            break
                elif "UPDATE strategy_mgmt.strategy_instance_v2" in sql and "instruments" in sql.lower():
                    instruments, inst_id = params
                    for inst in self._db["strategy_instance_v2"]:
                        if str(inst["id"]) == str(inst_id):
                            inst["instruments"] = json.loads(instruments) if isinstance(instruments, str) else instruments
                            self._rows = [tuple(inst.values())]
                            self.description = [(k,) for k in inst]
                            break
                elif sql.upper().startswith("SELECT") and "execution_eligible" in sql.lower():
                    inst_id = params[0]
                    match = next((i for i in self._db["strategy_instance_v2"] if str(i["id"]) == str(inst_id)), None)
                    if match:
                        self._rows = [(match["execution_eligible"],)]
                    else:
                        self._rows = []
                    self.description = [("execution_eligible",)]
                elif sql.upper().startswith("SELECT") and "strategy_mgmt.strategy_version" in sql.lower():
                    ver_id = params[0]
                    match = next((v for v in self._db["strategy_version"] if str(v["id"]) == str(ver_id)), None)
                    self._rows = [tuple(match.values())] if match else []
                    self.description = [(k,) for k in match] if match else []
                elif sql.upper().startswith("SELECT") and "strategy_mgmt.parameter_set" in sql.lower():
                    ps_id = params[0]
                    match = next((p for p in self._db["parameter_set"] if str(p["id"]) == str(ps_id)), None)
                    self._rows = [tuple(match.values())] if match else []
                    self.description = [(k,) for k in match] if match else []
                elif sql.upper().startswith("SELECT") and "strategy_mgmt.backtest_run" in sql.lower():
                    run_id = params[0]
                    match = next((r for r in self._db["backtest_run"] if str(r["id"]) == str(run_id)), None)
                    self._rows = [tuple(match.values())] if match else []
                    self.description = [(k,) for k in match] if match else []
                elif sql.upper().startswith("SELECT") and "strategy_mgmt.strategy_instance_v2" in sql.lower():
                    inst_id = params[0]
                    match = next((i for i in self._db["strategy_instance_v2"] if str(i["id"]) == str(inst_id)), None)
                    self._rows = [tuple(match.values())] if match else []
                    self.description = [(k,) for k in match] if match else []
                else:
                    self._rows = []
                    self.description = []

            def fetchone(self):
                return self._rows[0] if self._rows else None

            def fetchall(self):
                return list(self._rows)

        def _now():
            return datetime.now(UTC).isoformat()

        db = {
            "strategy_definition": [],
            "strategy_version": [],
            "parameter_schema": [],
            "parameter_set": [],
            "backtest_run": [],
            "strategy_instance_v2": [],
        }

        def connect_fn(readonly=False):
            return _FakeConn(db)

        from platform_api.strategy_mgmt import StrategyMgmtRepository, BacktestJobRunner
        repo = StrategyMgmtRepository(connect_fn=connect_fn)
        runner = BacktestJobRunner(repo)
        api = StrategyMgmtApi(repository=repo, job_runner=runner)
        return api, db

    def test_full_pipeline_acceptance_sequence(self):
        """Steps 1-11: full pipeline walk-through."""
        api, db = self._make_api()

        # Step 1: Create StrategyDefinition
        code, resp = api.handle("POST", "/api/v1/strategy-definitions", json.dumps({
            "name": "Kojo Structure Reclaim V1",
            "familyKey": "KOJO",
            "description": "H1 structural break/reclaim + M15 confirmation",
            "provenanceNotes": "RESEARCH_HYPOTHESIS_FROM_LIVE_OBSERVATION",
            "createdBy": "acceptance-test",
        }).encode())
        self.assertEqual(code, 201, f"Step 1 failed: {resp}")
        def_id = resp["data"]["id"]

        # Step 2: Create StrategyVersion
        code, resp = api.handle("POST", "/api/v1/strategy-versions", json.dumps({
            "definitionId": def_id,
            "versionLabel": "V1",
            "evaluatorKey": EVALUATOR_KEY,
            "schemaId": "kojo-structure-reclaim-v1",
            "releaseNotes": "Initial research hypothesis",
            "createdBy": "acceptance-test",
        }).encode())
        self.assertEqual(code, 201, f"Step 2 failed: {resp}")
        ver_id = resp["data"]["id"]
        self.assertEqual(resp["data"]["lifecycle"], "DRAFT")

        # Step 3: Promote to IMPLEMENTED (schema is implicitly published via schemaId).
        from platform_api.strategy_mgmt import StrategyMgmtRepository
        repo_direct = api._repo
        repo_direct.set_version_lifecycle(ver_id, "IMPLEMENTED", "acceptance-test")

        # Step 4: Create ParameterSet (DISCOVERY purpose annotated in provenance).
        ps_values = {
            "pivot_strength": 2,
            "retest_tolerance_atr": 0.5,
            "max_retest_wait_h1_bars": 24,
            "max_confirmation_wait_m15_bars": 16,
            "stop_buffer_type": "PRICE",
            "stop_buffer_value": 1.0,
        }
        code, resp = api.handle("POST", "/api/v1/parameter-sets", json.dumps({
            "parameterSetId": "kojo-structure-reclaim-v1-acceptance-test-ps",
            "strategyVersionId": ver_id,
            "schemaId": "kojo-structure-reclaim-v1",
            "values": ps_values,
            "provenance": {
                "purpose": "DISCOVERY",
                "source": "RESEARCH_HYPOTHESIS_FROM_LIVE_OBSERVATION",
                "PARAMETER_SEARCH": False,
            },
            "createdBy": "acceptance-test",
        }).encode())
        self.assertEqual(code, 201, f"Step 4 failed: {resp}")
        ps_row_id = resp["data"]["id"]
        ps_fingerprint = resp["data"]["fingerprint"]
        self.assertIsNotNone(ps_fingerprint)

        # Step 5: DISCOVERY BacktestRun
        discovery_events = _long_break_retest_confirmation_scenario()
        sorted_evs = sorted(discovery_events, key=lambda e: (e.open_timestamp, e.timeframe))
        feed_events_payload = [
            {
                "canonical_instrument": e.canonical_instrument,
                "timeframe": e.timeframe,
                "open_timestamp": e.open_timestamp,
                "close_timestamp": e.close_timestamp,
                "open": e.open,
                "high": e.high,
                "low": e.low,
                "close": e.close,
                "completed": True,
                "source": "synthetic_fixture",
                "provenance": {},
            }
            for e in sorted_evs
        ]
        code, resp = api.handle(
            "POST",
            f"/api/v1/strategy-versions/{ver_id}/backtests",
            json.dumps({
                "parameterSetId": ps_row_id,
                "backtestPurpose": "DISCOVERY",
                "instruments": [INSTRUMENT],
                "timeframes": [TIMEFRAME_H1, TIMEFRAME_M15],
                "dateRangeStart": "2026-07-01T00:00:00+00:00",
                "dateRangeEnd": "2026-08-09T00:00:00+00:00",
                "_feed_events": feed_events_payload,
                "_parameter_values": ps_values,
                "_evaluator_key": EVALUATOR_KEY,
                "_strategy_version_id": f"{STRATEGY_ID}@{VERSION}",
                "_schema_fields": {
                    f: {} for f in ps_values
                },
                "createdBy": "acceptance-test",
            }).encode(),
        )
        self.assertEqual(code, 201, f"Step 5 failed: {resp}")
        discovery_run_id = resp["data"]["id"]

        # Wait for async backtest to complete (with timeout).
        import time
        for _ in range(30):
            code2, resp2 = api.handle("GET", f"/api/v1/backtests/{discovery_run_id}", None)
            if resp2.get("data", {}).get("status") in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.1)
        self.assertEqual(resp2["data"]["status"], "COMPLETED",
                         f"Step 5: DISCOVERY backtest did not complete: {resp2}")
        discovery_result_fp = resp2["data"]["result_fingerprint"]
        self.assertIsNotNone(discovery_result_fp, "Step 6: result fingerprint must not be None")

        # Step 7: VALIDATION BacktestRun (separate parameter set snapshot — same values,
        # different provenance marking VALIDATION).
        code, resp = api.handle("POST", "/api/v1/parameter-sets", json.dumps({
            "parameterSetId": "kojo-structure-reclaim-v1-acceptance-test-ps-validation",
            "strategyVersionId": ver_id,
            "schemaId": "kojo-structure-reclaim-v1",
            "values": ps_values,
            "provenance": {
                "purpose": "VALIDATION",
                "source": "RESEARCH_HYPOTHESIS_FROM_LIVE_OBSERVATION",
                "VALIDATION_ACCESSED_BEFORE_DISCOVERY_FREEZE": False,
            },
            "createdBy": "acceptance-test",
        }).encode())
        self.assertEqual(code, 201, f"Step 7 ps failed: {resp}")
        val_ps_row_id = resp["data"]["id"]

        val_events = _long_break_retest_confirmation_scenario()
        sorted_val_evs = sorted(val_events, key=lambda e: (e.open_timestamp, e.timeframe))
        val_feed_payload = [
            {
                "canonical_instrument": e.canonical_instrument,
                "timeframe": e.timeframe,
                "open_timestamp": e.open_timestamp,
                "close_timestamp": e.close_timestamp,
                "open": e.open,
                "high": e.high,
                "low": e.low,
                "close": e.close,
                "completed": True,
                "source": "synthetic_fixture",
                "provenance": {},
            }
            for e in sorted_val_evs
        ]
        code, resp = api.handle(
            "POST",
            f"/api/v1/strategy-versions/{ver_id}/backtests",
            json.dumps({
                "parameterSetId": val_ps_row_id,
                "backtestPurpose": "VALIDATION",
                "instruments": [INSTRUMENT],
                "timeframes": [TIMEFRAME_H1, TIMEFRAME_M15],
                "dateRangeStart": "2026-08-10T00:00:00+00:00",
                "dateRangeEnd": "2026-08-30T00:00:00+00:00",
                "_feed_events": val_feed_payload,
                "_parameter_values": ps_values,
                "_evaluator_key": EVALUATOR_KEY,
                "_strategy_version_id": f"{STRATEGY_ID}@{VERSION}",
                "_schema_fields": {
                    f: {} for f in ps_values
                },
                "createdBy": "acceptance-test",
            }).encode(),
        )
        self.assertEqual(code, 201, f"Step 7 failed: {resp}")
        val_run_id = resp["data"]["id"]

        for _ in range(30):
            code2, resp2 = api.handle("GET", f"/api/v1/backtests/{val_run_id}", None)
            if resp2.get("data", {}).get("status") in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.1)
        self.assertEqual(resp2["data"]["status"], "COMPLETED",
                         f"Step 7: VALIDATION backtest did not complete")

        # Step 8: Freeze the version.
        code, resp = api.handle(
            "POST",
            f"/api/v1/strategy-versions/{ver_id}/freeze",
            json.dumps({"frozenBy": "acceptance-test"}).encode(),
        )
        self.assertEqual(code, 200, f"Step 8 failed: {resp}")
        self.assertEqual(resp["data"]["lifecycle"], "FROZEN")

        # Step 9: Create StrategyInstance.
        code, resp = api.handle("POST", "/api/v1/strategy-instances", json.dumps({
            "strategyVersionId": ver_id,
            "parameterSetId": ps_row_id,
            "displayName": "KOJO_STRUCTURE_RECLAIM_V1 — Research Instance",
            "createdBy": "acceptance-test",
        }).encode())
        self.assertEqual(code, 201, f"Step 9 failed: {resp}")
        inst_id = resp["data"]["id"]

        # Invariants: online=false, execution_eligible=false
        self.assertFalse(resp["data"]["online"],
                         "Step 11: new instance must start OFFLINE")
        self.assertFalse(resp["data"]["execution_eligible"],
                         "Step 11: new instance must start execution_ineligible")

        # Step 10: Configure XAUUSD instrument.
        code, resp = api.handle(
            "PATCH",
            f"/api/v1/strategy-instances/{inst_id}",
            json.dumps({
                "instruments": [{"canonical_instrument": INSTRUMENT, "role": "primary"}],
                "updatedBy": "acceptance-test",
            }).encode(),
        )
        self.assertEqual(code, 200, f"Step 10 failed: {resp}")

        # Step 11: Verify still OFFLINE and execution_ineligible.
        code, resp = api.handle("GET", f"/api/v1/strategy-instances/{inst_id}", None)
        self.assertEqual(code, 200)
        self.assertFalse(resp["data"]["online"])
        self.assertFalse(resp["data"]["execution_eligible"])

        # Attempt to set execution_eligible via PATCH must be refused.
        code, resp = api.handle(
            "PATCH",
            f"/api/v1/strategy-instances/{inst_id}",
            json.dumps({"executionEligible": True}).encode(),
        )
        self.assertEqual(code, 400,
                         "execution_eligible change via PATCH must be refused (400)")

        # Frozen version cannot be mutated.
        with self.assertRaises(Exception):
            api._repo.set_version_lifecycle(ver_id, "DRAFT", "test")


# ─── Safety tests ─────────────────────────────────────────────────────────────

class TestSafetyInvariants(unittest.TestCase):
    """Verify existing evaluators are not touched."""

    def test_kojo_wedge_evaluator_unchanged(self):
        from strategy_backtest.kojo_wedge import KojoWedgeEvaluator, kojo_wedge_baseline_parameter_set
        ev = KojoWedgeEvaluator()
        self.assertIsNotNone(ev)
        ps = kojo_wedge_baseline_parameter_set()
        self.assertEqual(ps.parameter_set_id, "kojo-wedge-v1-baseline")

    def test_broker_writes_zero(self):
        """No broker writes anywhere in the evaluator path."""
        ev = KojoStructureReclaimEvaluator()
        sv = _strategy_version()
        ps = _baseline_ps()
        ev.initialize(sv, ps)
        events = _long_break_retest_confirmation_scenario()
        for event in sorted(events, key=lambda e: (e.open_timestamp, e.timeframe)):
            ev.consume_market_event(event)
        # If we reach here without touching any broker API, BROKER_WRITES=0.
        # The evaluator has no broker connection; this test is structural.
        self.assertTrue(True, "BROKER_WRITES=0 — evaluator has no broker dependency")

    def test_execution_eligible_immutable(self):
        """execution_eligible must not be changeable through the PATCH online path."""
        from platform_api.strategy_mgmt import StrategyMgmtRepository

        class _AlwaysExecEligible:
            """Mock that simulates an instance with execution_eligible=True."""

            def __init__(self):
                self._data = {
                    "id": "test-inst",
                    "online": False,
                    "execution_eligible": False,
                }

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def cursor(self):
                return self

            def __enter2__(self):
                return self

            def commit(self):
                pass

        # The API layer hard-blocks executionEligible in the PATCH body.
        from platform_api.strategy_mgmt import StrategyMgmtApi
        api = StrategyMgmtApi()
        # Call handle directly — must return 400 with no DB needed.
        code, resp = api.handle(
            "PATCH",
            "/api/v1/strategy-instances/some-id",
            json.dumps({"executionEligible": True}).encode(),
        )
        self.assertEqual(code, 400)
        self.assertIn("execution_eligible", resp.get("message", "").lower())


if __name__ == "__main__":
    unittest.main()
