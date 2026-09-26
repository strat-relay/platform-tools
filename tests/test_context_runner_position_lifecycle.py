"""Context runner: position exit evaluation is independent of setup lifecycle.

Regression for OPEN positions orphaned once their setup left FILLED (INVALIDATED_NO_REENTRY when
price crosses the event-bar extreme but not the buffered stop) or was no longer retained in
compact state. The exit rule itself (bar high/low vs frozen stop/target, stop precedence,
realized R) is unchanged; only which OPEN positions receive it changed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import context_structure_retrace_forward as fwd  # noqa: E402
from context_structure_retrace_compact_state import project_state  # noqa: E402
from context_structure_retrace_outcome_projector import project_entry_only_outcomes  # noqa: E402
from test_context_entry_outcome_projection import CUTOFF, FakeDB  # noqa: E402

T0 = 1790000000            # setup (M15 event bar) time
FILL = T0 + 900            # fill bar
EVENT_LOW, EVENT_HIGH = 1.1000, 1.1020
ENTRY, STOP, TARGET = 1.1010, 1.0995, 1.1030   # LONG; stop buffered below the event-bar low
TARGET_R = (TARGET - ENTRY) / (ENTRY - STOP)


def position(pid="POS1", *, with_identity=True, **overrides):
    p = {"entry_opportunity_id": f"OP-{pid}", "entry_attempt_id": f"AT-{pid}", "economic_position_id": pid,
         "fill_timestamp": FILL, "fill_timestamp_iso": fwd.iso(FILL), "fill_candle_number": 0,
         "entry_mechanisms": ["DEPTH_ONLY"], "theoretical_entry": ENTRY, "executable_paper_entry": ENTRY,
         "spread_at_fill": 0.0001, "stop": STOP, "target": TARGET,
         "geometry": {"stop_distance": ENTRY - STOP, "target_R": TARGET_R},
         "leg_a": {"allocation_R": 0.5, "status": "OPEN"},
         "leg_b": {"allocation_R": 0.5, "status": "OPEN"},
         "status": "OPEN", "mfe_price": 0.0, "mae_price": 0.0, "reentry_type": "INITIAL"}
    if with_identity:  # what _fill now stamps at fill time
        p.update({"symbol": "EURUSDm", "direction": "LONG", "setup_id": "SETUP1"})
    p.update(overrides)
    return p


def state_with_filled_setup(**pos_kw):
    pos = position(**pos_kw)
    setup = {"setup_id": "SETUP1", "market_event_id": "ME1", "symbol": "EURUSDm", "direction": "LONG",
             "pattern": "BULLISH_ENGULFING", "setup_timestamp": T0, "status": "FILLED", "retrace_state": "FILLED",
             "event_bar": {"time": T0, "open": 1.1005, "high": EVENT_HIGH, "low": EVENT_LOW, "close": 1.1018},
             "entry_level": ENTRY, "spread_at_detection": 0.0001, "provenance": {"source": "LIVE_FORWARD"},
             "opportunities": [pos]}
    state = fwd.empty_state()
    state["setups"]["SETUP1"] = setup
    state["positions"][pos["economic_position_id"]] = pos
    return state


def bar(t, low, high, close):
    return {"time": t, "open": close, "high": high, "low": low, "close": close}


def reload(state):
    """Checkpoint + restart: the exact compact JSON round trip production performs."""
    return json.loads(json.dumps(project_state(state), default=str))


class RunnerSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [patch.object(fwd, name, root / f"{name.lower()}.json")
                        for name in ("STATE", "EVENTS", "HEARTBEAT")]
        for p in self.patches:
            p.start()
        fwd._EVENT_IDENTITY_CACHE_PATH = None
        fwd._EVENT_IDENTITIES = set()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def process(self, state, *bars):
        for b in bars:
            fwd._process_bar(state, "EURUSDm", b, 0, [b], {}, {}, "LIVE_FORWARD")

    def invalidate_without_stop(self, state):
        # Low crosses the event-bar extreme (1.1000) but not the buffered stop (1.0995).
        self.process(state, bar(FILL + 300, 1.0998, 1.1012, 1.1004))
        setup = state["setups"]["SETUP1"]
        self.assertEqual(setup["status"], "INVALIDATED_NO_REENTRY")
        return setup

    def events(self):
        path = fwd.EVENTS
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class PositionLifecycleTests(RunnerSandbox):
    def test_filled_setup_exits_still_evaluate(self):  # 1, 5, 9
        state = state_with_filled_setup()
        self.process(state, bar(FILL + 300, 1.1008, 1.1031, 1.1025))
        pos = state["setups"]["SETUP1"]["opportunities"][0]
        self.assertEqual((pos["status"], pos["realized_R"]), ("TARGET_HIT", TARGET_R))
        self.assertTrue(state["setups"]["SETUP1"]["target_completed"])

    def test_invalidated_setup_keeps_open_position_evaluated_then_stopped(self):  # 2, 3, 4, 9
        state = state_with_filled_setup()
        setup = self.invalidate_without_stop(state)
        pos = setup["opportunities"][0]
        self.assertEqual(pos["status"], "OPEN")
        self.process(state, bar(FILL + 600, 1.0999, 1.1008, 1.1001))       # still above stop
        self.assertEqual(pos["status"], "OPEN")
        self.assertAlmostEqual(pos["mae_price"], ENTRY - 1.0998)           # excursion keeps updating
        self.process(state, bar(FILL + 900, 1.0990, 1.1003, 1.0992))       # stop hit
        self.assertEqual((pos["status"], pos["exit_timestamp"], pos["realized_R"]), ("STOPPED", FILL + 900, -1.0))
        [stopped] = [e for e in self.events() if e["type"] == "STOPPED"]
        self.assertEqual((stopped["setup_id"], stopped["economic_position_id"]), ("SETUP1", "POS1"))

    def test_invalidated_setup_position_can_still_hit_target(self):  # 5
        state = state_with_filled_setup()
        setup = self.invalidate_without_stop(state)
        self.process(state, bar(FILL + 600, 1.1001, 1.1032, 1.1030))
        pos = setup["opportunities"][0]
        self.assertEqual((pos["status"], pos["realized_R"]), ("TARGET_HIT", TARGET_R))

    def test_same_bar_stop_and_target_keeps_stop_precedence(self):  # 6
        for invalidated in (False, True):
            with self.subTest(invalidated=invalidated):
                state = state_with_filled_setup()
                if invalidated:
                    self.invalidate_without_stop(state)
                self.process(state, bar(FILL + 900, 1.0990, 1.1035, 1.1010))
                pos = state["setups"]["SETUP1"]["opportunities"][0]
                self.assertEqual((pos["status"], pos["realized_R"]), ("STOPPED", -1.0))

    def test_invalidation_still_prevents_reentry(self):  # 7
        state = state_with_filled_setup()
        setup = self.invalidate_without_stop(state)
        before = len(self.events())   # the invalidating bar itself runs the unchanged FILLED-branch zone logic
        self.process(state, bar(FILL + 600, 1.1004, 1.1016, 1.1015),       # leaves zone
                     bar(FILL + 900, 1.1007, 1.1012, 1.1010))               # returns into zone
        self.assertEqual(len(setup["opportunities"]), 1)
        self.assertEqual(setup["status"], "INVALIDATED_NO_REENTRY")
        after = [e["type"] for e in self.events()[before:]]
        self.assertFalse({"FILLED", "POTENTIAL_SCALE_IN", "LEAVE_ENTRY_ZONE"} & set(after), after)

    def test_no_double_evaluation_in_the_invalidating_bar(self):
        state = state_with_filled_setup()
        self.invalidate_without_stop(state)
        self.assertEqual([e["type"] for e in self.events()].count("STOPPED"), 0)
        self.assertAlmostEqual(state["setups"]["SETUP1"]["opportunities"][0]["mae_price"], ENTRY - 1.0998)

    def test_bar_at_or_before_fill_is_not_evaluated(self):
        state = state_with_filled_setup()
        self.invalidate_without_stop(state)
        pos = state["setups"]["SETUP1"]["opportunities"][0]
        self.process(state, bar(FILL, 1.0900, 1.1100, 1.1000))
        self.assertEqual(pos["status"], "OPEN")


class RestartAndCompactionTests(RunnerSandbox):
    def test_after_restart_the_setup_held_position_is_evaluated_and_projected(self):  # 2, 10
        state = state_with_filled_setup()
        self.invalidate_without_stop(state)
        state = reload(state)  # production: setup-held and positions-map records are now distinct objects
        self.assertIsNot(state["setups"]["SETUP1"]["opportunities"][0], state["positions"]["POS1"])
        self.process(state, bar(FILL + 600, 1.0990, 1.1003, 1.0992))
        self.assertEqual(state["setups"]["SETUP1"]["opportunities"][0]["status"], "STOPPED")
        self.assertEqual(state["positions"]["POS1"]["status"], "OPEN")   # stale in-memory map ...
        captured = {}
        with patch.dict("os.environ", {"ENTRY_OUTCOME_SIGNAL_CUTOFF_ID": CUTOFF}), \
                patch("context_structure_retrace_outcome_projector.project_entry_only_outcomes",
                      side_effect=lambda s: captured.setdefault("state", s) and {}):
            fwd._project_entry_only_outcomes(state)
        self.assertEqual(captured["state"]["positions"]["POS1"]["status"], "STOPPED")  # ... projector sees authority

    def test_compacted_away_setup_position_keeps_evaluating_from_frozen_position_state(self):  # 8
        state = state_with_filled_setup()
        state = reload(state)
        del state["setups"]["SETUP1"]                  # setup no longer retained
        pos = state["positions"]["POS1"]
        self.process(state, bar(FILL + 300, 1.1008, 1.1031, 1.1025))
        self.assertEqual((pos["status"], pos["realized_R"]), ("TARGET_HIT", TARGET_R))
        self.assertEqual(reload(state)["positions"]["POS1"]["status"], "TARGET_HIT")  # survives checkpoint

    def test_legacy_setupless_position_without_frozen_direction_is_not_guessed(self):  # 8 (legacy)
        state = fwd.empty_state()
        state["positions"]["LEGACY"] = position("LEGACY", with_identity=False)
        self.process(state, bar(FILL + 300, 1.0900, 1.1100, 1.1000))
        self.assertEqual(state["positions"]["LEGACY"]["status"], "OPEN")


class CanonicalOutcomeTests(RunnerSandbox):
    def test_corrected_exit_reaches_canonical_outcome_unchanged_projector(self):  # 4, 10
        state = state_with_filled_setup()
        self.invalidate_without_stop(state)
        state = reload(state)
        self.process(state, bar(FILL + 600, 1.0990, 1.1003, 1.0992))
        db = FakeDB()
        db.signals = [("SIG1", "CONTEXT_STRUCTURE_RETRACE_V1", "POS1", "OP-POS1", CUTOFF)]
        result = project_entry_only_outcomes(project_state(state), connect_fn=db.connect,
                                             environ={"ENTRY_OUTCOME_SIGNAL_CUTOFF_ID": CUTOFF},
                                             clock=lambda: datetime(2026, 9, 26, tzinfo=timezone.utc))
        self.assertEqual(result["projected"], 1)
        self.assertEqual(db.outcomes["SIG1"][:4], ("ENTRY_ONLY", "STOPPED", -1.0,
                                                   datetime.fromtimestamp(FILL + 600, timezone.utc)))


class IdentityAndBoundaryTests(unittest.TestCase):
    def test_rules_config_unchanged_and_new_fingerprint_pinned(self):
        self.assertEqual(fwd.config_hash(), "1f1da2a63d69ac79e4aca21d0de33c860e76f4c33d9bd321cb50b20353114e1e")
        self.assertEqual(fwd.decision_code_hash(), fwd.FROZEN_DECISION_CODE_HASH)
        self.assertIn("70dba71d28fe8a5c09f9033b80eeb4c27a733c6c342537e03c631f41e2a1cdda", fwd.PRIOR_DECISION_CODE_HASHES)
        self.assertIn("_evaluate_open_position", fwd.DECISION_FUNCTION_NAMES)  # exit rule stays fingerprinted
        for path in ("orchestration/adapters/context_structure_retrace.py", "signal_orchestrator.py"):
            text = (ROOT / path).read_text()
            self.assertIn(fwd.FROZEN_DECISION_CODE_HASH, text)
            self.assertNotIn("70dba71d28fe8a5c09f9033b80eeb4c27a733c6c342537e03c631f41e2a1cdda", text)

    def test_runner_stays_read_only(self):  # 12
        self.assertTrue(all(name.startswith("mt5_") and not any(w in name for w in ("order", "close", "trailing"))
                            for name in fwd.READ_ONLY_BRIDGE_TOOLS))


if __name__ == "__main__":
    unittest.main()
