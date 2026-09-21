"""Tests for the standard strategy observability reporting layer:
liquidity_displacement_forward.build_standard_report(), the instance-safe
readers in control_api/observability.py, and the Control API routes that
expose them. These are read-only tests — no live runner is started, stopped,
or restarted, and no strategy module global is ever left mutated.
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

import liquidity_displacement_forward as liq
from control_api import observability as obs


def _signal(setup_id, direction="LONG", outcome=None, r=0.0, spread=0.5, fraction_day="2026-09-17"):
    row = {
        "setup_id": setup_id, "direction": direction,
        "detected_at": f"{fraction_day}T10:00:00+00:00",
        "spread_at_detection_price": spread, "spread_at_detection": spread,
        "stop_distance": 1.0, "status": "WAITING_FOR_RETRACE",
    }
    if outcome:
        row.update({
            "status": "TARGET_HIT" if outcome == "WIN" else "STOPPED" if outcome == "LOSS" else "TIME_EXIT",
            "outcome": outcome, "r_theoretical": r, "r_realistic": r,
            "simulated_fill_timestamp": f"{fraction_day}T10:05:00+00:00",
            "simulated_close_timestamp": f"{fraction_day}T11:05:00+00:00",
            "entry_realistic": 100.0, "exit_price_realistic": 100.0 + r,
        })
    return row


class LoadStateFromMatchesLoadState(unittest.TestCase):
    def test_matches_load_state_when_file_exists(self):
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        payload = {"version": {"version": "X"}, "signals": {"a": _signal("a")}, "started_at": "t0"}
        tmp.write_text(json.dumps(payload))

        original_state = liq.STATE
        try:
            liq.STATE = tmp
            expected = liq.load_state()
        finally:
            liq.STATE = original_state

        actual = obs._load_state_from(tmp)
        self.assertEqual(expected, actual)


class ModulePurityTests(unittest.TestCase):
    """Proves the Control API path never calls configure() and never leaves
    liquidity_displacement_forward's globals mutated, for any instance."""

    TRACKED = ("STATE", "EVENTS", "DAILY", "MANIFEST", "PIDFILE", "HEARTBEAT", "STOP", "SYMBOL", "CFG",
               "LiquidityDisplacementStrategy", "manifest", "event", "process")

    def _snapshot(self):
        return {name: getattr(liq, name) for name in self.TRACKED}

    def test_globals_unchanged_after_reading_every_registered_instance(self):
        before = self._snapshot()
        for instance_id in obs.LIQUIDITY_INSTANCES:
            obs.get_strategy_report(instance_id)
        after = self._snapshot()
        for name in self.TRACKED:
            self.assertIs(before[name], after[name], f"{name} was replaced by observability read path")

    def test_concurrent_cross_instance_reads_do_not_contaminate(self):
        ids = [i for i, meta in obs.LIQUIDITY_INSTANCES.items() if meta["state"].exists()]
        if len(ids) < 2:
            self.skipTest("fewer than two live instances on disk; nothing to cross-contaminate")
        baseline = {i: obs.get_strategy_report(i)[1] for i in ids}
        results = {}
        lock = threading.Lock()

        def worker(instance_id):
            _, data = obs.get_strategy_report(instance_id)
            with lock:
                results[instance_id] = data

        threads = [threading.Thread(target=worker, args=(i,)) for i in ids for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for instance_id in ids:
            self.assertEqual(results[instance_id]["identity"]["strategy_id"], instance_id)
            self.assertEqual(results[instance_id]["performance"], baseline[instance_id]["performance"],
                              f"{instance_id} performance changed under concurrent cross-instance reads")


class DailyRecordsParameterizationTests(unittest.TestCase):
    """The four tests requested for the daily_records/event_records fix."""

    def _state_with_many_closed_trades(self, spread=0.001):
        signals = {}
        for i in range(12):
            signals[f"s{i}"] = _signal(f"s{i}", outcome="WIN" if i % 2 == 0 else "LOSS", r=0.3, spread=spread)
        return {"signals": signals, "version": {}, "checkpoints": [], "decision_telemetry": {}}

    def test_default_behavior_unchanged_when_daily_records_omitted(self):
        s = self._state_with_many_closed_trades()
        # No 3rd arg at all — must behave exactly as before parameterization.
        result = liq.daily_aggregate(s, "2026-09-17")
        self.assertIn("warnings", result)
        self.assertIsInstance(result["warnings"], list)

    def test_two_instances_with_different_daily_histories_get_own_drift_warnings(self):
        s = self._state_with_many_closed_trades(spread=1.0)
        # Instance A has no prior daily history -> SPREAD_DRIFT cannot fire (needs >=2 prior points).
        result_a = liq.daily_aggregate(s, "2026-09-17", daily_records=[])
        codes_a = {w["code"] for w in result_a["warnings"]}
        self.assertNotIn("SPREAD_DRIFT", codes_a)

        # Instance B has a very-low-spread prior history -> today's much wider
        # spread must trigger SPREAD_DRIFT for B but must not have affected A.
        prior_b = [{"median_spread_price": 0.01}, {"median_spread_price": 0.01}]
        result_b = liq.daily_aggregate(s, "2026-09-17", daily_records=prior_b)
        codes_b = {w["code"] for w in result_b["warnings"]}
        self.assertIn("SPREAD_DRIFT", codes_b)
        self.assertNotIn("SPREAD_DRIFT", codes_a)

    def test_no_module_globals_mutated_by_daily_records_parameter(self):
        before_daily = liq.DAILY
        s = self._state_with_many_closed_trades()
        liq.daily_aggregate(s, "2026-09-17", daily_records=[{"median_spread_price": 9.0}])
        self.assertIs(liq.DAILY, before_daily)

    def test_concurrent_daily_aggregate_calls_do_not_cross_contaminate(self):
        s = self._state_with_many_closed_trades(spread=1.0)
        results = {}
        lock = threading.Lock()

        def worker(key, prior):
            r = liq.daily_aggregate(s, "2026-09-17", daily_records=prior)
            with lock:
                results[key] = {w["code"] for w in r["warnings"]}

        t1 = threading.Thread(target=worker, args=("no_prior", []))
        t2 = threading.Thread(target=worker, args=("drifted", [{"median_spread_price": 0.01}, {"median_spread_price": 0.01}]))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertNotIn("SPREAD_DRIFT", results["no_prior"])
        self.assertIn("SPREAD_DRIFT", results["drifted"])


class KillSwitchPerInstanceTests(unittest.TestCase):
    def test_kill_switch_reflects_instance_own_stop_path_not_bases(self):
        tmp = Path(tempfile.mkdtemp())
        stop_a = tmp / "a.stop"
        stop_b = tmp / "b.stop"
        stop_a.touch()  # only instance A's kill switch is set
        s = {"signals": {}, "version": {"version": "V"}, "checkpoints": [], "decision_telemetry": {}}
        instance_a = {"instance_id": "A", "family_id": "F", "symbol": "X", "label": "A", "stop_path": stop_a}
        instance_b = {"instance_id": "B", "family_id": "F", "symbol": "X", "label": "B", "stop_path": stop_b}
        report_a = liq.build_standard_report(s, instance_a, daily_records=[], event_records=[])
        report_b = liq.build_standard_report(s, instance_b, daily_records=[], event_records=[])
        self.assertTrue(report_a["status"]["kill_switch"])
        self.assertFalse(report_b["status"]["kill_switch"])


class InstanceSymbolCorrectnessTests(unittest.TestCase):
    """Regression test: build_standard_report() must report each instance's
    own configured symbol, never the base engine's module-global CFG.symbol
    (a real bug caught by browser-level verification — every non-base
    instance was silently reporting XAUUSDm regardless of its real symbol)."""

    def test_positions_and_symbols_use_instance_symbol_not_module_global(self):
        s = {
            "signals": {
                "s1": _signal("s1", outcome="WIN", r=0.5),
            },
            "version": {}, "checkpoints": [], "decision_telemetry": {},
        }
        instance = {"instance_id": "USDJPY25", "family_id": "F", "symbol": "USDJPYm", "label": "USDJPY 25%",
                    "stop_path": Path("/tmp/does-not-exist.stop")}
        report = liq.build_standard_report(s, instance, daily_records=[], event_records=[])
        self.assertNotEqual(liq.CFG.symbol, "USDJPYm", "test fixture assumption broken: module CFG must differ from instance symbol")
        self.assertEqual(report["symbols"][0]["symbol"], "USDJPYm")
        self.assertEqual(report["closed_positions"][0]["symbol"], "USDJPYm")


class NeverFabricatedTests(unittest.TestCase):
    def test_empty_state_reports_unavailable_not_zero(self):
        s = {"signals": {}, "version": {}, "checkpoints": [], "decision_telemetry": {}}
        instance = {"instance_id": "EMPTY", "family_id": "F", "symbol": "X", "label": "Empty",
                    "stop_path": Path("/tmp/does-not-exist.stop")}
        report = liq.build_standard_report(s, instance, daily_records=[], event_records=[])
        self.assertIsNone(report["performance"]["expectancy_R"])
        self.assertIsNone(report["performance"]["profit_factor"])
        self.assertEqual(report["performance"]["trades"], 0)


class RegistryAndDegradationTests(unittest.TestCase):
    def test_non_live_variants_are_enumerable_but_flagged_not_live(self):
        non_live = [i for i, m in obs.LIQUIDITY_INSTANCES.items() if not m["state"].exists()]
        for instance_id in non_live:
            found, data = obs.get_strategy_report(instance_id)
            self.assertFalse(found)
            self.assertEqual(data["status"], "NOT_LIVE")

    def test_unknown_identifier_returns_no_adapter_not_crash(self):
        found, data = obs.get_strategy_report("SOME_UNKNOWN_STRATEGY_ID")
        self.assertFalse(found)
        self.assertEqual(data["status"], "NO_ADAPTER")

    def test_shadow_lookup_for_strategy_with_no_shadow_entity_is_empty_list(self):
        result = obs.get_strategy_shadows("LIQUIDITY_DISPLACEMENT_SCALP_V1")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
