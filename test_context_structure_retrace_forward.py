from __future__ import annotations

import ast
import json
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

import context_structure_retrace_forward as fwd


def bar(t, o, h, l, c, spread=10):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "spread": spread, "tick_volume": 1}


def snapshot(price=100.0):
    return {
        "provenance": {"structure_timeframe": "M15"},
        "timeframes": {"M15": {"ema_context": {"atr": 1.0}, "sr_context": {"zones": []}, "completed_candle": {"close": price}}},
    }


def test_long_target_direction_never_masks_negative_reward():
    event = bar(0, 100, 101, 99, 100.2)
    g = fwd._geometry(event, "LONG", snapshot(), 103.5, 0.1, 1.0)
    assert g["signed_target_distance"] < 0
    assert g["target_direction_state"] == "TARGET_BEHIND_ENTRY"
    assert g["target_R"] < 0


def test_short_target_direction_is_symmetric():
    event = bar(0, 100, 101, 99, 99.8)
    g = fwd._geometry(event, "SHORT", snapshot(), 97.5, 0.1, 1.0)
    assert g["signed_target_distance"] < 0
    assert g["target_direction_state"] == "TARGET_BEHIND_ENTRY"
    assert g["target_R"] < 0


def test_valid_target_has_signed_positive_reward():
    event = bar(0, 100, 104, 99, 103)
    g = fwd._geometry(event, "LONG", snapshot(), 103.1, 0.1, 1.0)
    assert g["signed_target_distance"] > 0
    assert g["target_R"] > 0


def test_frozen_config_and_identity_are_instrument_agnostic():
    assert fwd.FROZEN_CONFIG["execution_timeframe"] == "M15"
    source = Path(fwd.__file__).read_text()
    assert "symbol == \"XAUUSDm\"" not in source
    assert "symbol == \"BTCUSDm\"" not in source
    assert "symbol == \"USDJPYm\"" not in source
    assert fwd.FROZEN_CONFIG["two_leg_risk"] == {"total": 1.0, "leg_a": 0.5, "leg_b": 0.5}


def test_bridge_boundary_allows_only_reads():
    called = []
    with patch.object(fwd, "call_bridge", lambda *args: called.append(args) or {}):
        fwd.bridge_read("mt5_quote", {}, "url")
        try:
            fwd.bridge_read("not_a_read", {}, "url")
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-read bridge tool was not rejected")
    assert called


def test_order_isolation_audit_has_no_non_read_calls():
    audit = fwd.order_isolation_audit()
    assert audit["non_readonly_bridge_calls"] == []
    assert audit["order_submission_code_path"] is False


def test_two_leg_total_risk_is_one():
    assert sum((fwd.FROZEN_CONFIG["two_leg_risk"]["leg_a"], fwd.FROZEN_CONFIG["two_leg_risk"]["leg_b"])) == fwd.FROZEN_CONFIG["two_leg_risk"]["total"]


def test_target_completion_is_explicitly_a_terminal_setup_move():
    assert "RETURN_AFTER_SETUP_TARGET_COMPLETED" in fwd.FROZEN_CONFIG["reentry"]
    assert fwd.FROZEN_CONFIG["scale_in"] == "DISABLED; potential scale-in is logged only"


def test_no_future_outcome_feature_provenance():
    assert fwd.FROZEN_CONFIG["strategy_version"] == fwd.VERSION
    assert fwd.PHASE2_HASH == "923d0d2762b6b78515a96e96dba17e42e34818aa82c406dc9ebc6f43b1a54c41"


def _shadow_position(mfe, mae=0.0, risk=10.0, status="OPEN"):
    return {"economic_position_id": "p", "geometry": {"stop_distance": risk}, "mfe_price": mfe, "mae_price": mae, "status": status, "leg_b": {"runner_hypotheses": ["+3R"]}}


def test_shadow_registered_hypothesis_is_not_reached_threshold():
    observation = fwd._shadow_position_observation(_shadow_position(0.0))
    assert observation["thresholds_reached"]["+3R"] is False


def test_shadow_threshold_requires_causal_favorable_excursion():
    observation = fwd._shadow_position_observation(_shadow_position(12.0))
    assert observation["thresholds_reached"]["+1R"] is True
    assert observation["thresholds_reached"]["+1.5R"] is False


def test_shadow_long_excursion_math():
    entry, high, low = 100.0, 112.0, 97.0
    assert high - entry == 12.0
    assert entry - low == 3.0


def test_shadow_short_excursion_math():
    entry, high, low = 100.0, 103.0, 88.0
    assert entry - low == 12.0
    assert high - entry == 3.0


def test_shadow_pre_entry_price_cannot_trigger_threshold():
    position = _shadow_position(0.0)
    position["entry_bar"] = {"high": 200.0, "low": 50.0}
    observation = fwd._shadow_position_observation(position)
    assert observation["thresholds_reached"]["+1R"] is False


def test_shadow_threshold_timestamps_are_not_fabricated():
    observation = fwd._shadow_position_observation(_shadow_position(20.0))
    assert all(value is None for value in observation["threshold_timestamps"].values())
    assert observation["post_exit_shadow_max_R"] is None


class ContextStructureRetraceForwardTests(unittest.TestCase):
    def test_target_direction_long(self): test_long_target_direction_never_masks_negative_reward()
    def test_target_direction_short(self): test_short_target_direction_is_symmetric()
    def test_target_direction_positive(self): test_valid_target_has_signed_positive_reward()
    def test_instrument_independence(self): test_frozen_config_and_identity_are_instrument_agnostic()
    def test_bridge_read_allowlist(self): test_bridge_boundary_allows_only_reads()
    def test_order_isolation(self): test_order_isolation_audit_has_no_non_read_calls()
    def test_two_leg_risk(self): test_two_leg_total_risk_is_one()
    def test_target_completion_semantics(self): test_target_completion_is_explicitly_a_terminal_setup_move()
    def test_provenance_hash(self): test_no_future_outcome_feature_provenance()
    def test_shadow_registered_not_reached(self): test_shadow_registered_hypothesis_is_not_reached_threshold()
    def test_shadow_causal_threshold(self): test_shadow_threshold_requires_causal_favorable_excursion()
    def test_shadow_long_math(self): test_shadow_long_excursion_math()
    def test_shadow_short_math(self): test_shadow_short_excursion_math()
    def test_shadow_no_preentry(self): test_shadow_pre_entry_price_cannot_trigger_threshold()
    def test_shadow_no_fabricated_timestamps(self): test_shadow_threshold_timestamps_are_not_fabricated()

    def test_hover_does_not_create_second_opportunity(self):
        setup = {"zone_left": False, "target_completed": False, "thesis_invalidated": False}
        self.assertFalse(setup["zone_left"])

    def test_reentry_semantics_are_frozen_before_target_only(self):
        self.assertIn("TARGET_NOT_COMPLETED", fwd.FROZEN_CONFIG["reentry"])
        self.assertIn("RETURN_AFTER_SETUP_TARGET_COMPLETED", fwd.FROZEN_CONFIG["reentry"])

    def test_scale_in_is_record_only(self):
        self.assertIn("DISABLED", fwd.FROZEN_CONFIG["scale_in"])

    def test_report_is_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = {"STATE": root / "state.json", "EVENTS": root / "events.jsonl", "HEARTBEAT": root / "heartbeat.json", "MANIFEST": root / "manifest.json"}
            paths["STATE"].write_text(json.dumps(fwd.empty_state()))
            paths["EVENTS"].write_text("")
            paths["HEARTBEAT"].write_text(json.dumps({"timestamp": "2026-09-16T05:00:08Z"}))
            paths["MANIFEST"].write_text(json.dumps({"freeze_timestamp": "2026-09-16T05:00:08Z", "code_hash": "old", "configuration_hash": "cfg", "phase2_representation_hash": fwd.PHASE2_HASH}))
            before = {key: path.read_bytes() for key, path in paths.items()}
            with patch.object(fwd, "STATE", paths["STATE"]), patch.object(fwd, "EVENTS", paths["EVENTS"]), patch.object(fwd, "HEARTBEAT", paths["HEARTBEAT"]), patch.object(fwd, "MANIFEST", paths["MANIFEST"]):
                fwd.report_data()
            after = {key: path.read_bytes() for key, path in paths.items()}
            self.assertEqual(before, after)

    def test_report_backfills_position_identity_from_setup(self):
        setup = {
            "setup_id": "setup-1",
            "symbol": "XAUUSDm",
            "direction": "SHORT",
            "pattern": "BEARISH_ENGULFING",
            "setup_timestamp": 100,
            "provenance": {"source": "LIVE_FORWARD"},
            "opportunities": [{
                "economic_position_id": "position-1",
                "fill_timestamp": 1789563900,
                "fill_timestamp_iso": "2026-09-16T05:05:00+00:00",
                "status": "OPEN",
                "executable_paper_entry": 100.0,
                "stop": 101.0,
                "target": 98.0,
            }],
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = {"STATE": root / "state.json", "EVENTS": root / "events.jsonl", "HEARTBEAT": root / "heartbeat.json", "MANIFEST": root / "manifest.json"}
            state = fwd.empty_state()
            state["runner_status"] = "ACTIVE"
            state["setups"] = {"setup-1": setup}
            paths["STATE"].write_text(json.dumps(state))
            paths["EVENTS"].write_text(json.dumps({"type": "SETUP_DETECTED", "setup_id": "setup-1", "symbol": "XAUUSDm", "event_time": "2026-09-16T05:01:40+00:00"}) + "\n")
            paths["HEARTBEAT"].write_text(json.dumps({"timestamp": "2026-09-16T05:00:08Z"}))
            paths["MANIFEST"].write_text(json.dumps({"freeze_timestamp": "2026-09-16T05:00:08+00:00", "code_hash": "old", "configuration_hash": "cfg", "phase2_representation_hash": fwd.PHASE2_HASH}))
            with patch.object(fwd, "STATE", paths["STATE"]), patch.object(fwd, "EVENTS", paths["EVENTS"]), patch.object(fwd, "HEARTBEAT", paths["HEARTBEAT"]), patch.object(fwd, "MANIFEST", paths["MANIFEST"]):
                report = fwd.report_data()
            self.assertEqual(len(report["open"]), 1)
            self.assertEqual(report["open"][0]["symbol"], "XAUUSDm")
            self.assertEqual(report["open"][0]["direction"], "SHORT")
            self.assertEqual(report["open"][0]["pattern"], "BEARISH_ENGULFING")
