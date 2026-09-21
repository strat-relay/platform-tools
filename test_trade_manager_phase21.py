import tempfile
import unittest
from pathlib import Path

from trade_manager.experiments import MultiPolicyExperimentRunner, default_experiments
from trade_manager.prospective import ProspectiveActivation, ProspectiveExperimentCollector
from trade_manager.observation_storage import ObservationStore


def pos():
    return {"strategy_id": "S", "setup_id": "U", "economic_position_id": "P", "symbol": "XAUUSDm",
            "direction": "LONG", "entry": 100.0, "original_stop": 90.0, "original_target": 120.0,
            "current_stop": 90.0, "size": 1, "status": "OPEN", "created_at": "2026-09-17T11:00:00+00:00"}


def obs(ts, bid, ask, r):
    return {"timestamp": ts, "bid": bid, "ask": ask, "close_executable_price": bid,
            "current_R": r, "mfe_R": max(0, r), "mae_R": max(0, -r), "time_in_trade_seconds": 60,
            "market": {"M5_EMA": {"value": None}, "structure": {}}}


class Phase21Tests(unittest.TestCase):
    def test_control_and_candidates_are_present(self):
        specs = default_experiments()
        self.assertEqual(specs[0].experiment_id, "CONTROL_FROZEN")
        self.assertTrue(all(x.experimental for x in specs))

    def test_candidates_are_isolated_and_one_can_close(self):
        runner = MultiPolicyExperimentRunner([default_experiments()[0], default_experiments()[1]])
        p = pos()
        runner.observe(p, obs("2026-09-17T11:01:00Z", 106, 107, .6))
        rows = runner.observe(p, obs("2026-09-17T11:02:00Z", 100, 101, 0.0))
        by_id = {r["management_experiment_id"]: r for r in rows}
        self.assertEqual(by_id["BE_PROTECTION_0_50R"]["status"], "CLOSED")
        self.assertEqual(by_id["BE_PROTECTION_0_50R"]["exit_reason"], "HYPOTHETICAL_STOP")
        self.assertEqual(runner.positions[("P", "CONTROL_FROZEN")].status, "OPEN")

    def test_activation_cutoff_excludes_old_positions(self):
        with tempfile.TemporaryDirectory() as d:
            state = {"trade_manager_activation_cutoff": "2026-09-17T11:30:00+00:00",
                     "strategies": ["S"], "instruments": ["XAUUSDm"]}
            collector = ProspectiveExperimentCollector(lambda: [pos()], lambda _: {"quote": {"bid": 100, "ask": 101}}, state, ObservationStore(Path(d) / "obs"))
            self.assertEqual(collector.collect_once("2026-09-17T10:00:00Z")["eligible_positions"], 0)
            newer = {**pos(), "created_at": "2026-09-17T12:00:00+00:00"}
            collector.position_source = lambda: [newer]
            self.assertEqual(collector.collect_once("2026-09-17T12:01:00Z")["eligible_positions"], 1)
            self.assertTrue((Path(d) / "obs" / "experiment_results.jsonl").exists())

    def test_summary_and_restart_activation(self):
        runner = MultiPolicyExperimentRunner([default_experiments()[0]])
        runner.observe(pos(), obs("2026-09-17T11:01:00Z", 95, 96, .5))
        summary = runner.summary()
        self.assertEqual(summary[0]["N"], 0)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "activation.json"
            first = ProspectiveActivation(path).activate(strategies=["S"], instruments=["XAUUSDm"])
            self.assertEqual(ProspectiveActivation(path).load(), first)

    def test_dashboard_backend_data(self):
        activation = {"trade_manager_activation_cutoff": "2026-09-17T10:00:00+00:00", "strategies": ["S"], "instruments": ["XAUUSDm"]}
        collector = ProspectiveExperimentCollector(lambda: [], lambda _: {}, activation)
        dashboard = collector.runner.dashboard()
        self.assertEqual(dashboard["broker_writes"], 0)
        self.assertTrue(any(x["experiment_id"] == "CONTROL_FROZEN" for x in dashboard["experiments"]))

    def test_experiment_restore_preserves_hypothetical_stop(self):
        runner = MultiPolicyExperimentRunner([default_experiments()[0], default_experiments()[1]])
        p = pos(); runner.observe(p, obs("2026-09-17T11:01:00Z", 106, 107, .6))
        rows = runner.results
        restarted = MultiPolicyExperimentRunner([default_experiments()[0], default_experiments()[1]])
        restarted.restore(p, rows)
        self.assertEqual(restarted.positions[("P", "BE_PROTECTION_0_50R")].hypothetical_stop, 100.0)

    def test_no_broker_surface(self):
        import trade_manager.experiments as module
        self.assertFalse(any(x in dir(module) for x in ("OrderSend", "CTrade", "mt5_market_order")))


if __name__ == "__main__":
    unittest.main()
