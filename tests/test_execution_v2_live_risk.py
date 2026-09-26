import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution_v2.risk import RiskPolicyError, evaluate_candidate, load_risk_policy
from execution_v2.fakes import FakeConnection
from execution_v2.intent import create_execution_intent


NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def valid_policy():
    return {
        "version": 2, "enabled": True,
        "allowed_accounts": ["188428665"],
        "allowed_strategies": ["STRAT@V1:param-a"],
        "allowed_symbols": ["EURUSD"],
        "risk_per_trade": 0.005, "max_volume": 0.05,
        "max_signal_age_seconds": 60, "max_daily_loss": 25,
        "max_concurrent_positions": 1, "max_concurrent_orders": 1,
        "max_account_exposure": 50,
        "duplicate_position_policy": "REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY",
        "canary_max_new_executions": 1,
    }


class LiveRiskPolicyTests(unittest.TestCase):
    def write_policy(self, raw):
        td = tempfile.TemporaryDirectory()
        path = Path(td.name) / "policy.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        self.addCleanup(td.cleanup)
        return load_risk_policy(path)

    def candidate(self, **overrides):
        value = {"strategy_id": "STRAT", "strategy_version": "V1", "strategy_ref": "STRAT@V1:param-a",
                 "instrument": "EURUSD", "entry_price": 1.10, "stop_price": 1.095,
                 "decision_time": NOW, "signal_emitted_at": NOW}
        value.update(overrides)
        return value

    def state(self, **overrides):
        value = {"daily_loss": 0, "concurrent_positions": 0, "concurrent_orders": 0,
                 "account_exposure": 0, "canary_used": 0}
        value.update(overrides)
        return value

    def test_enabled_policy_requires_every_bound(self):
        raw = valid_policy()
        for key in tuple(raw):
            if key not in {"version", "enabled"}:
                broken = dict(raw)
                broken[key] = None
                with self.subTest(key=key), self.assertRaises(RiskPolicyError):
                    self.write_policy(broken)

    def test_disabled_policy_is_explicitly_blocked(self):
        policy = self.write_policy({"version": 2, "enabled": False})
        self.assertFalse(policy.enabled)
        self.assertEqual(evaluate_candidate(self.candidate(), policy=policy, account_id="188428665",
                                             now_utc=NOW, broker={}, account={}, state={}).reason,
                         "RISK_POLICY_DISABLED")

    def test_strategy_and_symbol_are_default_deny(self):
        policy = self.write_policy(valid_policy())
        common = dict(policy=policy, account_id="188428665", now_utc=NOW,
                      broker={"tick_size": .00001, "tick_value": 1, "volume_min": .01,
                              "volume_max": 100, "volume_step": .01}, account={"equity": 1000}, state=self.state())
        self.assertEqual(evaluate_candidate(self.candidate(strategy_ref="STRAT@V2:new"), **common).reason,
                         "STRATEGY_NOT_ALLOWED")
        self.assertEqual(evaluate_candidate(self.candidate(instrument="XAUUSD"), **common).reason,
                         "SYMBOL_NOT_ALLOWED")

    def test_minimum_lot_does_not_round_up_past_risk(self):
        policy = self.write_policy(valid_policy())
        decision = evaluate_candidate(self.candidate(), policy=policy, account_id="188428665", now_utc=NOW,
                                      broker={"tick_size": .00001, "tick_value": 100, "volume_min": .01,
                                              "volume_max": 100, "volume_step": .01},
                                      account={"equity": 100}, state=self.state())
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.reason, "MINIMUM_LOT_EXCEEDS_RISK_LIMIT")

    def test_stale_and_unavailable_state_fail_closed(self):
        policy = self.write_policy(valid_policy())
        args = dict(policy=policy, account_id="188428665", broker={"tick_size": .00001, "tick_value": 1,
                    "volume_min": .01, "volume_max": 100, "volume_step": .01}, account={"equity": 1000})
        self.assertEqual(evaluate_candidate(self.candidate(), now_utc=NOW, state={}, **args).reason,
                         "RISK_STATE_UNAVAILABLE")
        self.assertEqual(evaluate_candidate(self.candidate(signal_emitted_at=NOW - timedelta(seconds=61)),
                                             now_utc=NOW, state=self.state(), **args).reason, "STALE_SIGNAL")

    def test_valid_candidate_is_sized_without_exceeding_cap(self):
        policy = self.write_policy(valid_policy())
        decision = evaluate_candidate(self.candidate(), policy=policy, account_id="188428665", now_utc=NOW,
                                      broker={"tick_size": .00001, "tick_value": 1, "volume_min": .01,
                                              "volume_max": 100, "volume_step": .01},
                                      account={"equity": 1000}, state=self.state())
        self.assertTrue(decision.permitted)
        self.assertLessEqual(decision.volume, .05)
        self.assertLessEqual(decision.risk_amount, 5.000001)

    def test_safety_limits_are_hard_rejections_but_canary_is_not_a_gate(self):
        policy = self.write_policy(valid_policy())
        args = dict(policy=policy, account_id="188428665", now_utc=NOW, broker={"tick_size": .00001,
                    "tick_value": 1, "volume_min": .01, "volume_max": 100, "volume_step": .01}, account={"equity": 1000})
        for field, reason in (("daily_loss", "DAILY_LOSS_LIMIT_EXCEEDED"),
                              ("concurrent_positions", "MAX_CONCURRENT_POSITIONS_EXCEEDED"),
                              ("concurrent_orders", "MAX_CONCURRENT_ORDERS_EXCEEDED"),
                              ("account_exposure", "MAX_ACCOUNT_EXPOSURE_EXCEEDED")):
            with self.subTest(field=field):
                self.assertEqual(evaluate_candidate(self.candidate(), state=self.state(**{field: 999}), **args).reason,
                                 reason)

        decision = evaluate_candidate(self.candidate(), state=self.state(canary_used=999), **args)
        self.assertTrue(decision.permitted)

    def test_missing_canary_counter_does_not_block_normal_risk_evaluation(self):
        policy = self.write_policy(valid_policy())
        args = dict(policy=policy, account_id="188428665", now_utc=NOW,
                    broker={"tick_size": .00001, "tick_value": 1, "volume_min": .01,
                            "volume_max": 100, "volume_step": .01}, account={"equity": 1000})
        state = {"daily_loss": 0, "concurrent_positions": 0, "concurrent_orders": 0,
                 "account_exposure": 0}
        self.assertTrue(evaluate_candidate(self.candidate(), state=state, **args).permitted)

    def test_intent_persists_evaluator_volume_not_flat_cap(self):
        policy = self.write_policy(valid_policy())
        conn = FakeConnection()
        conn.seed_entry_signal(signal_id="SIG-WIRED", strategy_id="STRAT", strategy_version="V1",
                               strategy_ref="STRAT@V1:param-a", instrument="EURUSD", direction="LONG",
                               decision_time=NOW, signal_emitted_at=NOW, entry_price=1.10, stop_price=1.095,
                               target_price=1.11, entry_signal_hash="hash")
        result = create_execution_intent(
            conn, signal_id="SIG-WIRED", account_id="188428665", risk_policy=policy, now_utc=NOW,
            risk_context_provider=lambda record: {"broker": {"tick_size": .00001, "tick_value": 1,
                "volume_min": .01, "volume_max": 100, "volume_step": .01},
                "account": {"equity": 2000}, "state": self.state()},
        )
        self.assertTrue(result.eligible)
        row = conn.tables["execution_v2.execution_intent"][result.execution_intent_id]
        self.assertEqual(row["approved_volume"], .02)
        self.assertEqual(row["risk_fraction"], .005)
        evidence = conn.tables["execution_v2.execution_risk_evidence"][result.execution_intent_id]
        self.assertEqual(evidence["policy_version"], policy.version)
        self.assertEqual(evidence["risk_per_trade"], .005)
        self.assertEqual(evidence["calculated_volume"], .02)
        self.assertEqual(evidence["account_equity"], 2000)

    def test_intent_rejects_when_broker_state_is_unavailable(self):
        policy = self.write_policy(valid_policy())
        conn = FakeConnection()
        conn.seed_entry_signal(signal_id="SIG-NO-STATE", strategy_id="STRAT", strategy_version="V1",
                               strategy_ref="STRAT@V1:param-a", instrument="EURUSD", direction="LONG",
                               decision_time=NOW, signal_emitted_at=NOW, entry_price=1.10, stop_price=1.095,
                               target_price=1.11, entry_signal_hash="hash")
        result = create_execution_intent(conn, signal_id="SIG-NO-STATE", account_id="188428665",
                                         risk_policy=policy, now_utc=NOW,
                                         risk_context_provider=lambda record: (_ for _ in ()).throw(RuntimeError("unavailable")))
        self.assertFalse(result.eligible)
        self.assertEqual(result.reason, "RISK_STATE_UNAVAILABLE")
        row = conn.tables["execution_v2.execution_intent"][result.execution_intent_id]
        self.assertEqual(row["status"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
