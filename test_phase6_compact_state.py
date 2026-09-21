from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import context_structure_retrace_forward as fwd
from context_structure_retrace_compact_state import (
    COMPACT_SCHEMA_VERSION,
    project_state,
    validate_equivalence,
    write_compact,
)


def _bar(t, o, h, l, c, spread=1):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "spread": spread, "tick_volume": 1}


def _snapshot():
    return {
        "provenance": {"structure_timeframe": "M15"},
        "timeframes": {
            "M15": {"ema_context": {"atr": 1.0}, "sr_context": {"zones": []}},
        },
    }


def _fixture():
    state = fwd.empty_state()
    state["symbols"] = {"XAUUSDm": {"last_m5": 100, "last_m15": 90, "initialized": True, "last_candle": "1970-01-01T00:01:40+00:00"}}
    setup = {
        "setup_id": "setup-1", "market_event_id": "event-1", "symbol": "XAUUSDm",
        "direction": "LONG", "pattern": "BULLISH_ENGULFING", "setup_timestamp": 50,
        "setup_timestamp_iso": "1970-01-01T00:00:50+00:00",
        "qualification": "QUALIFIED_FOR_RETRACE_MONITORING", "qualification_flags": [],
        "retrace_state": "FILLED", "entry_level": 100.0, "theoretical_entry": 100.0,
        "spread_at_detection": 1.0, "target_completed": False, "status": "FILLED",
        "m5_start_index": 1, "zone_left": False, "thesis_invalidated": False,
        "event_bar": _bar(50, 99, 101, 98, 100),
        "provenance": {"source": "LIVE_FORWARD", "phase2_representation_hash": fwd.PHASE2_HASH},
        "context_snapshot": _snapshot(),
        "opportunities": [{
            "entry_opportunity_id": "opp-1", "entry_attempt_id": "attempt-1",
            "economic_position_id": "position-1", "fill_timestamp": 80,
            "fill_timestamp_iso": "1970-01-01T00:01:20+00:00", "fill_candle_number": 2,
            "entry_mechanisms": ["DEPTH_ONLY"], "theoretical_entry": 100.0,
            "executable_paper_entry": 100.5, "spread_at_fill": 1.0,
            "stop": 98.0, "target": 102.0, "status": "OPEN", "mfe_price": 0.0,
            "mae_price": 0.0, "reentry_type": "INITIAL", "symbol": "XAUUSDm",
            "direction": "LONG", "setup_id": "setup-1", "pattern": "BULLISH_ENGULFING",
            "geometry": {"stop_distance": 2.5, "target_R": 0.6},
            "leg_a": {"allocation_R": 0.5, "status": "OPEN"},
            "leg_b": {"allocation_R": 0.5, "breakeven_activated": False},
        }],
    }
    state["setups"] = {"setup-1": setup}
    state["positions"] = {"position-1": copy.deepcopy(setup["opportunities"][0])}
    state["counters"] = {"events": 1, "setups": 1, "opportunities": 1, "positions": 1}
    state["prospective_boundary"] = "1970-01-01T00:00:00+00:00"
    state["kill_switch"] = "OFF"
    return state


def test_projection_schema_and_excludes_heavy_fields():
    compact = project_state(_fixture())
    assert compact["schema"] == COMPACT_SCHEMA_VERSION
    setup = compact["setups"]["setup-1"]
    assert "context_components" not in setup
    assert "geometry_at_reference" not in setup
    assert setup["context_snapshot"]["timeframes"]["M15"]["ema_context"]["atr"] == 1.0
    assert setup["opportunities"][0]["economic_position_id"] == "position-1"


def test_identifier_and_lifecycle_equivalence():
    source = _fixture()
    compact = project_state(source)
    result = validate_equivalence(source, compact, {"strategy_version": source["strategy_version"]})
    assert result["all_equal"] is True


def test_continuation_target_transition_is_identical():
    source = _fixture()
    compact = project_state(source)
    source_next = copy.deepcopy(source)
    compact_next = copy.deepcopy(compact)
    bars = {"M5": [_bar(105, 100, 103, 100, 102)], "M15": [_bar(105, 100, 103, 100, 102)]}
    contract = {"point": 1.0}
    quote = {"bid": 102.0, "ask": 102.0}
    with patch.object(fwd, "append_event", lambda event, state: None):
        fwd.process_symbol(source_next, "XAUUSDm", contract, quote, bars)
        fwd.process_symbol(compact_next, "XAUUSDm", contract, quote, bars)
    assert source_next["setups"]["setup-1"]["status"] == compact_next["setups"]["setup-1"]["status"]
    assert source_next["setups"]["setup-1"]["target_completed"] == compact_next["setups"]["setup-1"]["target_completed"]
    source_position = source_next["setups"]["setup-1"]["opportunities"][0]
    compact_position = compact_next["setups"]["setup-1"]["opportunities"][0]
    assert source_position["status"] == compact_position["status"]
    assert source_position.get("realized_R") == compact_position.get("realized_R")
    assert source_next["symbols"] == compact_next["symbols"]


def _run_pair(source, compact, bars):
    contract = {"point": 1.0}
    quote = {"bid": 100.0, "ask": 100.0}
    with patch.object(fwd, "append_event", lambda event, state: None):
        fwd.process_symbol(source, "XAUUSDm", contract, quote, bars)
        fwd.process_symbol(compact, "XAUUSDm", contract, quote, bars)


def test_continuation_invalidation_is_identical():
    source = _fixture()
    source["setups"]["setup-1"]["status"] = "WAITING_FOR_RETRACE"
    source["setups"]["setup-1"]["retrace_state"] = "WAITING_FOR_RETRACE"
    source["setups"]["setup-1"]["opportunities"] = []
    source["positions"] = {}
    source["symbols"]["XAUUSDm"].update({"last_m5": 100, "last_m15": 100})
    compact = project_state(source)
    bars = {"M5": [_bar(105, 99, 100, 97, 99)], "M15": [_bar(105, 99, 100, 97, 99)]}
    _run_pair(source, compact, bars)
    assert source["setups"]["setup-1"]["status"] == compact["setups"]["setup-1"]["status"] == "INVALIDATED_NO_REENTRY"
    assert source["setups"]["setup-1"]["thesis_invalidated"] == compact["setups"]["setup-1"]["thesis_invalidated"]


def test_continuation_entry_creation_is_identical():
    source = _fixture()
    source["setups"]["setup-1"]["status"] = "WAITING_FOR_RETRACE"
    source["setups"]["setup-1"]["retrace_state"] = "WAITING_FOR_RETRACE"
    source["setups"]["setup-1"]["opportunities"] = []
    source["positions"] = {}
    source["symbols"]["XAUUSDm"].update({"last_m5": 100, "last_m15": 100})
    compact = project_state(source)
    bars = {"M5": [_bar(105, 99.9, 100.5, 99.5, 100.2)], "M15": [_bar(105, 99.9, 100.5, 99.5, 100.2)]}
    _run_pair(source, compact, bars)
    source_opp = source["setups"]["setup-1"]["opportunities"]
    compact_opp = compact["setups"]["setup-1"]["opportunities"]
    assert [x["entry_opportunity_id"] for x in source_opp] == [x["entry_opportunity_id"] for x in compact_opp]
    assert [x["economic_position_id"] for x in source_opp] == [x["economic_position_id"] for x in compact_opp]
    assert source["setups"]["setup-1"]["status"] == compact["setups"]["setup-1"]["status"] == "FILLED"


def test_continuation_reentry_creation_is_identical():
    source = _fixture()
    source["setups"]["setup-1"]["target_completed"] = False
    source["setups"]["setup-1"]["zone_left"] = True
    source["setups"]["setup-1"]["opportunities"][0]["status"] = "STOPPED"
    source["setups"]["setup-1"]["opportunities"][0]["realized_R"] = -1.0
    source["positions"]["position-1"]["status"] = "STOPPED"
    source["positions"]["position-1"]["realized_R"] = -1.0
    source["symbols"]["XAUUSDm"].update({"last_m5": 100, "last_m15": 100})
    compact = project_state(source)
    bars = {"M5": [_bar(105, 100.0, 100.2, 99.8, 100.0)], "M15": [_bar(105, 100.0, 100.2, 99.8, 100.0)]}
    _run_pair(source, compact, bars)
    source_opp = source["setups"]["setup-1"]["opportunities"]
    compact_opp = compact["setups"]["setup-1"]["opportunities"]
    assert len(source_opp) == len(compact_opp) == 2
    assert source_opp[-1]["reentry_type"] == compact_opp[-1]["reentry_type"] == "REENTRY_BEFORE_TARGET_COMPLETION"


def test_continuation_stop_and_realized_r_are_identical():
    source = _fixture()
    source["setups"]["setup-1"]["opportunities"][0]["stop"] = 99.0
    source["setups"]["setup-1"]["opportunities"][0]["target"] = 999.0
    source["positions"]["position-1"]["stop"] = 99.0
    source["positions"]["position-1"]["target"] = 999.0
    source["symbols"]["XAUUSDm"].update({"last_m5": 100, "last_m15": 100})
    compact = project_state(source)
    bars = {"M5": [_bar(105, 100.0, 100.2, 98.0, 98.5)], "M15": [_bar(105, 100.0, 100.2, 98.0, 98.5)]}
    _run_pair(source, compact, bars)
    source_position = source["setups"]["setup-1"]["opportunities"][0]
    compact_position = compact["setups"]["setup-1"]["opportunities"][0]
    assert source_position["status"] == compact_position["status"] == "STOPPED"
    assert source_position["realized_R"] == compact_position["realized_R"] == -1.0


def test_projection_does_not_write_source():
    source = _fixture()
    before = json.dumps(source, sort_keys=True)
    project_state(source)
    assert json.dumps(source, sort_keys=True) == before


def test_checkpoint_preserves_live_references():
    state = _fixture()
    symbol_ref = state["symbols"]["XAUUSDm"]
    setup_ref = state["setups"]["setup-1"]
    position_ref = setup_ref["opportunities"][0]
    with patch.object(fwd, "atomic_json"):
        fwd.save_state(state)
    symbol_ref["last_m5"] = 200
    setup_ref["zone_left"] = True
    position_ref["mfe_price"] = 3.0
    assert state["symbols"]["XAUUSDm"]["last_m5"] == 200
    assert state["setups"]["setup-1"]["zone_left"] is True
    assert state["setups"]["setup-1"]["opportunities"][0]["mfe_price"] == 3.0


def test_cursor_advances_across_checkpoints_without_false_gap():
    state = fwd.empty_state()
    state["symbols"] = {"XAUUSDm": {"last_m5": 100, "last_m15": 100, "initialized": True,
                                     "last_candle": "1970-01-01T00:01:40+00:00"}}
    bars = {"M5": [_bar(200, 100, 101, 99, 100)], "M15": [_bar(200, 100, 101, 99, 100)]}
    with patch.object(fwd, "atomic_json"):
        fwd.process_symbol(state, "XAUUSDm", {"point": 1.0}, {"bid": 100.0, "ask": 100.0}, bars)
        assert state["symbols"]["XAUUSDm"]["last_m5"] == 200
        fwd.process_symbol(state, "XAUUSDm", {"point": 1.0}, {"bid": 100.0, "ask": 100.0}, bars)
    assert state["symbols"]["XAUUSDm"]["last_m5"] == 200


def test_real_gap_still_enters_gap_detection_after_reference_fix():
    state = fwd.empty_state()
    state["symbols"] = {"XAUUSDm": {"last_m5": 100, "last_m15": 100, "initialized": True,
                                     "last_candle": "1970-01-01T00:01:40+00:00"}}
    bars = {"M5": [_bar(1000, 100, 101, 99, 100)], "M15": [_bar(1000, 100, 101, 99, 100)]}
    events = []
    with patch.object(fwd, "atomic_json"), patch.object(fwd, "append_event", lambda event, state: events.append(event)):
        fwd.process_symbol(state, "XAUUSDm", {"point": 1.0}, {"bid": 100.0, "ask": 100.0}, bars)
    assert any(event.get("type") == "DATA_GAP_DETECTED" for event in events)


def test_projection_is_idempotent():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source_path = root / "source.json"
        first = root / "first.json"
        second = root / "second.json"
        source_path.write_text(json.dumps(_fixture(), sort_keys=True))
        write_compact(source_path, first)
        write_compact(source_path, second)
        assert first.read_bytes() == second.read_bytes()
