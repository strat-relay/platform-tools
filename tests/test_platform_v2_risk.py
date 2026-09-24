"""platform_api/v2_risk.py + PlatformControlApi's one mutation route (mission
CLAUDE-V2-RISK-EXECUTION-CONSOLE section 10, 18): read, update valid, reject invalid, no secret
exposure, policy.enabled independent of execution authority, and the READ_ONLY_API guard stays
enforced for every other path/method."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from platform_api.control import PlatformControlApi
from platform_api.v2_risk import V2RiskExecutionApi

VALID_POLICY = {
    "version": 1, "enabled": True, "max_volume": 0.01,
    "allowed_symbols": ["XAUUSD", "BTCUSD", "USDJPY", "EURUSD"],
    "allowed_accounts": ["188428665"], "allowed_strategies": ["CONTEXT_STRUCTURE_RETRACE_V1@V1"],
    "risk_per_trade": 0.005, "max_signal_age_seconds": 60, "max_daily_loss": 10,
    "max_concurrent_positions": 1, "max_concurrent_orders": 1, "max_account_exposure": 50,
    "duplicate_position_policy": "REJECT_SAME_ACCOUNT_SYMBOL_DIRECTION_STRATEGY",
    "canary_max_new_executions": 1,
}


class FakeCursor:
    def __init__(self, conn): self.conn = conn; self._result = None
    def __enter__(self): return self
    def __exit__(self, *exc): return False

    def execute(self, sql, params=()):
        upper = " ".join(sql.split()).upper()
        if "SELECT POLICY_JSON FROM EXECUTION_V2.RISK_POLICY_OVERRIDE" in upper:
            self._result = (self.conn.override,) if self.conn.override is not None else None
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_OVERRIDE"):
            self.conn.override = json.loads(params[0])
        elif "SELECT MAX_NEW_EXECUTIONS, CONSUMED FROM EXECUTION_V2.CANARY_STATE" in upper:
            self._result = self.conn.canary_rows.get(params[0])
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self): return self._result


class FakeConnection:
    def __init__(self, *, override=None, canary_rows=None):
        self.override = override
        self.canary_rows = canary_rows or {}
    def cursor(self): return FakeCursor(self)
    def commit(self): pass
    def rollback(self): pass
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def make_api(*, override=None, environ=None):
    conn = FakeConnection(override=override)
    def connect_fn(readonly=False):
        return conn
    v2_api = V2RiskExecutionApi(connect_fn, environ=environ or {"EXECUTION_AUTHORITY_MODE": "DISABLED"})
    td = tempfile.TemporaryDirectory()
    config = Path(td.name) / "platform.json"
    config.write_text(json.dumps({"strategies": []}))
    return PlatformControlApi(environ=environ or {"EXECUTION_AUTHORITY_MODE": "DISABLED"},
                              strategy_config_path=str(config), v2_risk_api=v2_api), conn


class ReadTests(unittest.TestCase):
    def test_read_returns_the_effective_policy_and_execution_authority_mode(self):
        api, _ = make_api(override=VALID_POLICY)
        status, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["riskPerTrade"], 0.005)
        self.assertEqual(body["data"]["executionAuthorityMode"], "DISABLED")
        self.assertEqual(body["data"]["source"], "postgres_override")

    def test_account_is_masked_never_the_raw_account_id(self):
        api, _ = make_api(override=VALID_POLICY)
        _, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        [masked] = body["data"]["allowedAccounts"]
        self.assertNotEqual(masked, "188428665")
        self.assertTrue(masked.endswith("8665"))
        self.assertNotIn("188428665", json.dumps(body))

    def test_no_secret_or_credential_field_anywhere_in_the_response(self):
        api, _ = make_api(override=VALID_POLICY)
        _, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        dumped = json.dumps(body).lower()
        for forbidden in ("password", "secret", "token", "api_key", "credential"):
            self.assertNotIn(forbidden, dumped)

    def test_canary_status_is_present_and_reflects_the_real_counter(self):
        conn_override = dict(VALID_POLICY)
        api, conn = make_api(override=conn_override)
        conn.canary_rows["execution:demo:188428665"] = (1, 0)
        _, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        self.assertEqual(body["data"]["canary"]["maxNewExecutions"], 1)
        self.assertEqual(body["data"]["canary"]["remaining"], 1)


class SaveTests(unittest.TestCase):
    def test_a_valid_update_is_accepted_and_echoed_back(self):
        api, conn = make_api(override=VALID_POLICY)
        payload = json.dumps({"riskPerTrade": 0.01}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["riskPerTrade"], 0.01)
        self.assertEqual(conn.override["risk_per_trade"], 0.01)

    def test_an_invalid_update_is_rejected_with_a_clear_message_and_never_written(self):
        api, conn = make_api(override=VALID_POLICY)
        payload = json.dumps({"riskPerTrade": -5}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "POLICY_VALIDATION_FAILED")
        self.assertIn("risk_per_trade", body["message"])
        self.assertEqual(conn.override["risk_per_trade"], 0.005)  # unchanged

    def test_a_partial_update_never_touches_fields_it_did_not_mention(self):
        api, conn = make_api(override=VALID_POLICY)
        payload = json.dumps({"maxDailyLoss": 20}).encode()
        api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(conn.override["max_daily_loss"], 20)
        self.assertEqual(conn.override["risk_per_trade"], 0.005)  # carried over, unchanged
        self.assertEqual(conn.override["canary_max_new_executions"], 1)

    def test_account_identity_can_never_be_changed_through_this_endpoint(self):
        api, conn = make_api(override=VALID_POLICY)
        payload = json.dumps({"allowedAccounts": ["999999999"]}).encode()
        api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(conn.override["allowed_accounts"], ["188428665"])  # untouched

    def test_enabling_the_policy_never_touches_execution_authority_mode(self):
        api, conn = make_api(override=dict(VALID_POLICY, enabled=False),
                             environ={"EXECUTION_AUTHORITY_MODE": "DISABLED"})
        payload = json.dumps({"enabled": True}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["enabled"])
        self.assertEqual(body["data"]["executionAuthorityMode"], "DISABLED")

    def test_malformed_json_body_is_rejected_cleanly(self):
        api, _ = make_api(override=VALID_POLICY)
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "INVALID_JSON")

    def test_empty_body_is_rejected_cleanly(self):
        api, _ = make_api(override=VALID_POLICY)
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", None)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "INVALID_REQUEST_BODY")


class ReadOnlyGuardTests(unittest.TestCase):
    def test_post_to_any_other_path_is_still_rejected(self):
        api, _ = make_api(override=VALID_POLICY)
        status, body = api.execute("POST", "/api/v1/system", b"{}")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "READ_ONLY_API")

    def test_put_to_the_risk_policy_path_is_rejected_only_post_is_accepted(self):
        api, _ = make_api(override=VALID_POLICY)
        status, _ = api.execute("PUT", "/api/v1/v2-execution/risk-policy", b"{}")
        self.assertEqual(status, 405)

    def test_get_still_works_for_every_pre_existing_route(self):
        api, _ = make_api(override=VALID_POLICY)
        status, _ = api.execute("GET", "/healthz")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
