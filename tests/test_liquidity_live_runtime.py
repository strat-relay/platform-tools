import unittest
from dataclasses import replace
from unittest.mock import patch

from orchestration.liquidity_live import (
    PARAMETER_SETS,
    LiquidityLiveEvaluator,
    PaperStateRejected,
)
from liquidity_market_data import LiveMarketSnapshot
from liquidity_live_runtime import LiquidityLiveRuntime
from liquidity_live_service import run_once


class FakeStrategy:
    def find_candidate(self, m15, m5, i, quote, contract, timestamp):
        return {
            "direction": "LONG",
            "displacement_index": 1,
            "stop_loss": 98.0,
            "setup_type": "LIQUIDITY_DISPLACEMENT_SCALP",
            "setup_id": "setup-1",
        }


class IncrementalStrategy:
    def find_candidate(self, m15, m5, i, quote, contract, timestamp):
        if i != 40:
            return None
        return {"direction": "LONG", "displacement_index": 40, "stop_loss": 98.0}


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params):
        self.calls.append((query, params))


class RecordingConnection:
    def __init__(self):
        self.recording_cursor = RecordingCursor()

    def cursor(self):
        return self.recording_cursor


def snapshot():
    bars = [
            {"time": 1000, "low": 99, "high": 100, "close": 99.5},
            {"time": 1300, "low": 99, "high": 104, "close": 103},
            {"time": 1600, "low": 101, "high": 104, "close": 102},
        ] + [{"time": 1900 + n * 300, "low": 101, "high": 104, "close": 102} for n in range(42)]
    return LiveMarketSnapshot(
        M5=tuple(bars),
        M15=({"time": 900, "low": 98, "high": 105, "close": 102},),
        quote={"bid": 102.0, "ask": 102.1},
        contract={"tick_size": 0.01, "stops_level": 0, "point": 0.01},
        canonical_instrument="XAUUSD", provider_symbol="XAUUSDm", source_market_data_timestamp="2026-09-26T12:00:00Z")


class LiquidityLiveRuntimeTests(unittest.TestCase):
    def test_heartbeat_serializes_runtime_metadata_for_jsonb(self):
        conn = RecordingConnection()
        runtime = LiquidityLiveRuntime(conn=conn, snapshot_reader=lambda *_args: None,
                                       publisher=object())

        runtime.heartbeat()

        query, params = conn.recording_cursor.calls[0]
        self.assertIn("metadata", query)
        self.assertIn("%s", query)
        self.assertEqual(params[2], '{"broker_writes": 0, "source": "LIVE_MARKET"}')

    def test_workload_is_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                run_once()

    def test_ordinary_forged_snapshot_cannot_cross_live_boundary(self):
        evaluator = LiquidityLiveEvaluator(PARAMETER_SETS["liquidity-xau-base"], strategy=FakeStrategy())
        with self.assertRaises(PaperStateRejected):
            evaluator.evaluate({"source_kind": "LIVE_MARKET", "M5": [], "M15": [], "quote": {}, "contract": {}},
                               evaluation_time="2026-09-26T12:00:00Z")

    def test_wrong_validated_boundary_is_rejected(self):
        evaluator = LiquidityLiveEvaluator(PARAMETER_SETS["liquidity-xau-base"], strategy=FakeStrategy())
        with self.assertRaises(PaperStateRejected):
            evaluator.evaluate(replace(snapshot(), validated_by="paper-runner"),
                               evaluation_time="2026-09-26T12:00:00Z")

    def test_market_data_cache_is_a_validated_live_boundary(self):
        evaluator = LiquidityLiveEvaluator(PARAMETER_SETS["liquidity-xau-base"], strategy=FakeStrategy())
        result = evaluator.evaluate(replace(snapshot(), validated_by="market-data-cache"),
                                     evaluation_time="2026-09-26T12:00:00Z")
        self.assertIsNotNone(result)
        self.assertEqual(result.provenance["validated_by"], "market-data-cache")

    def test_incremental_setup_waits_for_later_completed_bar_and_survives_restart(self):
        params = PARAMETER_SETS["liquidity-xau-base"]
        bars = list(snapshot().M5[:40]) + [
            {"time": 13000, "low": 101, "high": 104, "close": 102},
            {"time": 13300, "low": 103, "high": 105, "close": 104},
        ]
        first_snapshot = replace(snapshot(), M5=tuple(bars))
        evaluator = LiquidityLiveEvaluator(params, strategy=IncrementalStrategy())
        self.assertIsNone(evaluator.evaluate(first_snapshot, evaluation_time="2026-09-26T12:00:00Z"))
        saved = evaluator.export_state()
        self.assertEqual(saved[0]["state"], "PENDING_RETRACE")
        restarted = LiquidityLiveEvaluator(params, strategy=IncrementalStrategy())
        restarted.restore(saved)
        fill = {"time": 13600, "low": 100, "high": 103, "close": 103}
        result = restarted.evaluate(replace(first_snapshot, M5=tuple(bars + [fill])),
                                    evaluation_time="2026-09-26T12:01:00Z")
        self.assertIsNotNone(result)
        self.assertEqual(restarted.export_state()[0]["state"], "ENTERED")
    def test_four_variants_remain_explicit_parameter_sets(self):
        self.assertEqual(set(PARAMETER_SETS), {
            "liquidity-xau-base", "liquidity-xau33", "liquidity-btc25", "liquidity-usdjpy25",
        })
        self.assertEqual(PARAMETER_SETS["liquidity-xau-base"].max_retrace_candles, 3)
        self.assertEqual(PARAMETER_SETS["liquidity-xau33"].entry_fraction, 1 / 3)
        self.assertEqual(PARAMETER_SETS["liquidity-btc25"].entry_fraction, 0.25)

    def test_paper_state_cannot_cross_live_boundary(self):
        evaluator = LiquidityLiveEvaluator(PARAMETER_SETS["liquidity-xau-base"], strategy=FakeStrategy())
        paper = replace(snapshot(), source_kind="FORWARD_PAPER")
        with self.assertRaises(PaperStateRejected):
            evaluator.evaluate(paper, evaluation_time="2026-09-26T12:00:00Z")

    def test_signal_is_canonical_and_idempotent_across_restart(self):
        params = PARAMETER_SETS["liquidity-xau-base"]
        first = LiquidityLiveEvaluator(params, strategy=FakeStrategy()).evaluate(
            snapshot(), evaluation_time="2026-09-26T12:00:00Z")
        restarted = LiquidityLiveEvaluator(params, strategy=FakeStrategy()).evaluate(
            snapshot(), evaluation_time="2026-09-26T12:01:00Z")
        self.assertIsNotNone(first)
        self.assertIsNotNone(restarted)
        self.assertEqual(first.signal_id, restarted.signal_id)
        self.assertEqual(first.strategy_id, "LIQUIDITY_DISPLACEMENT_SCALP_V1")
        self.assertEqual(first.strategy_instance_id, "liquidity-xau-base")
        self.assertEqual(first.entry_type, "MARKET")
        self.assertEqual(first.provenance["source_kind"], "LIVE_MARKET")
        self.assertFalse(first.provenance["paper_only"])

    def test_same_process_duplicate_poll_is_suppressed(self):
        evaluator = LiquidityLiveEvaluator(PARAMETER_SETS["liquidity-xau-base"], strategy=FakeStrategy())
        self.assertIsNotNone(evaluator.evaluate(snapshot(), evaluation_time="2026-09-26T12:00:00Z"))
        self.assertIsNone(evaluator.evaluate(snapshot(), evaluation_time="2026-09-26T12:00:01Z"))


if __name__ == "__main__":
    unittest.main()
