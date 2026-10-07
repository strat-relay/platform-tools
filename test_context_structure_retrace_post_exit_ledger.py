import json

from research import context_structure_retrace_post_exit_ledger as ledger


def position(direction="LONG", status="STOPPED", exit_timestamp=1200):
    entry = 100.0
    stop = 99.0 if direction == "LONG" else 101.0
    target = 101.25 if direction == "LONG" else 98.75
    return {
        "economic_position_id": "ep-1", "symbol": "GBPUSDm", "direction": direction,
        "setup_id": "setup-1", "pattern": "BULLISH_REJECTION_WICK", "fill_timestamp": 900,
        "fill_timestamp_iso": ledger.iso(900), "executable_paper_entry": entry, "stop": stop,
        "target": target, "geometry": {"stop_distance": 1.0, "target_R": 1.25},
        "status": status, "exit_timestamp": exit_timestamp, "exit_reason": "STOPPED",
    }


def bar(ts, high, low, spread=0):
    return {"time": ts, "open": low, "high": high, "low": low, "close": high, "spread": spread}


def test_only_stopped_positions_are_selected():
    state = {"strategy_version": ledger.STRATEGY_ID, "positions": {"open": position(status="OPEN"), "stopped": position()}}
    assert [row["economic_position_id"] for row in ledger.stopped_positions(state)] == ["ep-1"]


def test_post_exit_observations_measure_target_and_stop_excursion():
    row = position()
    record = ledger.observe_stopped_trade(row, {
        "M5": [bar(1500, 101.3, 98.4), bar(1800, 100.5, 98.0)],
        "M15": [bar(1800, 101.3, 98.0)],
    }, {"point": 0.01})
    assert record["original_target_after_stop"] is True
    assert record["time_to_original_target_minutes"] == 5
    assert record["max_excursion_beyond_original_stop_r"] == 1.0
    assert record["low_rr_diagnostic_cohort"] is False
    assert record["low_rr_diagnostic_only"] is True


def test_short_uses_ask_sides_for_favorable_and_adverse():
    row = position("SHORT")
    record = ledger.observe_stopped_trade(row, {
        "M5": [bar(1201, 102.2, 98.6, spread=10), bar(1500, 99.0, 98.0, spread=10)],
        "M15": [],
    }, {"point": 0.01})
    assert record["original_target_after_stop"] is True
    assert record["max_excursion_beyond_original_stop_r"] > 1.0


def test_low_rr_is_a_diagnostic_label_not_a_filter():
    row = position()
    row["geometry"]["target_R"] = 0.10
    record = ledger.observe_stopped_trade(row, {"M5": [], "M15": []}, {"point": 0.01})
    assert record["low_rr_diagnostic_cohort"] is True
    assert record["low_rr_diagnostic_only"] is True
    assert record["record_type"] == "CONTEXT_STOPPED_POST_EXIT_OBSERVATION"


def test_ledger_is_idempotent(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = ledger.ObservationLedger(path)
    record = ledger.observe_stopped_trade(position(), {"M5": [], "M15": []}, {"point": 0.01})
    assert writer.append(record) is True
    assert writer.append(record) is False
    assert len(path.read_text().splitlines()) == 1
    assert json.loads(path.read_text())["research_only"] is True


def test_ledger_reloads_by_streaming_records(tmp_path):
    path = tmp_path / "ledger.jsonl"
    writer = ledger.ObservationLedger(path)
    first = ledger.observe_stopped_trade(position(), {"M5": [], "M15": []}, {"point": 0.01})
    second_position = position()
    second_position["economic_position_id"] = "ep-2"
    second = ledger.observe_stopped_trade(second_position, {"M5": [], "M15": []}, {"point": 0.01})
    assert writer.append(first) is True
    assert writer.append(second) is True
    reloaded = ledger.ObservationLedger(path)
    assert reloaded.append(first) is False
    assert reloaded.append(second) is False
