from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import context_structure_retrace_forward as forward
from context_structure_retrace_compact_state import project_state
from postgres.working_set import build_working_set, make_boundary, validate_working_set


def _state():
    return {"schema": "v1", "strategy_version": "S", "last_successful_read_at": "2026-09-17T10:00:00Z",
            "symbols": {"XAUUSDm": {"last_m5": 10}}, "setups": {
                "active": {"setup_id": "active", "status": "FILLED", "context_snapshot": {"schema": "compact"}, "opportunities": [
                    {"entry_opportunity_id": "opp", "economic_position_id": "pos", "status": "OPEN"}]},
                "terminal": {"setup_id": "terminal", "status": "INVALIDATED_NO_REENTRY", "opportunities": [
                    {"entry_opportunity_id": "old", "economic_position_id": "old-pos", "status": "TARGET_HIT"}]},
            }, "positions": {"pos": {"status": "OPEN"}, "old-pos": {"status": "OPEN"}}}


def test_working_set_excludes_terminal_history_and_keeps_context():
    selected = build_working_set(_state())
    assert set(selected["setups"]) == {"active"}
    assert len(selected["opportunities"]) == 1
    assert len(selected["positions"]) == 1
    assert len(selected["context_references"]) == 1


def test_working_set_preflight_reports_archived_terminal_drift_without_blocking_active():
    result = validate_working_set(_state(), [{"type": "TARGET_HIT", "economic_position_id": "old-pos",
                                               "event_time": "2026-09-17T09:00:00Z"}],
                                  cutoff="2026-09-17T10:00:00Z")
    assert result["safe_to_import"] is True
    assert result["archived_historical_inconsistencies"]


def test_boundary_is_content_addressed():
    state = _state()
    boundary = make_boundary(state, captured_at="2026-09-17T10:00:00Z", event_cutoff="2026-09-17T10:00:00Z",
                             source_files={"compact": "compact.json"}, source_hashes={"state": "abc"},
                             archive_refs={"full_state": "preserved-read-only"})
    assert boundary.as_dict()["mode"] == "WORKING_SET"
    assert boundary.boundary_id.startswith("ws-")


def test_replay_deduplicates_lifecycle_event_identity():
    with tempfile.TemporaryDirectory() as directory:
        events = Path(directory) / "events.jsonl"
        state = {"counters": {"events": 0}, "setups": {}, "positions": {}, "symbols": {}}
        event = {"type": "TARGET_HIT", "economic_position_id": "pos-1", "setup_id": "setup-1"}
        with patch.object(forward, "EVENTS", events), patch.object(forward, "save_state", lambda value: None):
            forward.append_event(event, state)
            forward.append_event(event, state)
        assert len(events.read_text().splitlines()) == 1
        assert state["counters"]["events"] == 1


def test_projection_uses_nested_opportunity_as_position_authority():
    source = _state()
    source["setups"]["active"]["opportunities"][0]["status"] = "TARGET_HIT"
    source["positions"]["pos"]["status"] = "OPEN"
    compact = project_state(copy.deepcopy(source))
    assert compact["positions"]["pos"]["status"] == "TARGET_HIT"
