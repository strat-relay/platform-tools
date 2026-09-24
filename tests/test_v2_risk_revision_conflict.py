import json
import unittest
from unittest.mock import patch

from execution_v2.risk import RiskPolicy, RiskPolicyError, RiskPolicyRevisionConflict
from execution_v2.risk_policy_store import persist_policy
from platform_api.v2_risk import V2RiskExecutionApi


VALID_POLICY = {
    "version": 1,
    "enabled": True,
    "max_volume": 0.01,
    "allowed_symbols": ["XAUUSD"],
    "allowed_accounts": ["188428665"],
    "allowed_strategies": ["CONTEXT_STRUCTURE_RETRACE_V1@V1"],
    "risk_per_trade": 0.02,
    "max_signal_age_seconds": 60,
    "max_daily_loss": 100,
    "max_concurrent_positions": 1,
    "max_concurrent_orders": 1,
    "max_account_exposure": 50,
    "duplicate_position_policy": "REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY",
    "canary_max_new_executions": 1,
}


def policy() -> RiskPolicy:
    return RiskPolicy(
        version=1, enabled=True, max_volume=0.01, allowed_symbols=("XAUUSD",),
        allowed_accounts=("188428665",), max_signal_age_seconds=60, source="POSTGRES",
        allowed_strategies=("CONTEXT_STRUCTURE_RETRACE_V1@V1",), risk_per_trade=0.02,
        max_daily_loss=100, max_concurrent_positions=1, max_concurrent_orders=1,
        max_account_exposure=50,
        duplicate_position_policy="REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY",
        canary_max_new_executions=1,
    )


META = {"policy_id": "current", "revision": 8, "updated_at": None,
        "source": "POSTGRES", "fingerprint": "fingerprint"}


class NoMutationConnection:
    def __init__(self):
        self.cursor_calls = 0
        self.rolled_back = False

    def cursor(self):
        self.cursor_calls += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        raise AssertionError("stale revision must be rejected before any write")

    def commit(self):
        raise AssertionError("stale revision must not commit")

    def rollback(self):
        self.rolled_back = True


class StoreTests(unittest.TestCase):
    def test_stale_revision_raises_specific_conflict_before_mutation(self):
        conn = NoMutationConnection()
        with patch("execution_v2.risk_policy_store._read_row",
                   return_value=({"revision": 8}, "current")):
            with self.assertRaises(RiskPolicyRevisionConflict):
                persist_policy(VALID_POLICY, expected_revision=7,
                               connect_fn=lambda readonly=False: conn)
        self.assertEqual(conn.cursor_calls, 1)
        self.assertTrue(conn.rolled_back)


class ApiTests(unittest.TestCase):
    def make_api(self):
        return V2RiskExecutionApi(environ={"EXECUTION_AUTHORITY_MODE": "DISABLED"})

    def test_current_revision_succeeds(self):
        api = self.make_api()
        with patch("platform_api.v2_risk.read_effective_policy_record",
                   side_effect=[(policy(), META), (policy(), {**META, "revision": 9})]), \
             patch("platform_api.v2_risk.write_policy_override", return_value=policy()):
            status, body = api.save(json.dumps({"revision": 8, "maxSignalAgeSeconds": 61}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["revision"], 9)

    def test_stale_revision_is_409_and_not_validation_error(self):
        api = self.make_api()
        with patch("platform_api.v2_risk.read_effective_policy_record", return_value=(policy(), META)), \
             patch("platform_api.v2_risk.write_policy_override",
                   side_effect=RiskPolicyRevisionConflict("stale policy revision")) as write:
            status, body = api.save(json.dumps({"revision": 7, "maxSignalAgeSeconds": 61}).encode())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "REVISION_CONFLICT")
        self.assertNotEqual(body["error"], "POLICY_VALIDATION_FAILED")
        write.assert_called_once()

    def test_invalid_policy_remains_400(self):
        api = self.make_api()
        with patch("platform_api.v2_risk.read_effective_policy_record", return_value=(policy(), META)), \
             patch("platform_api.v2_risk.write_policy_override",
                   side_effect=RiskPolicyError("risk_per_trade must be greater than zero")):
            status, body = api.save(json.dumps({"revision": 8, "riskPerTrade": -1}).encode())
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "POLICY_VALIDATION_FAILED")

    def test_missing_revision_preserves_existing_409(self):
        api = self.make_api()
        with patch("platform_api.v2_risk.read_effective_policy_record", return_value=(policy(), META)):
            status, body = api.save(json.dumps({"maxSignalAgeSeconds": 61}).encode())
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "REVISION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
