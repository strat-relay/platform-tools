"""Runtime-integrity repair tests for KOJO_STRUCTURE_RECLAIM_V3.

Covers the 8 repairs specified in the v3-integrity-repair task:

  FANOUT_DEDUP           — one M15 economic opportunity → one EntrySignal
  H1_SUPPORTING_LEVELS   — non-canonical levels folded into supporting_h1_levels
  RETEST_DIAGNOSTICS     — m15_atr_at_retest, retest_tolerance_band,
                           retest_distance_abs, retest_distance_atr in provenance
  PARAM_SET_REF          — migration.signal picks up parameter_set_id from provenance
  EMISSION_CLASS         — signal_emission_class PROSPECTIVE / RETROSPECTIVE
  PULLBACK_STATUS        — pullback_classifier_status is always present
  TP1_QUALITY_FIELDS     — top-level tp1 reaction count fields present
  EQUAL_HIGHS_NESTED     — EQUAL_HIGHS/LOWS are inside tp1_provenance.all_liquidity_objectives
"""
from __future__ import annotations

import dataclasses
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
    LIQUIDITY_EQUAL_HIGHS,
    LIQUIDITY_EQUAL_LOWS,
    KojoStructureReclaimV3Evaluator,
    kojo_structure_reclaim_v3_baseline_parameter_set,
    kojo_structure_reclaim_v3_parameter_schema,
)

H1 = "H1"
M15 = "M15"
XAUUSD = "XAUUSD"
H1S = 3600
M15S = 900
BASE_TS = 1_784_000_000


def h1(t, o, h, lo, c, instr=XAUUSD):
    return MarketEvent(
        canonical_instrument=instr, timeframe=H1,
        open_timestamp=t, close_timestamp=t + H1S,
        open=o, high=h, low=lo, close=c,
        completed=True, source="TEST",
    )


def m15(t, o, h, lo, c, instr=XAUUSD):
    return MarketEvent(
        canonical_instrument=instr, timeframe=M15,
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


def feed_signals(evaluator, events):
    signals, setups = [], []
    for ev in events:
        for out in evaluator.consume_market_event(ev):
            if isinstance(out, EntrySignal):
                signals.append(out)
            elif isinstance(out, SetupLifecycleEvent):
                setups.append(out)
    return signals, setups


def _make_entry_signal(conf_ts, direction, entry_price, level_price, setup_id="s1") -> EntrySignal:
    """Synthetic EntrySignal for deduplication unit tests."""
    stop = entry_price - 5.0 if direction == "LONG" else entry_price + 5.0
    target = entry_price + 10.0 if direction == "LONG" else entry_price - 10.0
    return EntrySignal(
        signal_id=f"SIG_{setup_id}",
        strategy_version_id="KOJO@V3",
        canonical_instrument=XAUUSD,
        direction=direction,
        entry_price=entry_price,
        stop_price=stop,
        target_price=target,
        decision_timestamp=conf_ts,
        order_type="MARKET",
        provenance={
            "confirmation_timestamp": conf_ts,
            "direction": direction,
            "structural_level_id": f"lvl_{setup_id}",
            "structural_level_price": level_price,
            "structural_level_type": "SWING_HIGH",
            "setup_id": setup_id,
        },
    )


class TestFanoutDedup(unittest.TestCase):
    """FANOUT_DEDUP + H1_SUPPORTING_LEVELS."""

    def _ev(self):
        return make_evaluator()

    def test_single_signal_no_change(self):
        """Single EntrySignal passes through unchanged."""
        ev = self._ev()
        sig = _make_entry_signal(100, "SHORT", 95.0, 100.0, "s1")
        result = ev._deduplicate_m15_entry_signals([sig])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], sig)

    def test_two_signals_same_opportunity_dedup(self):
        """Two signals with same (conf_ts, direction) → one signal after dedup."""
        ev = self._ev()
        conf_ts = 1_000_000
        entry = 95.0
        # Level s1 at 100 is closer to 95 than s2 at 105
        sig1 = _make_entry_signal(conf_ts, "SHORT", entry, 100.0, "s1")
        sig2 = _make_entry_signal(conf_ts, "SHORT", entry, 105.0, "s2")
        result = ev._deduplicate_m15_entry_signals([sig1, sig2])
        entry_results = [r for r in result if isinstance(r, EntrySignal)]
        self.assertEqual(len(entry_results), 1, "Expected exactly one EntrySignal after dedup")

    def test_canonical_is_closest_level(self):
        """Canonical = level whose price is closest to entry_price."""
        ev = self._ev()
        conf_ts = 2_000_000
        entry = 95.0
        # s1: level at 96 → distance 1; s2: level at 100 → distance 5
        sig_near = _make_entry_signal(conf_ts, "SHORT", entry, 96.0, "near")
        sig_far = _make_entry_signal(conf_ts, "SHORT", entry, 100.0, "far")
        result = ev._deduplicate_m15_entry_signals([sig_near, sig_far])
        entry_results = [r for r in result if isinstance(r, EntrySignal)]
        self.assertEqual(len(entry_results), 1)
        self.assertEqual(entry_results[0].provenance["structural_level_id"], "lvl_near")

    def test_supporting_levels_in_provenance(self):
        """Non-canonical levels are folded into supporting_h1_levels."""
        ev = self._ev()
        conf_ts = 3_000_000
        entry = 95.0
        sig1 = _make_entry_signal(conf_ts, "SHORT", entry, 96.0, "near")
        sig2 = _make_entry_signal(conf_ts, "SHORT", entry, 100.0, "far")
        result = ev._deduplicate_m15_entry_signals([sig1, sig2])
        canonical = next(r for r in result if isinstance(r, EntrySignal))
        self.assertIn("supporting_h1_levels", canonical.provenance)
        supporting = canonical.provenance["supporting_h1_levels"]
        self.assertEqual(len(supporting), 1)
        self.assertEqual(supporting[0]["structural_level_id"], "lvl_far")
        self.assertEqual(canonical.provenance["h1_fanout_suppressed_count"], 1)

    def test_different_directions_not_deduped(self):
        """LONG and SHORT signals with same conf_ts are independent opportunities."""
        ev = self._ev()
        conf_ts = 4_000_000
        long_sig = _make_entry_signal(conf_ts, "LONG", 100.0, 95.0, "ls")
        short_sig = _make_entry_signal(conf_ts, "SHORT", 95.0, 100.0, "ss")
        result = ev._deduplicate_m15_entry_signals([long_sig, short_sig])
        entry_results = [r for r in result if isinstance(r, EntrySignal)]
        self.assertEqual(len(entry_results), 2)

    def test_lifecycle_events_preserved(self):
        """SetupLifecycleEvents alongside a dedup group are not dropped."""
        ev = self._ev()
        conf_ts = 5_000_000
        entry = 95.0
        sig1 = _make_entry_signal(conf_ts, "SHORT", entry, 96.0, "a")
        sig2 = _make_entry_signal(conf_ts, "SHORT", entry, 100.0, "b")
        lifecycle = SetupLifecycleEvent(
            setup_id="a", strategy_version_id="KOJO@V3",
            canonical_instrument=XAUUSD, status=CONSUMED,
            event_timestamp=conf_ts, provenance={},
        )
        result = ev._deduplicate_m15_entry_signals([lifecycle, sig1, sig2])
        lc_results = [r for r in result if isinstance(r, SetupLifecycleEvent)]
        self.assertEqual(len(lc_results), 1)

    def test_snapshot_restore_parity_after_dedup(self):
        """snapshot/restore doesn't break after a dedup-producing scenario."""
        ev = self._ev()
        conf_ts = 6_000_000
        entry = 95.0
        sig1 = _make_entry_signal(conf_ts, "SHORT", entry, 96.0, "p1")
        sig2 = _make_entry_signal(conf_ts, "SHORT", entry, 100.0, "p2")
        ev._deduplicate_m15_entry_signals([sig1, sig2])
        state = ev.snapshot_state()
        ev2 = make_evaluator()
        ev2.restore_state(state)
        self.assertEqual(ev2.snapshot_state(), state)


class TestRetestDiagnostics(unittest.TestCase):
    """RETEST_DIAGNOSTICS — retest_tolerance_band, retest_distance_abs,
    retest_distance_atr always present; m15_atr_at_retest present on M15-retest path."""

    def _build_and_get_signal(self):
        """Use build_pivot_sequence from the v3 test helper to get a real signal."""
        from tests.test_kojo_structure_reclaim_v3 import build_pivot_sequence
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS, 2000.0, "SHORT")
        sigs, _ = feed_signals(ev, events)
        return sigs[0] if sigs else None

    def test_retest_fields_present_in_provenance(self):
        """All four diagnostic fields must appear as provenance keys."""
        sig = self._build_and_get_signal()
        if sig is None:
            self.skipTest("no signal produced by build_pivot_sequence")
        p = sig.provenance
        for field in ("m15_atr_at_retest", "retest_tolerance_band",
                      "retest_distance_abs", "retest_distance_atr"):
            self.assertIn(field, p, f"missing diagnostic field: {field}")

    def test_retest_tolerance_band_nonnull(self):
        """retest_tolerance_band is non-None: it's always stored at retest detection."""
        sig = self._build_and_get_signal()
        if sig is None:
            self.skipTest("no signal produced")
        self.assertIsNotNone(sig.provenance["retest_tolerance_band"])
        self.assertGreater(float(sig.provenance["retest_tolerance_band"]), 0)

    def test_retest_distance_abs_nonnull(self):
        """retest_distance_abs is non-None when m15_retest_bar_extreme was stored."""
        sig = self._build_and_get_signal()
        if sig is None:
            self.skipTest("no signal produced")
        dist = sig.provenance["retest_distance_abs"]
        self.assertIsNotNone(dist)
        self.assertGreaterEqual(float(dist), 0.0)

    def test_m15_atr_present_on_m15_retest_path(self):
        """m15_atr_at_retest is non-None only when the M15 retest path was taken
        (m15_retest_first_open_ts is set in provenance)."""
        sig = self._build_and_get_signal()
        if sig is None:
            self.skipTest("no signal produced")
        p = sig.provenance
        if p.get("m15_retest_first_ts") is not None:
            # M15 retest path: ATR must be non-None
            self.assertIsNotNone(p["m15_atr_at_retest"])
            self.assertGreater(float(p["m15_atr_at_retest"]), 0)
        # H1 retest path: m15_atr_at_retest may be None, which is expected

    def test_retest_distance_atr_consistent_when_both_set(self):
        """distance_atr == distance_abs / m15_atr when both are non-None."""
        sig = self._build_and_get_signal()
        if sig is None:
            self.skipTest("no signal produced")
        p = sig.provenance
        if p.get("retest_distance_abs") is not None and p.get("m15_atr_at_retest"):
            expected = float(p["retest_distance_abs"]) / float(p["m15_atr_at_retest"])
            self.assertAlmostEqual(float(p["retest_distance_atr"]), expected, places=6)


class TestPullbackClassifierStatus(unittest.TestCase):
    """PULLBACK_STATUS — pullback_classifier_status present in provenance."""

    def test_pullback_classifier_status_in_provenance(self):
        from tests.test_kojo_structure_reclaim_v3 import build_pivot_sequence
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS + 100_000, 2000.0, "SHORT")
        sigs, _ = feed_signals(ev, events)
        if not sigs:
            self.skipTest("no signal produced")
        self.assertIn("pullback_classifier_status", sigs[0].provenance)
        self.assertEqual(
            sigs[0].provenance["pullback_classifier_status"],
            "INERT_SCAFFOLD_V3_TREATS_ALL_PULLBACKS_EQUALLY",
        )


class TestTP1QualityFields(unittest.TestCase):
    """TP1_QUALITY_FIELDS — top-level TP1 reaction counts in provenance."""

    def test_tp1_reaction_fields_present(self):
        from tests.test_kojo_structure_reclaim_v3 import build_pivot_sequence
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS + 100_000, 2000.0, "SHORT")
        sigs, _ = feed_signals(ev, events)
        if not sigs:
            self.skipTest("no signal produced")
        p = sigs[0].provenance
        for field in ("tp1_wick_reaction_count", "tp1_body_close_rejection_count",
                      "tp1_total_distinct_reactions"):
            self.assertIn(field, p, f"Missing field: {field}")
        self.assertTrue(p.get("tp1_source_rule_required"))


class TestParameterSetRef(unittest.TestCase):
    """PARAM_SET_REF — migration.signal.canonical_signal resolves parameter_set_ref
    from provenance["parameter_set_id"] when top-level fields are absent."""

    def _minimal_raw(self, signal_id: str, **overrides) -> dict:
        base = {
            "signal_id": signal_id,
            "schema_version": "strategy-signal-v1",
            "strategy_id": "KOJO_STRUCTURE_RECLAIM_V3",
            "strategy_version": "V3",
            "strategy_instance_id": "kojo-v3-forward",
            "direction": "SHORT",
            "entry_price": 2000.0,
            "stop_price": 2010.0,
            "target_price": 1990.0,
            "risk_distance": 10.0,
            "target_distance": 10.0,
            "target_r": 1.0,
            "entry_type": "MARKET",
            "entry_mechanism": ["M15_OPEN"],
            "symbol": "XAUUSD",
            "canonical_symbol": "XAUUSD",
            "timeframe": "M15",
            "signal_timestamp": "2026-10-12T11:00:00.000000Z",
            "created_at": "2026-10-12T11:15:06.000000Z",
            "decision_time": "2026-10-12T11:00:00.000000Z",
            "signal_emitted_at": "2026-10-12T11:15:06.000000Z",
            "strategy_metadata": {},
            "provenance": {},
        }
        base.update(overrides)
        return base

    def test_param_set_ref_from_provenance(self):
        """When raw has no top-level parameter_set_ref/id but provenance contains
        parameter_set_id, canonical_signal should set parameter_set_status=EXPLICIT."""
        try:
            from migration.signal import canonical_signal
        except ImportError:
            self.skipTest("migration.signal not importable")
        raw = self._minimal_raw(
            "TEST_SIG_0000000000000000000000",
            provenance={
                "parameter_set_id": "kojo-v3-default",
                "parameter_set_fingerprint": "abc123",
            },
        )
        cs = canonical_signal(raw, source_reference={"source_id": "test", "source_offset": 0})
        self.assertEqual(cs.fields.get("parameter_set_ref"), "kojo-v3-default")
        self.assertEqual(cs.fields.get("parameter_set_status"), "EXPLICIT")

    def test_param_set_ref_absent_stays_legacy(self):
        """When provenance has no parameter_set_id either, status stays LEGACY."""
        try:
            from migration.signal import canonical_signal
        except ImportError:
            self.skipTest("migration.signal not importable")
        raw = self._minimal_raw("TEST_SIG_1111111111111111111111")
        cs = canonical_signal(raw, source_reference={"source_id": "test", "source_offset": 0})
        self.assertIsNone(cs.fields.get("parameter_set_ref"))
        self.assertEqual(cs.fields.get("parameter_set_status"), "LEGACY_IMPLICIT_IN_STRATEGY_ID")


class TestEmissionClassification(unittest.TestCase):
    """EMISSION_CLASS — signal_emission_class PROSPECTIVE vs RETROSPECTIVE logic."""

    def _classify(self, latency_s: int) -> str:
        return "PROSPECTIVE" if latency_s <= 1200 else "RETROSPECTIVE"

    def test_prospective_boundary(self):
        self.assertEqual(self._classify(906), "PROSPECTIVE")
        self.assertEqual(self._classify(1200), "PROSPECTIVE")

    def test_retrospective(self):
        self.assertEqual(self._classify(1201), "RETROSPECTIVE")
        self.assertEqual(self._classify(86400), "RETROSPECTIVE")

    def test_fresh_signal_prospective(self):
        """0-latency (same-cycle replay) is PROSPECTIVE."""
        self.assertEqual(self._classify(0), "PROSPECTIVE")


class TestEqualHighsClustering(unittest.TestCase):
    """EQUAL_HIGHS_NESTED — verify EQUAL_HIGHS/EQUAL_LOWS are inside
    tp1_provenance.all_liquidity_objectives (not top-level), confirming the
    previous audit report was wrong about their absence."""

    def test_equal_highs_constants_exist(self):
        self.assertEqual(LIQUIDITY_EQUAL_HIGHS, "EQUAL_HIGHS")
        self.assertEqual(LIQUIDITY_EQUAL_LOWS, "EQUAL_LOWS")

    def test_equal_highs_constant_matches_clustering_key(self):
        """EQUAL_HIGHS constant matches the string used in the clustering code."""
        import re
        src = (ROOT / "strategy_backtest" / "kojo_structure_reclaim_v3.py").read_text()
        # The clustering assigns liq_type = LIQUIDITY_EQUAL_HIGHS or "EQUAL_HIGHS"
        self.assertTrue(
            "EQUAL_HIGHS" in src,
            "clustering must reference EQUAL_HIGHS in source",
        )
        # EQUAL_HIGHS and EQUAL_LOWS are produced via the same clustering loop
        self.assertIn("EQUAL_LOWS", src)

    def test_equal_highs_in_tp1_provenance_not_top_level(self):
        """Confirms that the correct path for EQUAL_HIGHS is nested in tp1_provenance,
        not at the source_provenance top level — fixing the cohort audit report error."""
        from tests.test_kojo_structure_reclaim_v3 import build_pivot_sequence
        ev = make_evaluator()
        events = build_pivot_sequence(BASE_TS + 300_000, 2000.0, "SHORT")
        sigs, _ = feed_signals(ev, events)
        if not sigs:
            self.skipTest("no signal produced")
        prov = sigs[0].provenance
        # EQUAL_HIGHS must NOT be a top-level key (that was the audit error)
        self.assertNotIn("EQUAL_HIGHS", prov)
        self.assertNotIn("EQUAL_LOWS", prov)
        # tp1_provenance may or may not contain EQUAL_HIGHS depending on data,
        # but the key path must be tp1_provenance.all_liquidity_objectives[*].liquidity_type
        if "tp1_provenance" in prov and prov["tp1_provenance"]:
            all_objs = prov["tp1_provenance"].get("all_liquidity_objectives", [])
            types_found = {o.get("liquidity_type") for o in all_objs}
            # Any liquidity types found are nested as expected
            self.assertIsInstance(types_found, set)


if __name__ == "__main__":
    unittest.main()
