import tempfile
import unittest

from trade_manager.counterfactual import CounterfactualPosition
from trade_manager.metrics import mfe_surrender
from trade_manager.phase2 import Phase2TradeManager
from trade_manager.policies import ManagementPolicyConfig, PolicyRegistry, context_v1_experiment
from trade_manager.semantics import price_semantics
from trade_manager.state import ManagementState, ManagementStateStore
from trade_manager.trailing import evaluate_trailing


def position(direction="LONG"):
    return {"strategy_id": "S", "setup_id": "U", "economic_position_id": "P", "symbol": "XAUUSDm",
            "direction": direction, "entry": 100.0, "original_stop": 90.0 if direction == "LONG" else 110.0,
            "original_target": 120.0 if direction == "LONG" else 80.0, "current_stop": 102.0 if direction == "LONG" else 98.0,
            "size": 1, "status": "OPEN"}


def observation(direction="LONG"):
    return {"close_executable_price": 110.0 if direction == "LONG" else 90.0, "current_R": 1.0,
            "mfe_R": 1.3, "market": {"M5_EMA": {"value": 105.0}, "structure": {
                "latest_confirmed_swing_low": {"price": 106.0}, "latest_confirmed_swing_high": {"price": 104.0}}}}


class Phase2Tests(unittest.TestCase):
    def test_price_semantics_long_short(self):
        long = price_semantics("LONG", 99, 100)
        short = price_semantics("SHORT", 99, 100)
        self.assertEqual((long.entry_price, long.close_price), (100, 99))
        self.assertEqual((short.entry_price, short.close_price), (99, 100))
        self.assertEqual(short.stop_cross_price, 100)

    def test_policy_resolution_hierarchy_and_missing_policy(self):
        registry = PolicyRegistry(); default = context_v1_experiment(); registry.register_strategy_default(default)
        self.assertEqual(registry.resolve("CONTEXT_STRUCTURE_RETRACE_V1", "XAUUSDm").policy_id, default.policy_id)
        self.assertIsNone(registry.resolve("UNKNOWN", "XAUUSDm"))
        override = ManagementPolicyConfig("OVERRIDE", "2", "CONTEXT_STRUCTURE_RETRACE_V1")
        registry.register_instrument_override("CONTEXT_STRUCTURE_RETRACE_V1", "XAUUSDm", override)
        self.assertEqual(registry.resolve("CONTEXT_STRUCTURE_RETRACE_V1", "XAUUSDm").policy_id, "OVERRIDE")

    def test_missing_policy_holds(self):
        decision = Phase2TradeManager(PolicyRegistry()).evaluate(position(), observation(), timestamp="t")
        self.assertEqual(decision["action"], "HOLD")
        self.assertEqual(decision["reason_codes"], ["NO_MANAGEMENT_POLICY"])

    def test_r_trailing_improves_only(self):
        p = position("LONG"); o = observation("LONG")
        proposal = evaluate_trailing(p, o, {"enabled": True, "method": "R_BASED", "activation_r": .5, "locked_r": .25})
        self.assertIsNotNone(proposal); self.assertGreater(proposal.proposed_stop, proposal.old_stop)
        self.assertIsNone(evaluate_trailing({**p, "current_stop": 115}, o, {"enabled": True, "method": "R_BASED", "activation_r": .5, "locked_r": .25}))

    def test_structure_ema_and_atr_methods(self):
        p = position("LONG"); o = {**observation("LONG"), "atr": 1.0}
        for method, cfg in (("STRUCTURE", {"enabled": True, "method": "STRUCTURE", "buffer": .2}),
                            ("EMA_STRUCTURE", {"enabled": True, "method": "EMA_STRUCTURE", "buffer": .2}),
                            ("ATR_STRUCTURE", {"enabled": True, "method": "ATR_STRUCTURE", "atr_multiple": 1.0})):
            self.assertIsNotNone(evaluate_trailing(p, o, cfg), method)

    def test_state_restart_and_policy_version(self):
        with tempfile.TemporaryDirectory() as d:
            store = ManagementStateStore(f"{d}/state.jsonl")
            state = ManagementState("P", policy_id="POL", policy_version="2")
            state.transition({"action": "HOLD", "current_R": 0}, "t"); store.update(state)
            recovered = ManagementStateStore(f"{d}/state.jsonl")
            self.assertEqual(recovered.states["P"].policy_version, "2")

    def test_mfe_surrender_capture_and_counterfactual_isolated(self):
        metrics = mfe_surrender(1.3, .75)
        self.assertAlmostEqual(metrics["MFE_SURRENDER_R"], .55)
        self.assertAlmostEqual(metrics["mfe_capture_ratio"], .75 / 1.3)
        cf = CounterfactualPosition("P", frozen={"realized_R": -1})
        cf.observe({"current_R": .75, "mfe_R": 1.3, "mae_R": .2, "time_in_trade_seconds": 10})
        cf.apply_shadow({"action": "ADD_POSITION"})
        self.assertTrue(cf.additions[0]["portfolio_authorization_required"])
        self.assertAlmostEqual(cf.comparison()["managed_hypothetical"]["MFE_SURRENDER_R"], .55)

    def test_phase2_custom_trailing_is_advisory(self):
        cfg = context_v1_experiment()
        cfg = ManagementPolicyConfig(cfg.policy_id, cfg.version, "S",
                                     trailing={"enabled": True, "method": "R_BASED", "activation_r": .5, "locked_r": .25})
        reg = PolicyRegistry(); reg.register_strategy_default(cfg)
        decision = Phase2TradeManager(reg).evaluate(position(), observation(), timestamp="t")
        self.assertEqual(decision["action"], "TRAIL_STOP")
        self.assertTrue(decision["advisory_only"])
        self.assertEqual(decision["authorization_mode"], "ADVISORY_SHADOW")


if __name__ == "__main__":
    unittest.main()
