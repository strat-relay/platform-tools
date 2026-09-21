import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import liquidity_displacement_forward as f


class ForwardHealthTests(unittest.TestCase):
    def record(self, **overrides):
        rec = {
            "setup_id": "x", "direction": "LONG", "status": "TARGET_HIT",
            "detected_at": "2026-09-14T10:00:00+00:00",
            "simulated_fill_timestamp": "2026-09-14T10:05:00+00:00",
            "simulated_close_timestamp": "2026-09-14T10:20:00+00:00",
            "outcome": "WIN", "r_theoretical": 1.25, "r_realistic": 1.20,
            "entry_theoretical": 100.0, "stop_loss": 99.0, "stop_distance": 1.0,
            "target_theoretical": 101.25, "risk_at_001": 4.0,
            "required_balance_0_5": 800.0, "required_balance_1_0": 400.0,
            "required_balance_2_0": 200.0, "spread_at_detection": .20,
            "spread_at_detection_price": .20, "spread_at_detection_points": 200,
            "slippage_assumption_price": .02, "sweep_timestamp": "2026-09-14T10:00:00+00:00",
        }
        rec.update(overrides)
        return rec

    def test_spread_conversion(self):
        self.assertEqual(f.price_spread_points({"spread_points": 260}, {"point": .001}), (260.0, .26))

    def test_daily_aggregation_and_fill_rate(self):
        s = {"signals": {"x": self.record()}, "checkpoints": []}
        d = f.daily_aggregate(s, "2026-09-14")
        self.assertEqual(d["setups_detected"], 1)
        self.assertEqual(d["retracement_fills"], 1)
        self.assertEqual(d["fill_rate_pct"], 100)
        self.assertAlmostEqual(d["daily_r"], 1.25)

    def test_unfilled_followup(self):
        rec = self.record(status="UNFILLED_EXPIRED", outcome=None, simulated_fill_timestamp=None,
                          simulated_close_timestamp=None, r_theoretical=None, r_realistic=None,
                          displacement_index=0, entry_theoretical=100.0, stop_loss=99.0,
                          target_theoretical=101.25)
        bars = [{"time": 0, "high": 100.2, "low": 99.8}, {"time": 300, "high": 101.4, "low": 99.9}]
        f.follow_unfilled(rec, bars)
        self.assertTrue(rec["unfilled_followup"]["target_eventually_reached"])
        self.assertEqual(rec["status"], "UNFILLED_EXPIRED")

    def test_decision_telemetry_starts_at_sweep_and_records_rejection_measurements(self):
        bars = [{"time": i * 300, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0} for i in range(50)]
        bars[40] = {"time": 40 * 300, "open": 100.0, "high": 100.5, "low": 98.0, "close": 99.5}
        class FakeStrategy:
            def __init__(self, config): pass
            def _levels(self, m5, i): return 99.0, 101.0, 99.0, 101.0
        with patch.object(f, "LiquidityDisplacementStrategy", FakeStrategy), patch.object(f, "atr", return_value=[1.0]):
            telemetry = f.decision_telemetry(bars, [], 40, {"bid": 100, "ask": 100.2}, {"point": .01})
        self.assertEqual(telemetry["final_status"], "REJECTED_DISPLACEMENT_TOO_WEAK")
        self.assertTrue(telemetry["sweep"]["passed"])
        self.assertTrue(telemetry["reclaim"]["passed"])
        self.assertFalse(telemetry["displacement"]["passed"])
        self.assertEqual(telemetry["reason_code"], "REJECTED_DISPLACEMENT_TOO_WEAK")

    def test_account_feasibility(self):
        recs = [self.record(required_balance_0_5=100, required_balance_1_0=50, required_balance_2_0=25), self.record(setup_id="y", required_balance_0_5=300, required_balance_1_0=150, required_balance_2_0=75)]
        out = f.account_feasibility(recs)
        self.assertEqual(out["required_balance_0_5"]["minimum"], 100)
        self.assertEqual(out["100"]["0.5%"], 50.0)

    def test_warning_generation(self):
        summary = {"setups_detected": 10, "fill_rate_pct": 0, "wins": 0, "losses": 10, "time_exits": 0, "expectancy_r": -1, "profit_factor": 0, "max_drawdown_r": 7, "median_spread_price": .2, "median_stop_distance": 1}
        codes = {x["code"] for x in f.drift_warnings(summary)}
        self.assertTrue({"FILL_RATE_DRIFT", "EXPECTANCY_DRIFT", "PROFIT_FACTOR_DRIFT", "DRAWDOWN_WARNING"}.issubset(codes))

    def test_idempotent_daily_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "daily.jsonl"
            with patch.object(f, "DAILY", path):
                summary = {"date": "2026-09-14", "value": 1}
                self.assertTrue(f.persist_daily(summary))
                self.assertFalse(f.persist_daily(summary))
                self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_watch_snapshot_and_staleness(self):
        rec = self.record(status="WAITING_FOR_RETRACE", outcome=None, simulated_fill_timestamp=None, simulated_close_timestamp=None)
        state = {"signals": {"x": rec}, "running": True, "last_poll_timestamp": "2026-09-14T10:00:00+00:00", "version": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "source_sha256": "abc"}}
        data = {"quote": {"bid": 100, "ask": 100.26, "time": 1000, "spread_points": 260}, "contract": {"point": .001}, "M5": [{"time": 400, "close": 100}, {"time": 700, "close": 100}]}
        snap = f.watch_snapshot(state, data, now_ts=1000)
        self.assertEqual(snap["bridge"], "CONNECTED")
        self.assertEqual(len(snap["waiting"]), 1)
        stale = f.watch_snapshot(state, data, now_ts=2000)
        self.assertEqual(stale["mt5_state"], "STALE")

    def test_watch_position_checkpoint_and_recent_events(self):
        pos = self.record(status="FILLED", outcome=None, simulated_close_timestamp=None, r_theoretical=None, r_realistic=None)
        state = {"signals": {"x": pos}, "running": True, "last_poll_timestamp": "1970-01-01T00:16:00+00:00", "version": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "source_sha256": "abc"}}
        data = {"quote": {"bid": 100, "ask": 100.26, "time": 1000, "spread_points": 260}, "contract": {"point": .001}, "M5": [{"time": 400, "close": 100}, {"time": 700, "close": 100}]}
        snap = f.watch_snapshot(state, data, now_ts=1000)
        self.assertEqual(snap["next_checkpoint"], 25)
        self.assertEqual(len(snap["positions"]), 1)
        rendered = f.render_watch(snap)
        self.assertIn("Open:                1", rendered)
        with tempfile.TemporaryDirectory() as td:
            event_path = Path(td) / "events.jsonl"
            event_path.write_text('{"timestamp":"2026-09-14T10:00:00+00:00","event":"DETECTED","direction":"LONG"}\n')
            with patch.object(f, "EVENTS", event_path):
                self.assertEqual(f.event_rows(5)[0]["event"], "DETECTED")

    def test_gap_detection(self):
        state = {"last_candle": 1000, "last_poll_timestamp": "1970-01-01T00:00:00+00:00", "last_read_error": None}
        data = {"M5": [{"time": 1000}, {"time": 1300}, {"time": 1600}, {"time": 1900}, {"time": 2200}, {"time": 2500}, {"time": 2800}]}
        gap = f.detect_gap(state, data)
        self.assertEqual(gap["missing_m5_candles"], 4)
        self.assertEqual(gap["gap_start"], "1970-01-01T00:21:40+00:00")

    def test_gap_recovery_order_and_tag(self):
        state = {"signals": {}, "last_candle": 1000, "gold_symbols": []}
        data = {"M5": [{"time": t} for t in (1000, 1300, 1600, 1900, 2200, 2500, 2800)], "M15": [], "H1": [], "quote": {}, "contract": {}}
        gap = {"gap_start": "1970-01-01T00:21:40+00:00", "gap_end": "1970-01-01T00:31:40+00:00", "missing_m5_candles": 3}
        calls = []
        def fake_process(s, raw, source):
            calls.append((raw["M5"][-2]["time"], source)); s["last_candle"] = raw["M5"][-2]["time"]
        with patch.object(f, "process", side_effect=fake_process), patch.object(f, "event") as ev:
            result = f.replay_gap(state, data, gap)
        self.assertEqual([x[0] for x in calls], [1300, 1600, 1900])
        self.assertTrue(all(x[1] == "GAP_RECOVERY" for x in calls))
        self.assertEqual(result["recovery_status"], "COMPLETE")
        self.assertEqual(ev.call_args[0][1], "DATA_GAP_RECOVERY")

    def test_lock_prevents_duplicate_and_cleans_stale(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "runner.pid"
            with patch.object(f, "PIDFILE", path):
                state = {"started_at": "now"}
                path.write_text(json.dumps({"pid": 999999, "started_at": "old"}))
                self.assertTrue(f.acquire_lock(state))
                self.assertFalse(f.acquire_lock(state))
                f.release_lock()
                self.assertFalse(path.exists())

    def test_heartbeat_atomic_and_freshness_data(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "heartbeat.json"
            with patch.object(f, "HEARTBEAT", path):
                f.write_heartbeat({"last_successful_mt5_read": "x", "last_candle": 100}, "RUNNING", 15)
                hb = json.loads(path.read_text())
                self.assertEqual(hb["status"], "RUNNING")
                self.assertEqual(hb["poll_interval_seconds"], 15)

    def test_checkpoint_separates_live_and_recovered(self):
        live = self.record(setup_id="live")
        recovered = self.record(setup_id="recovered", detected_at="2026-09-14T11:00:00+00:00", source="GAP_RECOVERY", classification="RECOVERED_FORWARD")
        state = {"signals": {"live": live, "recovered": recovered}, "checkpoints": []}
        with tempfile.TemporaryDirectory() as td, patch.object(f, "STATE", Path(td) / "state.json"):
            cp = f.checkpoint(state)
            self.assertEqual(cp["live_observed"]["fills"], 1)
            self.assertEqual(cp["recovered"]["fills"], 1)

    def test_strategy_hash_unchanged(self):
        manifest = json.loads((Path(f.ROOT) / "liquidity_displacement_forward_manifest.json").read_text())
        self.assertEqual(f.source_hash(), manifest["source_sha256"])

    def test_graceful_shutdown_cleans_lock_and_marks_heartbeat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path, heartbeat_path, pid_path, summary_path = [root / x for x in ("state.json", "heartbeat.json", "runner.pid", "summary.md")]
            with patch.object(f, "STATE", state_path), patch.object(f, "HEARTBEAT", heartbeat_path), patch.object(f, "PIDFILE", pid_path), patch.object(f, "SUMMARY", summary_path), patch.object(f, "manifest", return_value={"source_sha256": "abc"}), patch.object(f, "daily_aggregate", return_value={}):
                state = {"running": True, "last_successful_mt5_read": None, "last_candle": None, "signals": {}, "fills": [], "checkpoints": [], "gold_symbols": []}
                f.atomic_write(pid_path, {"pid": __import__("os").getpid()})
                f.finalize_runner(state, 15)
                self.assertFalse(pid_path.exists())
                self.assertEqual(json.loads(heartbeat_path.read_text())["status"], "STOPPED")
                self.assertFalse(json.loads(state_path.read_text())["running"])

    def test_trades_ledger_is_read_only(self):
        trade = self.record(setup_id="trade", status="TARGET_HIT", exit_price_realistic=101.23, exit_price_theoretical=101.25)
        state = {"signals": {"trade": trade}}
        before = json.dumps(state, sort_keys=True)
        with patch.object(f, "save", side_effect=AssertionError("trades wrote state")), patch.object(f, "event", side_effect=AssertionError("trades appended event")), patch.object(f, "read_once", side_effect=AssertionError("trades read MT5")), patch.object(f, "LiquidityDisplacementStrategy", side_effect=AssertionError("trades evaluated strategy")):
            output = f.render_trades(state)
        self.assertIn("TARGET_HIT", output)
        self.assertIn("+1.25R", output)
        self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_trades_no_fill_message(self):
        state = {"signals": {"x": {"setup_id": "x", "status": "UNFILLED_EXPIRED", "detected_at": "2026-09-14T10:00:00+00:00"}}}
        output = f.render_trades(state)
        self.assertIn("No paper trades have filled yet.", output)
        self.assertIn("Setups detected: 1", output)
        self.assertIn("Expired unfilled: 1", output)

    def test_completed_candle_settlement_uses_first_hit_and_same_candle_sl_priority(self):
        rec = self.record(status="FILLED", outcome=None, simulated_close_timestamp=None, r_theoretical=None, r_realistic=None, simulated_fill_timestamp="1970-01-01T00:05:00+00:00", entry_theoretical=100.0, entry_realistic=100.13, stop_loss=99.0, target_theoretical=101.25)
        bars = [{"time": 0, "open": 100, "high": 100.5, "low": 99.8, "close": 100.1, "spread": 260}, {"time": 300, "open": 100.1, "high": 101.3, "low": 99.0, "close": 100.5, "spread": 260}]
        with patch.object(f, "event"):
            f.settle_filled_record({}, rec, bars, {"bid": 100, "ask": 100.26}, {"point": .001}, "LIVE_FORWARD")
        self.assertEqual(rec["outcome"], "LOSS")
        self.assertEqual(rec["status"], "STOPPED")
        self.assertEqual(rec["exit_reason"], "STOPPED")
        self.assertEqual(rec["duration_minutes"], 5)
        self.assertGreaterEqual(rec["mfe_r"], 0.5)
        self.assertGreaterEqual(rec["mae_r"], 1.0)
        self.assertEqual(rec["simulated_close_timestamp"], "1970-01-01T00:10:00+00:00")

    def test_watch_does_not_mutate_state_or_evaluate(self):
        state = {"signals": {}, "running": True, "version": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "source_sha256": "abc"}}
        before = json.dumps(state, sort_keys=True)
        with patch.object(f, "LiquidityDisplacementStrategy", side_effect=AssertionError("watch evaluated strategy")):
            f.watch_snapshot(state, {}, now_ts=1000)
        self.assertEqual(json.dumps(state, sort_keys=True), before)

    def test_watch_ctrl_c_does_not_stop_runner(self):
        with patch.object(f, "load_state", return_value={"signals": {}, "running": True, "version": {"version": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "source_sha256": "abc"}}), patch.object(f, "read_once", return_value={}), patch.object(f.time, "sleep", side_effect=KeyboardInterrupt):
            with patch("builtins.print") as printer:
                f.run_watch(5)
                self.assertIn("Forward paper runner remains active.", printer.call_args_list[-1].args[0])


if __name__ == "__main__":
    unittest.main()
