import json

from research.price_action_historical_replay import audit_series, event_stream
from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_engulfing_continuation import EngulfingContinuationEvaluator, parameter_set, strategy_version


def _replay(events):
    evaluator = EngulfingContinuationEvaluator()
    evaluator.initialize(strategy_version(), parameter_set())
    return [item for event in events for item in evaluator.consume_market_event(event)]


def test_audit_rejects_unexpected_gap_and_invalid_ohlc(tmp_path):
    series = tmp_path / "EURUSD"
    series.mkdir()
    (series / "H1.metadata.json").write_text(json.dumps({
        "boundary_semantics": "start <= candle_timestamp < end",
        "completed_only": True, "gap_ledger": [], "bar_count": 2,
        "first_timestamp": 0, "last_timestamp": 7200,
    }))
    (series / "H1.jsonl").write_text("\n".join([
        json.dumps({"time": 0, "open": 1, "high": 2, "low": 0, "close": 1}),
        json.dumps({"time": 7200, "open": 1, "high": 0, "low": 2, "close": 1}),
    ]) + "\n")
    result = audit_series("EURUSD", "H1", series / "H1.jsonl")
    assert result["unexpected_gap_count"] == 1
    assert result["invalid_ohlc_timestamps"] == [7200]


def test_equal_close_orders_completed_h1_before_m15():
    rows = {
        ("EURUSD", "H1"): [{"time": 0, "open": 1, "high": 2, "low": 0, "close": 1}],
        ("EURUSD", "M15"): [{"time": 2700, "open": 1, "high": 2, "low": 0, "close": 1}],
    }
    events = event_stream("EURUSD", rows, 0, 3600, "test", 0.00001)
    assert [event.timeframe for event in events] == ["H1", "M15"]


def test_market_event_rejects_incomplete_ohlc():
    try:
        MarketEvent("EURUSD", "H1", 0, 3600, 1, 0, 2, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid OHLC must be rejected")


def test_prefix_invariance_and_deterministic_candidate_hashes():
    events = [
        MarketEvent("EURUSD", "H1", i * 3600, (i + 1) * 3600, 1 + i * .001,
                    1.01 + i * .001, .99 + i * .001, 1 + i * .001,
                    provenance={"tick_size": .00001, "snapshot_id": "test"})
        for i in range(16)
    ] + [
        MarketEvent("EURUSD", "M15", 16 * 3600 + i * 900, 16 * 3600 + (i + 1) * 900,
                    1, 1.01, .99, 1, provenance={"tick_size": .00001, "snapshot_id": "test"})
        for i in range(4)
    ]
    prefix = events[:18]
    full_prefix = [item for item in _replay(events) if int(item.decision_time) <= prefix[-1].close_timestamp]
    direct_prefix = _replay(prefix)
    assert [item.evaluation_hash for item in direct_prefix] == [item.evaluation_hash for item in full_prefix]
