import json
import tempfile
import unittest
from pathlib import Path

from research.context_structure_retrace_post_exit_ledger import observe_state, resolve_symbol_provenance


class FakeStore:
    def __init__(self):
        self.calls = []

    def metadata(self, symbol):
        self.calls.append(("metadata", symbol))
        return {"symbol_info": {"point": 0.01, "tick_size": 0.01}}

    def bars(self, symbol, timeframe):
        self.calls.append(("bars", symbol, timeframe))
        return [{"time": 1200, "high": 105.0, "low": 99.0, "spread": 1, "complete": True}]


def stopped(**overrides):
    value = {
        "economic_position_id": "pos-1",
        "entry_opportunity_id": "opp-1",
        "signal_id": "sig-1",
        "setup_id": "setup-1",
        "status": "STOPPED",
        "exit_reason": "STOPPED",
        "exit_timestamp": "1970-01-01T00:10:00+00:00",
        "fill_timestamp": 600,
        "executable_paper_entry": 100.0,
        "stop": 95.0,
        "target": 104.0,
        "direction": "LONG",
        "geometry": {"stop_distance": 5.0, "target_R": 0.8},
    }
    value.update(overrides)
    return value


def state(position, **collections):
    return {"strategy_version": "CONTEXT_STRUCTURE_RETRACE_V1", "positions": {"p": position}, **collections}


def test_symbol_provenance_precedence_and_conflict():
    result = resolve_symbol_provenance(
        stopped(symbol="BTCUSDm"),
        state(stopped(symbol="BTCUSDm"), broker_results=[{"signal_id": "sig-1", "symbol": "ETHUSDm"}]),
    )
    assert result["symbol"] == "BTCUSDm"
    assert result["source"] == "position"
    assert result["conflicts"] == [{"source": "broker_result", "symbol": "ETHUSDm", "selected_symbol": "BTCUSDm"}]


def test_symbol_can_be_recovered_from_each_fallback():
    for collection, key in (
        ("broker_results", "broker_result"),
        ("execution_attempts", "execution_attempt"),
        ("execution_intents", "execution_intent"),
        ("signals", "signal"),
        ("candidates", "candidate"),
    ):
        position = stopped()
        result = resolve_symbol_provenance(position, state(position, **{collection: [{"signal_id": "sig-1", "symbol": "BTCUSDm"}]}))
        assert result["symbol"] == "BTCUSDm"
        assert result["source"] == key

    position = stopped()
    setup = {"setup-1": {"opportunities": [{"entry_opportunity_id": "opp-1", "symbol": "BTCUSDm"}]}}
    result = resolve_symbol_provenance(position, state(position, setups=setup))
    assert result["source"] == "setup_opportunity"


def test_unrecoverable_symbol_is_explicit_and_never_looked_up(tmp_path: Path):
    position = stopped()
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "ledger.jsonl"
    state_path.write_text(json.dumps(state(position)), encoding="utf-8")
    store = FakeStore()

    result = observe_state(state_path, ledger_path, store)

    assert result["data_gaps"][0]["reason"] == "MISSING_SYMBOL_PROVENANCE"
    assert store.calls == []
    assert "None" not in ledger_path.read_text(encoding="utf-8")


def test_malformed_position_does_not_stop_valid_position(tmp_path: Path):
    malformed = stopped(economic_position_id="bad", signal_id="bad-signal")
    valid = stopped(economic_position_id="good", signal_id="good-signal", symbol="BTCUSDm")
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "ledger.jsonl"
    state_path.write_text(json.dumps({"strategy_version": "CONTEXT_STRUCTURE_RETRACE_V1",
                                      "positions": {"bad": malformed, "good": valid}}), encoding="utf-8")

    result = observe_state(state_path, ledger_path, FakeStore())

    assert result["records_written"] == 1
    assert {gap["reason"] for gap in result["data_gaps"]} == {"MISSING_SYMBOL_PROVENANCE"}


def test_observation_is_idempotent_across_restart(tmp_path: Path):
    position = stopped(symbol="BTCUSDm")
    state_path = tmp_path / "state.json"
    ledger_path = tmp_path / "ledger.jsonl"
    state_path.write_text(json.dumps(state(position)), encoding="utf-8")

    assert observe_state(state_path, ledger_path, FakeStore())["records_written"] == 1
    assert observe_state(state_path, ledger_path, FakeStore())["records_written"] == 0
    assert len(ledger_path.read_text(encoding="utf-8").splitlines()) == 1


class ContextPostExitObserverTests(unittest.TestCase):
    def test_symbol_provenance_precedence_and_conflict(self):
        test_symbol_provenance_precedence_and_conflict()

    def test_symbol_can_be_recovered_from_each_fallback(self):
        test_symbol_can_be_recovered_from_each_fallback()

    def test_unrecoverable_symbol_is_explicit_and_never_looked_up(self):
        with tempfile.TemporaryDirectory() as directory:
            test_unrecoverable_symbol_is_explicit_and_never_looked_up(Path(directory))

    def test_malformed_position_does_not_stop_valid_position(self):
        with tempfile.TemporaryDirectory() as directory:
            test_malformed_position_does_not_stop_valid_position(Path(directory))

    def test_observation_is_idempotent_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            test_observation_is_idempotent_across_restart(Path(directory))
