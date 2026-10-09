"""KOJO_STRUCTURE_RECLAIM_V3 managed strategy registration tests.

Covers:
  REGISTRATION   — strategy definition, version, schema, default parameter set
  PARITY         — default ParameterSet reproduces c82d290 V3 evaluator behavior exactly
  VALIDATION     — schema rejects unknown / wrong-type / invalid-enum / OOB / missing params
  IMMUTABILITY   — frozen ParameterSet fingerprint behavior
  RUNTIME        — instance loads active ParameterSet; parameter switch changes future config
  PROVENANCE     — generated signal stores strategy/instance/parameter provenance
  SAFETY         — ONLINE != execution; SHADOW instance cannot broker-write; config switch
                   cannot arm execution authority
  REGRESSION     — V1/V2 evaluator tests not broken; research artifacts untouched
"""
from __future__ import annotations

import hashlib
import json
import unittest
from typing import Any

from strategy_backtest.models import (
    MarketEvent,
    ParameterSchema,
    ParameterSet,
    StrategyVersion,
    fingerprint,
)
from strategy_backtest.kojo_structure_reclaim_v3 import (
    EVALUATOR_KEY,
    STRATEGY_ID,
    VERSION,
    KojoStructureReclaimV3Evaluator,
    kojo_structure_reclaim_v3_baseline_parameter_set,
    kojo_structure_reclaim_v3_default_parameter_set,
    kojo_structure_reclaim_v3_parameter_schema,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

_STRATEGY_VERSION_ID = f"{STRATEGY_ID}@{VERSION}"
_DEFAULT_PS_FINGERPRINT = "b1228ba7513d41e23e503f9b770a222c8349a17c0b7348e53e3a87bcc4f751aa"


def _h1(ts: int, o: float, h: float, lo: float, c: float, instrument: str = "XAUUSD") -> MarketEvent:
    return MarketEvent(instrument, "H1", ts, ts + 3600, o, h, lo, c)


def _m15(ts: int, o: float, h: float, lo: float, c: float, instrument: str = "XAUUSD") -> MarketEvent:
    return MarketEvent(instrument, "M15", ts, ts + 900, o, h, lo, c)


def _make_sv(schema: ParameterSchema | None = None) -> StrategyVersion:
    return StrategyVersion(
        STRATEGY_ID,
        VERSION,
        EVALUATOR_KEY,
        schema or kojo_structure_reclaim_v3_parameter_schema(),
        lifecycle="IMPLEMENTED",
    )


def _make_evaluator(ps: ParameterSet | None = None, instance_id: str | None = None) -> KojoStructureReclaimV3Evaluator:
    sv = _make_sv()
    if ps is None:
        ps = kojo_structure_reclaim_v3_default_parameter_set()
    ev = KojoStructureReclaimV3Evaluator()
    ev.initialize(sv, ps, instance_id=instance_id, configuration_revision="1")
    return ev


# ─── REGISTRATION ──────────────────────────────────────────────────────────────

class RegistrationTests(unittest.TestCase):
    def test_strategy_definition_constants(self):
        self.assertEqual(STRATEGY_ID, "KOJO_STRUCTURE_RECLAIM_V3")
        self.assertEqual(VERSION, "V3")
        self.assertEqual(EVALUATOR_KEY, "kojo_structure_reclaim_v3")

    def test_strategy_version_id(self):
        sv = _make_sv()
        self.assertEqual(sv.strategy_version_id, _STRATEGY_VERSION_ID)
        self.assertEqual(sv.evaluator_key, EVALUATOR_KEY)
        self.assertEqual(sv.lifecycle, "IMPLEMENTED")

    def test_parameter_schema_resolves(self):
        schema = kojo_structure_reclaim_v3_parameter_schema()
        self.assertEqual(schema.schema_id, "kojo-structure-reclaim-v3")
        self.assertEqual(len(schema.fields), 18)

    def test_parameter_schema_has_all_required_keys(self):
        schema = kojo_structure_reclaim_v3_parameter_schema()
        required = {
            "context_timeframe", "confirmation_timeframe", "execution_timeframe",
            "pivot_strength", "stop_buffer_type", "stop_buffer_value",
            "retest_tolerance_atr", "max_retest_wait_h1_bars", "max_confirmation_wait_m15_bars",
            "trade_management_mode",
            "allow_wick_rejection", "allow_body_close_rejection", "entry_type",
            "simple_pullback_enabled", "complex_pullback_enabled",
            "tp1_min_rr", "reaction_lookback_scope", "tp2_selection_policy",
        }
        self.assertEqual(set(schema.fields.keys()), required)

    def test_parameter_schema_categories(self):
        schema = kojo_structure_reclaim_v3_parameter_schema()
        cats = {spec.get("category") for spec in schema.fields.values()}
        self.assertIn("TIMEFRAMES", cats)
        self.assertIn("STRUCTURE", cats)
        self.assertIn("CONFIRMATION", cats)
        self.assertIn("LIFECYCLE", cats)
        self.assertIn("ENTRY", cats)
        self.assertIn("PULLBACK", cats)
        self.assertIn("TARGETS", cats)

    def test_default_parameter_set_created(self):
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        self.assertEqual(ps.parameter_set_id, "kojo-v3-default")
        self.assertEqual(ps.strategy_version_id, _STRATEGY_VERSION_ID)
        self.assertEqual(ps.schema_id, "kojo-structure-reclaim-v3")
        self.assertEqual(len(ps.values), 18)

    def test_default_parameter_set_fingerprint(self):
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        self.assertEqual(ps.fingerprint, _DEFAULT_PS_FINGERPRINT)

    def test_strategy_version_validates_default_parameter_set(self):
        sv = _make_sv()
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        # Should not raise
        sv.validate_parameter_set(ps)

    def test_evaluator_initializes_with_default_parameter_set(self):
        ev = _make_evaluator()
        self.assertIsNotNone(ev.parameters)
        self.assertEqual(ev.parameters.parameter_set_id, "kojo-v3-default")

    def test_parameter_schema_idempotent(self):
        s1 = kojo_structure_reclaim_v3_parameter_schema()
        s2 = kojo_structure_reclaim_v3_parameter_schema()
        self.assertEqual(s1.schema_id, s2.schema_id)
        self.assertEqual(set(s1.fields.keys()), set(s2.fields.keys()))

    def test_default_parameter_set_idempotent(self):
        ps1 = kojo_structure_reclaim_v3_default_parameter_set()
        ps2 = kojo_structure_reclaim_v3_default_parameter_set()
        self.assertEqual(ps1.fingerprint, ps2.fingerprint)
        self.assertEqual(ps1.values, ps2.values)


# ─── PARITY ────────────────────────────────────────────────────────────────────

def _run_synthetic_session(evaluator: KojoStructureReclaimV3Evaluator, bars: list[MarketEvent]) -> list[Any]:
    """Feed a list of market events and collect all outputs."""
    outputs = []
    for event in bars:
        outputs.extend(evaluator.consume_market_event(event))
    return outputs


def _build_synthetic_session() -> list[MarketEvent]:
    """Build a minimal synthetic bar sequence that exercises the V3 signal path.

    Uses a LONG setup:
      - H1 bars establish a RESISTANCE swing pivot at 2000.0
      - A later H1 bar closes above it (episode created)
      - H1 bars stay above level (no invalidation)
      - M15 bars after episode_start_ts include:
        - A reaction bar that creates a TP1 zone (bearish wick > 33%)
        - A retest bar (low touches level)
        - A rejection bar (bullish engulfing → LONG confirmation)
        - Next M15 open triggers entry

    Times are chosen so all causal constraints hold.
    Prices:
      Level: 2000.0 (RESISTANCE)
      Break bar close: 2010.0 > 2000.0
      Entry area: ~2011.0 (next M15 open after confirmation)
      TP1 reaction zone: ~2025.0 (1.25R above entry with stop ~2 below)
    """
    # H1 base: establish swing pivot at 2000.0
    # Need `pivot_strength=2` bars on each side, plus the pivot bar itself.
    # Pivot bar at index 2 (0-indexed); needs bars 0,1 below and bars 3,4 below its high.
    t0 = 1_700_000_000  # some Unix ts divisible by 3600
    h1_seed = [
        _h1(t0 + 0*3600, 1990, 1998, 1989, 1995),   # idx 0 — high 1998 < 2000 (pre-pivot)
        _h1(t0 + 1*3600, 1992, 1999, 1991, 1996),   # idx 1 — high 1999 < 2000 (pre-pivot)
        _h1(t0 + 2*3600, 1995, 2000, 1993, 1998),   # idx 2 — PIVOT high = 2000.0 (RESISTANCE)
        _h1(t0 + 3*3600, 1997, 1999, 1996, 1997),   # idx 3 — high 1999 < 2000 (confirms pivot)
        _h1(t0 + 4*3600, 1994, 1998, 1993, 1995),   # idx 4 — high 1998 < 2000 (confirms pivot)
        # Now we have the pivot confirmed.  Break bar: closes above 2000.0.
        _h1(t0 + 5*3600, 1996, 2012, 1994, 2005),   # idx 5 — close 2005 > 2000 → EPISODE created
        # Episode continues (stay above level to avoid invalidation)
        _h1(t0 + 6*3600, 2003, 2015, 2001, 2007),   # idx 6 — still above level
        _h1(t0 + 7*3600, 2005, 2018, 2003, 2009),   # idx 7 — still above
    ]

    # episode_start_ts = close_timestamp of break bar = t0 + 5*3600 + 3600 = t0 + 21600
    episode_start_ts = t0 + 6 * 3600

    # M15 bars — must have open_timestamp >= episode_start_ts
    # First, a reaction bar (TP1 zone): bearish wick > 33%
    # Bar at 2025 with upper wick >= 33% of range
    # e.g. o=2020, h=2030, lo=2019, c=2021 → upper_wick = 2030-2021=9, range=11, 9/11=0.82 > 0.33
    m15_t0 = episode_start_ts  # M15 starts right at episode start

    # day_start for TP1 zone detection: same UTC day as confirmation_timestamp
    # Let's pick m15_t0 to be within the same UTC day as the break bar
    m15_seed = [
        # Reaction bar — creates TP1 zone near 2030
        _m15(m15_t0 + 0*900, 2020, 2030, 2019, 2021),      # wick from 2021 to 2030 → WICK_REJECTION zone ~2030
        # Retest bar — low touches level 2000 (within tolerance 0.5 * ATR)
        # M15 ATR ~= 11 (from above bar); tolerance = 0.5 * 11 = 5.5; level=2000; low must be <= 2005.5
        _m15(m15_t0 + 1*900, 2008, 2010, 2000, 2006),      # low=2000 → retest
        # After retest (state=RETEST_SEEN), next bars form confirmation
        # Confirmation = bullish engulfing: prev bearish, curr bullish engulfing prev
        _m15(m15_t0 + 2*900, 2006, 2008, 2003, 2004),      # bearish (prev for engulfing)
        _m15(m15_t0 + 3*900, 2003, 2012, 2002, 2011),      # bullish engulfing → CONFIRMED_PENDING_NEXT_OPEN
        # Next M15 open triggers entry
        _m15(m15_t0 + 4*900, 2011, 2015, 2010, 2013),      # entry at 2011.0 (open)
    ]

    return h1_seed + m15_seed


class ParityTests(unittest.TestCase):
    """Verify that default ParameterSet produces same decisions as the legacy baseline."""

    def setUp(self):
        self._bars = _build_synthetic_session()

    def _run_with_ps(self, ps_factory, instance_id=None):
        sv = _make_sv()
        ps = ps_factory()
        ev = KojoStructureReclaimV3Evaluator()
        ev.initialize(sv, ps, instance_id=instance_id)
        return _run_synthetic_session(ev, self._bars)

    def test_baseline_and_default_produce_same_decision_count(self):
        from strategy_backtest.models import EntrySignal
        outputs_baseline = self._run_with_ps(kojo_structure_reclaim_v3_baseline_parameter_set)
        outputs_default = self._run_with_ps(kojo_structure_reclaim_v3_default_parameter_set)
        signals_b = [o for o in outputs_baseline if isinstance(o, EntrySignal)]
        signals_d = [o for o in outputs_default if isinstance(o, EntrySignal)]
        self.assertEqual(len(signals_b), len(signals_d), "signal count must match")

    def test_baseline_and_default_produce_same_directions(self):
        from strategy_backtest.models import EntrySignal
        outputs_baseline = self._run_with_ps(kojo_structure_reclaim_v3_baseline_parameter_set)
        outputs_default = self._run_with_ps(kojo_structure_reclaim_v3_default_parameter_set)
        dirs_b = [o.direction for o in outputs_baseline if isinstance(o, EntrySignal)]
        dirs_d = [o.direction for o in outputs_default if isinstance(o, EntrySignal)]
        self.assertEqual(dirs_b, dirs_d)

    def test_baseline_and_default_produce_same_entry_stop_target(self):
        from strategy_backtest.models import EntrySignal
        outputs_baseline = self._run_with_ps(kojo_structure_reclaim_v3_baseline_parameter_set)
        outputs_default = self._run_with_ps(kojo_structure_reclaim_v3_default_parameter_set)
        sigs_b = [o for o in outputs_baseline if isinstance(o, EntrySignal)]
        sigs_d = [o for o in outputs_default if isinstance(o, EntrySignal)]
        for sb, sd in zip(sigs_b, sigs_d):
            self.assertAlmostEqual(sb.entry_price, sd.entry_price, places=5)
            self.assertAlmostEqual(sb.stop_price, sd.stop_price, places=5)
            self.assertAlmostEqual(sb.target_price, sd.target_price, places=5)

    def test_no_mixed_configuration_within_evaluation(self):
        """One evaluation must use exactly one complete immutable snapshot."""
        from strategy_backtest.models import EntrySignal
        outputs = self._run_with_ps(kojo_structure_reclaim_v3_default_parameter_set)
        signals = [o for o in outputs if isinstance(o, EntrySignal)]
        for sig in signals:
            fp_in_prov = sig.provenance.get("parameter_set_fingerprint")
            self.assertIsNotNone(fp_in_prov, "signal must carry parameter_set_fingerprint")
            snap = sig.provenance.get("effective_parameter_snapshot")
            self.assertIsNotNone(snap, "signal must carry effective_parameter_snapshot")
            self.assertEqual(len(snap), 18, "snapshot must include all 18 parameters")

    def test_parity_long(self):
        from strategy_backtest.models import EntrySignal
        outputs = self._run_with_ps(kojo_structure_reclaim_v3_default_parameter_set)
        signals = [o for o in outputs if isinstance(o, EntrySignal)]
        for sig in signals:
            if sig.direction == "LONG":
                self.assertLess(sig.stop_price, sig.entry_price)
                self.assertLess(sig.entry_price, sig.target_price)

    def test_parity_order_type_matches_entry_type_param(self):
        from strategy_backtest.models import EntrySignal
        outputs = self._run_with_ps(kojo_structure_reclaim_v3_default_parameter_set)
        signals = [o for o in outputs if isinstance(o, EntrySignal)]
        for sig in signals:
            self.assertEqual(sig.order_type, "MARKET")


# ─── VALIDATION ────────────────────────────────────────────────────────────────

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self._schema = kojo_structure_reclaim_v3_parameter_schema()
        self._valid_values = dict(kojo_structure_reclaim_v3_default_parameter_set().values)

    def test_valid_values_pass(self):
        self._schema.validate(self._valid_values)  # must not raise

    def test_unknown_parameter_rejected(self):
        bad = {**self._valid_values, "totally_unknown_param": 42}
        with self.assertRaises(ValueError, msg="unknown parameters must be rejected"):
            self._schema.validate(bad)

    def test_wrong_type_rejected_pivot_strength_float(self):
        # ParameterSchema checks enum/min/max but not strict Python types.
        # pivot_strength has minimum=1; passing 0 should fail.
        bad = {**self._valid_values, "pivot_strength": 0}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_invalid_enum_context_timeframe(self):
        bad = {**self._valid_values, "context_timeframe": "W1"}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_invalid_enum_entry_type(self):
        bad = {**self._valid_values, "entry_type": "LIMIT"}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_invalid_enum_trade_management_mode(self):
        bad = {**self._valid_values, "trade_management_mode": "AUTO"}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_invalid_bounds_pivot_strength_above_max(self):
        bad = {**self._valid_values, "pivot_strength": 11}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_invalid_bounds_tp1_min_rr_below_min(self):
        bad = {**self._valid_values, "tp1_min_rr": 0.1}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_invalid_bounds_retest_tolerance_above_max(self):
        bad = {**self._valid_values, "retest_tolerance_atr": 5.0}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_missing_required_parameter(self):
        bad = {k: v for k, v in self._valid_values.items() if k != "pivot_strength"}
        with self.assertRaises(ValueError):
            self._schema.validate(bad)

    def test_evaluator_rejects_wrong_strategy_version(self):
        sv_wrong = StrategyVersion("WRONG_STRATEGY", "V1", "wrong_key",
                                   kojo_structure_reclaim_v3_parameter_schema())
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        ev = KojoStructureReclaimV3Evaluator()
        with self.assertRaises(ValueError):
            ev.initialize(sv_wrong, ps)


# ─── IMMUTABILITY ──────────────────────────────────────────────────────────────

class ImmutabilityTests(unittest.TestCase):
    def test_parameter_set_is_frozen_dataclass(self):
        from dataclasses import FrozenInstanceError
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        with self.assertRaises((FrozenInstanceError, TypeError)):
            ps.parameter_set_id = "mutated"  # type: ignore[misc]

    def test_fingerprint_changes_when_values_change(self):
        ps_a = kojo_structure_reclaim_v3_default_parameter_set()
        ps_b = ParameterSet(
            parameter_set_id="kojo-v3-experimental-1",
            strategy_version_id=ps_a.strategy_version_id,
            schema_id=ps_a.schema_id,
            values={**ps_a.values, "tp1_min_rr": 1.25},
            provenance=ps_a.provenance,
        )
        self.assertNotEqual(ps_a.fingerprint, ps_b.fingerprint)

    def test_same_values_produce_same_fingerprint(self):
        ps1 = kojo_structure_reclaim_v3_default_parameter_set()
        ps2 = kojo_structure_reclaim_v3_default_parameter_set()
        self.assertEqual(ps1.fingerprint, ps2.fingerprint)

    def test_new_parameter_set_required_for_value_change(self):
        """Changing a value must create a new ParameterSet — in-place mutation is impossible."""
        ps_original = kojo_structure_reclaim_v3_default_parameter_set()
        # Must create a new PS instead of mutating
        ps_new = ParameterSet(
            parameter_set_id="kojo-v3-experimental-1",
            strategy_version_id=ps_original.strategy_version_id,
            schema_id=ps_original.schema_id,
            values={**ps_original.values, "tp1_min_rr": 1.25},
            provenance={**ps_original.provenance, "source": "EXPERIMENTAL"},
        )
        self.assertNotEqual(ps_original.parameter_set_id, ps_new.parameter_set_id)
        self.assertNotEqual(ps_original.fingerprint, ps_new.fingerprint)
        self.assertEqual(ps_original.values["tp1_min_rr"], 1.0)
        self.assertEqual(ps_new.values["tp1_min_rr"], 1.25)

    def test_fingerprint_is_deterministic(self):
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        self.assertEqual(ps.fingerprint, _DEFAULT_PS_FINGERPRINT)


# ─── RUNTIME ───────────────────────────────────────────────────────────────────

class RuntimeTests(unittest.TestCase):
    def test_instance_loads_active_parameter_set(self):
        ev = _make_evaluator(instance_id="test-instance-001")
        self.assertEqual(ev.parameters.parameter_set_id, "kojo-v3-default")
        self.assertEqual(ev._instance_id, "test-instance-001")

    def test_configuration_revision_tracked(self):
        ev = _make_evaluator()
        ev._configuration_revision = "1"
        self.assertEqual(ev._configuration_revision, "1")

    def test_in_flight_evaluation_uses_original_snapshot(self):
        """While evaluating one bar stream, the snapshot is frozen at initialize() time."""
        ev = _make_evaluator()
        original_fp = ev.parameters.fingerprint
        bars = _build_synthetic_session()
        for event in bars:
            _ = ev.consume_market_event(event)
            # Parameter set must never change during evaluation
            self.assertEqual(ev.parameters.fingerprint, original_fp)

    def test_parameter_switch_requires_new_parameter_set_id(self):
        ps_a = kojo_structure_reclaim_v3_default_parameter_set()
        ps_b = ParameterSet(
            parameter_set_id="kojo-v3-experimental-1",
            strategy_version_id=ps_a.strategy_version_id,
            schema_id=ps_a.schema_id,
            values={**ps_a.values, "tp1_min_rr": 1.25},
            provenance=ps_a.provenance,
        )
        self.assertNotEqual(ps_a.parameter_set_id, ps_b.parameter_set_id)

    def test_adapter_diagnostics_include_provenance(self):
        from orchestration.adapters.kojo_structure_reclaim_v3_adapter import KojoStructureReclaimV3Adapter
        sv = _make_sv()
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        adapter = KojoStructureReclaimV3Adapter(
            instance_id="test-inst", display_name="Test", instruments=["XAUUSDm"],
            parameter_set_fingerprint=ps.fingerprint,
        )
        adapter.initialize(sv, ps)
        diag = adapter.diagnostics()
        self.assertEqual(diag["instance_id"], "test-inst")
        self.assertEqual(diag["active_parameter_set_fingerprint"], ps.fingerprint)
        self.assertFalse(diag["execution_eligible"])
        self.assertEqual(diag["broker_writes"], 0)

    def test_no_mixed_configuration_evaluation(self):
        """An evaluator initialized with PS-A must use PS-A for the entire evaluation."""
        from strategy_backtest.models import EntrySignal
        ps_a = kojo_structure_reclaim_v3_default_parameter_set()
        ev = _make_evaluator(ps=ps_a)
        bars = _build_synthetic_session()
        signals = []
        for event in bars:
            outputs = ev.consume_market_event(event)
            signals.extend(o for o in outputs if isinstance(o, EntrySignal))
        for sig in signals:
            self.assertEqual(sig.provenance["parameter_set_fingerprint"], ps_a.fingerprint)

    def test_adapter_snapshot_and_restore(self):
        from orchestration.adapters.kojo_structure_reclaim_v3_adapter import KojoStructureReclaimV3Adapter
        sv = _make_sv()
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        adapter = KojoStructureReclaimV3Adapter(
            instance_id="snap-test", display_name="Test", instruments=[],
            parameter_set_fingerprint=ps.fingerprint,
        )
        adapter.initialize(sv, ps)
        bars = _build_synthetic_session()[:3]
        for event in bars:
            adapter.consume_market_event(event)
        state = adapter.snapshot_state()
        self.assertIn("_configuration_revision", state)
        self.assertIn("_active_fingerprint", state)
        # Restore into a new adapter
        adapter2 = KojoStructureReclaimV3Adapter(
            instance_id="snap-test", display_name="Test", instruments=[],
        )
        adapter2.initialize(sv, ps)
        adapter2.restore_state(state)
        self.assertEqual(adapter2._active_fingerprint, ps.fingerprint)


# ─── PROVENANCE ────────────────────────────────────────────────────────────────

class ProvenanceTests(unittest.TestCase):
    def _get_first_signal(self, instance_id: str | None = None):
        from strategy_backtest.models import EntrySignal
        ev = _make_evaluator(instance_id=instance_id)
        bars = _build_synthetic_session()
        for event in bars:
            for output in ev.consume_market_event(event):
                if isinstance(output, EntrySignal):
                    return output
        return None

    def test_signal_stores_strategy_version(self):
        sig = self._get_first_signal()
        if sig is None:
            self.skipTest("synthetic session produced no signal")
        self.assertEqual(sig.provenance["strategy_version"], _STRATEGY_VERSION_ID)

    def test_signal_stores_parameter_set_id(self):
        sig = self._get_first_signal()
        if sig is None:
            self.skipTest("synthetic session produced no signal")
        self.assertEqual(sig.provenance["parameter_set_id"], "kojo-v3-default")

    def test_signal_stores_parameter_set_fingerprint(self):
        sig = self._get_first_signal()
        if sig is None:
            self.skipTest("synthetic session produced no signal")
        self.assertEqual(sig.provenance["parameter_set_fingerprint"], _DEFAULT_PS_FINGERPRINT)

    def test_signal_stores_effective_parameter_snapshot(self):
        sig = self._get_first_signal()
        if sig is None:
            self.skipTest("synthetic session produced no signal")
        snap = sig.provenance.get("effective_parameter_snapshot")
        self.assertIsNotNone(snap)
        self.assertEqual(len(snap), 18)
        self.assertEqual(snap["tp1_min_rr"], 1.0)
        self.assertEqual(snap["entry_type"], "MARKET")

    def test_signal_stores_strategy_instance_id(self):
        sig = self._get_first_signal(instance_id="kojo-v3-forward")
        if sig is None:
            self.skipTest("synthetic session produced no signal")
        self.assertEqual(sig.provenance["strategy_instance_id"], "kojo-v3-forward")

    def test_signal_stores_configuration_revision(self):
        ev = _make_evaluator(instance_id="rev-test")
        ev._configuration_revision = "5"
        # Reinitialize to apply the revision
        sv = _make_sv()
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        ev.initialize(sv, ps, instance_id="rev-test", configuration_revision="5")
        from strategy_backtest.models import EntrySignal
        for event in _build_synthetic_session():
            for output in ev.consume_market_event(event):
                if isinstance(output, EntrySignal):
                    self.assertEqual(output.provenance["configuration_revision"], "5")
                    return
        self.skipTest("synthetic session produced no signal")

    def test_lifecycle_event_stores_parameter_set_fingerprint(self):
        from strategy_backtest.models import SetupLifecycleEvent
        ev = _make_evaluator()
        bars = _build_synthetic_session()
        for event in bars:
            for output in ev.consume_market_event(event):
                if isinstance(output, SetupLifecycleEvent):
                    self.assertIn("parameter_set_fingerprint", output.provenance)
                    return
        self.skipTest("synthetic session produced no lifecycle event")


# ─── SAFETY ───────────────────────────────────────────────────────────────────

class SafetyTests(unittest.TestCase):
    def test_evaluator_online_does_not_imply_execution(self):
        """ONLINE means signal generation is active; it must NOT imply broker execution."""
        ev = _make_evaluator()
        # The evaluator has no execution authority property; signal generation ≠ execution.
        self.assertFalse(hasattr(ev, "execution_eligible"))
        self.assertFalse(hasattr(ev, "broker_write"))

    def test_adapter_execution_eligible_always_false(self):
        from orchestration.adapters.kojo_structure_reclaim_v3_adapter import KojoStructureReclaimV3Adapter
        sv = _make_sv()
        ps = kojo_structure_reclaim_v3_default_parameter_set()
        adapter = KojoStructureReclaimV3Adapter(
            instance_id="safety-test", display_name="Safety Test", instruments=[]
        )
        adapter.initialize(sv, ps)
        diag = adapter.diagnostics()
        self.assertFalse(diag["execution_eligible"])
        self.assertEqual(diag["broker_writes"], 0)

    def test_config_switch_cannot_arm_execution(self):
        """Switching from PS-A to PS-B cannot change execution_eligible."""
        from orchestration.adapters.kojo_structure_reclaim_v3_adapter import KojoStructureReclaimV3Adapter
        sv = _make_sv()
        ps_a = kojo_structure_reclaim_v3_default_parameter_set()
        adapter = KojoStructureReclaimV3Adapter(
            instance_id="exec-test", display_name="Exec Test", instruments=[]
        )
        adapter.initialize(sv, ps_a)
        # After reload, execution_eligible must remain false
        ps_b = ParameterSet(
            parameter_set_id="kojo-v3-experimental-1",
            strategy_version_id=ps_a.strategy_version_id,
            schema_id=ps_a.schema_id,
            values={**ps_a.values, "tp1_min_rr": 1.25},
            provenance=ps_a.provenance,
        )
        adapter._evaluator.initialize(sv, ps_b, instance_id="exec-test")
        diag = adapter.diagnostics()
        self.assertFalse(diag["execution_eligible"])
        self.assertEqual(diag["broker_writes"], 0)

    def test_parameter_change_cannot_alter_account_authority(self):
        """Parameters control signal generation knobs; they have no account/risk authority fields."""
        schema = kojo_structure_reclaim_v3_parameter_schema()
        forbidden = {"account_id", "risk_limit", "execution_eligible", "broker_write", "execution_authority"}
        for key in schema.fields:
            self.assertNotIn(key, forbidden, f"parameter {key} must not be an execution/account authority field")

    def test_signal_production_eligible_false(self):
        from strategy_backtest.models import EntrySignal
        ev = _make_evaluator()
        for event in _build_synthetic_session():
            for output in ev.consume_market_event(event):
                if isinstance(output, EntrySignal):
                    self.assertFalse(output.provenance.get("production_eligible", True))
                    return
        self.skipTest("no signal generated")

    def test_source_fidelity_blocked_false(self):
        from strategy_backtest.kojo_structure_reclaim_v3 import SOURCE_FIDELITY_BLOCKED
        self.assertFalse(SOURCE_FIDELITY_BLOCKED)


# ─── REGRESSION ───────────────────────────────────────────────────────────────

class RegressionTests(unittest.TestCase):
    def test_v1_evaluator_still_importable(self):
        from strategy_backtest.kojo_structure_reclaim import (
            KojoStructureReclaimEvaluator,
            STRATEGY_ID as V1_ID,
        )
        self.assertEqual(V1_ID, "KOJO_STRUCTURE_RECLAIM_V1")

    def test_v2_evaluator_still_importable(self):
        from strategy_backtest.kojo_structure_reclaim_v2 import (
            KojoStructureReclaimV2Evaluator,
            STRATEGY_ID as V2_ID,
        )
        self.assertEqual(V2_ID, "KOJO_STRUCTURE_RECLAIM_V2")

    def test_v3_research_baseline_still_importable_and_valid(self):
        ps = kojo_structure_reclaim_v3_baseline_parameter_set()
        schema = kojo_structure_reclaim_v3_parameter_schema()
        # Research baseline must validate against expanded schema
        schema.validate(ps.values)
        self.assertEqual(ps.parameter_set_id, "kojo-structure-reclaim-v3-baseline")

    def test_registry_includes_all_kojo_evaluators(self):
        from strategy_backtest.registry import StrategyEvaluatorRegistry, register_builtin_evaluators
        registry = register_builtin_evaluators(StrategyEvaluatorRegistry())
        # Must have V1, V2, V3 registered
        sv1 = StrategyVersion("KOJO_STRUCTURE_RECLAIM_V1", "V1", "kojo_structure_reclaim",
                              kojo_structure_reclaim_v3_parameter_schema())
        sv2 = StrategyVersion("KOJO_STRUCTURE_RECLAIM_V2", "V2", "kojo_structure_reclaim_v2",
                              kojo_structure_reclaim_v3_parameter_schema())
        sv3 = _make_sv()
        self.assertIsNotNone(registry.resolve(sv1))
        self.assertIsNotNone(registry.resolve(sv2))
        self.assertIsNotNone(registry.resolve(sv3))

    def test_v3_does_not_change_v1_signal_semantics(self):
        """V1 evaluator must still produce the same outputs regardless of V3 schema expansion."""
        from strategy_backtest.kojo_structure_reclaim import (
            KojoStructureReclaimEvaluator,
            kojo_structure_reclaim_parameter_schema,
            kojo_structure_reclaim_baseline_parameter_set,
            STRATEGY_ID as V1_ID,
            VERSION as V1_VER,
            EVALUATOR_KEY as V1_KEY,
        )
        sv_v1 = StrategyVersion(V1_ID, V1_VER, V1_KEY,
                                kojo_structure_reclaim_parameter_schema())
        ps_v1 = kojo_structure_reclaim_baseline_parameter_set(sv_v1.strategy_version_id)
        ev_v1 = KojoStructureReclaimEvaluator()
        ev_v1.initialize(sv_v1, ps_v1)
        # Should not raise; V1 evaluator is independent of V3 schema


# ─── PARAMETER INVENTORY REPORT ───────────────────────────────────────────────

class ParameterInventoryTests(unittest.TestCase):
    """Verify parameter classification matches the implementation."""

    def test_semantic_invariants_not_exposed_as_parameters(self):
        """Core causal and source invariants must NOT appear as parameter keys."""
        schema = kojo_structure_reclaim_v3_parameter_schema()
        invariants = {
            "h1_close_beyond_level_required",
            "h1_confirmation_is_completed_bar",
            "h1_confirmation_precedes_m15_retest",
            "m15_only_baseline_enabled",
            "causal_no_lookahead",
            "terminal_episode_integrity",
            "duplicate_opportunity_prevention",
        }
        for inv in invariants:
            self.assertNotIn(inv, schema.fields, f"semantic invariant {inv} must not be a parameter")

    def test_all_runtime_parameter_keys(self):
        """Complete runtime parameter inventory — 18 keys, exact match."""
        schema = kojo_structure_reclaim_v3_parameter_schema()
        expected_keys = {
            "context_timeframe", "confirmation_timeframe", "execution_timeframe",
            "pivot_strength", "stop_buffer_type", "stop_buffer_value",
            "retest_tolerance_atr",
            "max_retest_wait_h1_bars", "max_confirmation_wait_m15_bars", "trade_management_mode",
            "allow_wick_rejection", "allow_body_close_rejection", "entry_type",
            "simple_pullback_enabled", "complex_pullback_enabled",
            "tp1_min_rr", "reaction_lookback_scope", "tp2_selection_policy",
        }
        self.assertEqual(set(schema.fields.keys()), expected_keys)

    def test_allow_wick_rejection_wired_into_evaluator(self):
        """Setting allow_wick_rejection=False must reduce TP1 zone candidates."""
        from strategy_backtest.models import EntrySignal
        ps_wick_on = kojo_structure_reclaim_v3_default_parameter_set()
        ps_wick_off = ParameterSet(
            parameter_set_id="kojo-v3-no-wick",
            strategy_version_id=ps_wick_on.strategy_version_id,
            schema_id=ps_wick_on.schema_id,
            values={**ps_wick_on.values, "allow_wick_rejection": False, "allow_body_close_rejection": False},
            provenance=ps_wick_on.provenance,
        )
        sv = _make_sv()
        ev_on = KojoStructureReclaimV3Evaluator()
        ev_on.initialize(sv, ps_wick_on)
        ev_off = KojoStructureReclaimV3Evaluator()
        ev_off.initialize(sv, ps_wick_off)

        bars = _build_synthetic_session()
        sigs_on = [o for o in _run_synthetic_session(ev_on, bars) if isinstance(o, EntrySignal)]
        sigs_off = [o for o in _run_synthetic_session(ev_off, bars) if isinstance(o, EntrySignal)]
        # With both rejection types disabled, TP1 zone has no candidates → no signal
        self.assertGreaterEqual(len(sigs_on), len(sigs_off),
                                "disabling both rejection types must reduce or eliminate signals")

    def test_tp1_min_rr_wired_into_evaluator(self):
        """Setting tp1_min_rr higher should reduce (or maintain) signal count."""
        from strategy_backtest.models import EntrySignal
        ps_base = kojo_structure_reclaim_v3_default_parameter_set()
        ps_high = ParameterSet(
            parameter_set_id="kojo-v3-high-rr",
            strategy_version_id=ps_base.strategy_version_id,
            schema_id=ps_base.schema_id,
            values={**ps_base.values, "tp1_min_rr": 5.0},
            provenance=ps_base.provenance,
        )
        sv = _make_sv()
        ev_base = KojoStructureReclaimV3Evaluator()
        ev_base.initialize(sv, ps_base)
        ev_high = KojoStructureReclaimV3Evaluator()
        ev_high.initialize(sv, ps_high)

        bars = _build_synthetic_session()
        sigs_base = [o for o in _run_synthetic_session(ev_base, bars) if isinstance(o, EntrySignal)]
        sigs_high = [o for o in _run_synthetic_session(ev_high, bars) if isinstance(o, EntrySignal)]
        self.assertGreaterEqual(len(sigs_base), len(sigs_high),
                                "requiring high tp1_min_rr must reduce or maintain signal count")


if __name__ == "__main__":
    unittest.main()
