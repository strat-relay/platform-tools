"""platform_api/v2_risk.py + PlatformControlApi's one mutation route (mission
CLAUDE-V2-RISK-EXECUTION-CONSOLE section 10, 18): read, update valid, reject invalid, no secret
exposure, policy.enabled independent of execution authority, and the READ_ONLY_API guard stays
enforced for every other path/method.

Exercises the real relational schema end-to-end (execution_v2.risk_policy + its
risk_policy_allowed_{account,strategy,symbol} child tables - the DB-authority cutover, platform
commit 7ecabba) via the same fake shape tests/test_execution_v2_risk_policy_store.py uses,
rather than mocking read_effective_policy_record/write_policy_override out entirely - this is
the one place platform_api/v2_risk.py's own body-parsing/merge/revision-echo logic is proven
against something that behaves like the real store.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

_SCALAR_COLUMNS = ("version", "enabled", "risk_per_trade", "max_volume", "max_signal_age_seconds",
                   "max_daily_loss", "max_concurrent_positions", "max_concurrent_orders",
                   "max_account_exposure", "duplicate_position_policy", "canary_max_new_executions")


class FakeCursor:
    """Matches the exact SQL text execution_v2/risk_policy_store.py issues - see that file's
    `_read_row`/`persist_policy`, and tests/test_execution_v2_risk_policy_store.py's own fake,
    which this mirrors."""

    def __init__(self, conn: "FakeConnection") -> None:
        self.conn = conn
        self._result = None
        self._results: list[tuple] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        upper = " ".join(sql.split()).upper()
        if upper.startswith("SELECT POLICY_ID, REVISION, VERSION"):
            row = self.conn.row
            self._result = None if row is None else (
                row["policy_id"], row["revision"], *[row[c] for c in _SCALAR_COLUMNS], row["updated_at"])
        elif upper.startswith("SELECT ACCOUNT_ID FROM"):
            self._results = [(a,) for a in sorted(self.conn.allowed_accounts)]
        elif upper.startswith("SELECT STRATEGY_REF FROM"):
            self._results = [(s,) for s in sorted(self.conn.allowed_strategies)]
        elif upper.startswith("SELECT SYMBOL FROM"):
            self._results = [(s,) for s in sorted(self.conn.allowed_symbols)]
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY "):
            (revision, version, enabled, risk_per_trade, max_volume, max_signal_age_seconds,
             max_daily_loss, max_concurrent_positions, max_concurrent_orders, max_account_exposure,
             duplicate_position_policy, canary_max_new_executions, updated_by) = params
            self.conn.row = {
                "policy_id": "current", "revision": revision, "version": version, "enabled": enabled,
                "risk_per_trade": risk_per_trade, "max_volume": max_volume,
                "max_signal_age_seconds": max_signal_age_seconds, "max_daily_loss": max_daily_loss,
                "max_concurrent_positions": max_concurrent_positions,
                "max_concurrent_orders": max_concurrent_orders, "max_account_exposure": max_account_exposure,
                "duplicate_position_policy": duplicate_position_policy,
                "canary_max_new_executions": canary_max_new_executions, "updated_at": None,
            }
        elif upper.startswith("DELETE FROM EXECUTION_V2.RISK_POLICY_ALLOWED_ACCOUNT"):
            self.conn.allowed_accounts = set()
        elif upper.startswith("DELETE FROM EXECUTION_V2.RISK_POLICY_ALLOWED_STRATEGY"):
            self.conn.allowed_strategies = set()
        elif upper.startswith("DELETE FROM EXECUTION_V2.RISK_POLICY_ALLOWED_SYMBOL"):
            self.conn.allowed_symbols = set()
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_ALLOWED_ACCOUNT"):
            self.conn.allowed_accounts.add(params[0])
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_ALLOWED_STRATEGY"):
            self.conn.allowed_strategies.add(params[0])
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_ALLOWED_SYMBOL"):
            self.conn.allowed_symbols.add(params[0])
        elif upper.startswith("INSERT INTO EXECUTION_V2.RISK_POLICY_CHANGE"):
            pass
        elif "FROM PLATFORM.INSTRUMENT_PROVIDER_MAPPING" in upper:
            symbol = self.conn.provider_mappings.get(params[0])
            self._result = (symbol,) if symbol else None
        elif "FROM EXECUTION_V2.CANARY_STATE" in upper:
            row = self.conn.canary_rows.get(params[0])
            if "LIFECYCLE_STATE" in upper and row is not None:
                self._results = [(params[0], 1, "ACTIVE", row[0], row[1])]
            else:
                self._result = row
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._results


class FakeConnection:
    def __init__(self, *, policy: dict | None = None, revision: int = 1, canary_rows: dict | None = None):
        if policy is None:
            self.row = None
            self.allowed_accounts: set = set()
            self.allowed_strategies: set = set()
            self.allowed_symbols: set = set()
        else:
            self.row = {"policy_id": "current", "revision": revision,
                       **{col: policy[col] for col in _SCALAR_COLUMNS}, "updated_at": None}
            self.allowed_accounts = set(policy["allowed_accounts"])
            self.allowed_strategies = set(policy["allowed_strategies"])
            self.allowed_symbols = set(policy["allowed_symbols"])
        self.canary_rows = canary_rows or {}
        # platform.instrument_provider_mapping (ACTIVE, MT5)
        self.provider_mappings = {"XAUUSD": "XAUUSDm", "BTCUSD": "BTCUSDm", "EURUSD": "EURUSDm",
                                  "USDJPY": "USDJPYm", "ETHUSD": "ETHUSDm", "GBPUSD": "GBPUSDm"}

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def make_api(*, policy=None, revision=1, environ=None):
    conn = FakeConnection(policy=policy, revision=revision)
    def connect_fn(readonly=False):
        return conn
    test_environ = environ or {"EXECUTION_AUTHORITY_MODE": "DISABLED", "V2_EXECUTION_BRIDGE_MODE": "demo"}
    v2_api = V2RiskExecutionApi(connect_fn, environ=test_environ)
    td = tempfile.TemporaryDirectory()
    config = Path(td.name) / "platform.json"
    config.write_text(json.dumps({"strategies": []}))
    api = PlatformControlApi(environ=test_environ,
                             strategy_config_path=str(config), v2_risk_api=v2_api)
    return api, conn


class ReadTests(unittest.TestCase):
    def test_read_returns_the_effective_policy_and_execution_authority_mode(self):
        api, _ = make_api(policy=VALID_POLICY)
        status, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["riskPerTrade"], 0.005)
        self.assertEqual(body["data"]["executionAuthorityMode"], "DISABLED")
        self.assertEqual(body["data"]["source"], "POSTGRES")
        self.assertEqual(body["data"]["revision"], 1)

    def test_account_is_masked_never_the_raw_account_id(self):
        api, _ = make_api(policy=VALID_POLICY)
        _, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        [masked] = body["data"]["allowedAccounts"]
        self.assertNotEqual(masked, "188428665")
        self.assertTrue(masked.endswith("8665"))
        self.assertNotIn("188428665", json.dumps(body))

    def test_no_secret_or_credential_field_anywhere_in_the_response(self):
        api, _ = make_api(policy=VALID_POLICY)
        _, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        dumped = json.dumps(body).lower()
        for forbidden in ("password", "secret", "token", "api_key", "credential"):
            self.assertNotIn(forbidden, dumped)

    def test_canary_status_is_present_and_reflects_the_real_counter(self):
        api, conn = make_api(policy=VALID_POLICY)
        conn.canary_rows["demo"] = (1, 0)
        _, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        self.assertEqual(body["data"]["canary"]["maxNewExecutions"], 1)
        self.assertEqual(body["data"]["canary"]["remaining"], 1)

    def test_a_missing_canonical_policy_is_unavailable_not_a_crash(self):
        api, _ = make_api(policy=None)
        status, body = api.execute("GET", "/api/v1/v2-execution/risk-policy")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "POLICY_UNAVAILABLE")


class SaveTests(unittest.TestCase):
    def test_a_valid_update_is_accepted_and_echoed_back(self):
        api, conn = make_api(policy=VALID_POLICY)
        payload = json.dumps({"revision": 1, "riskPerTrade": 0.01}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["riskPerTrade"], 0.01)
        self.assertEqual(body["data"]["revision"], 2)

        self.assertEqual(conn.row["risk_per_trade"], 0.01)

    def test_a_missing_revision_is_a_409_not_a_validation_error(self):
        api, _ = make_api(policy=VALID_POLICY)
        payload = json.dumps({"riskPerTrade": 0.01}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "REVISION_REQUIRED")

    def test_a_stale_revision_is_a_409_revision_conflict(self):
        api, conn = make_api(policy=VALID_POLICY, revision=5)
        payload = json.dumps({"revision": 3, "riskPerTrade": 0.01}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "REVISION_CONFLICT")
        self.assertEqual(conn.row["risk_per_trade"], 0.005)  # unchanged

    def test_an_invalid_update_is_rejected_with_a_clear_message_and_never_written(self):
        api, conn = make_api(policy=VALID_POLICY)
        payload = json.dumps({"revision": 1, "riskPerTrade": -5}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "POLICY_VALIDATION_FAILED")
        self.assertIn("risk_per_trade", body["message"])
        self.assertEqual(conn.row["risk_per_trade"], 0.005)  # unchanged
        self.assertEqual(conn.row["revision"], 1)  # unchanged

    def test_a_partial_update_never_touches_fields_it_did_not_mention(self):
        api, conn = make_api(policy=VALID_POLICY)
        payload = json.dumps({"revision": 1, "maxDailyLoss": 20}).encode()
        api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(conn.row["max_daily_loss"], 20)
        self.assertEqual(conn.row["risk_per_trade"], 0.005)  # carried over, unchanged
        self.assertEqual(conn.row["canary_max_new_executions"], 1)

    def test_account_identity_can_never_be_changed_through_this_endpoint(self):
        api, conn = make_api(policy=VALID_POLICY)
        payload = json.dumps({"revision": 1, "allowedAccounts": ["999999999"]}).encode()
        api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(conn.allowed_accounts, {"188428665"})  # untouched

    def test_enabling_the_policy_never_touches_execution_authority_mode(self):
        api, conn = make_api(policy=dict(VALID_POLICY, enabled=False),
                             environ={"EXECUTION_AUTHORITY_MODE": "DISABLED"})
        payload = json.dumps({"revision": 1, "enabled": True}).encode()
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["enabled"])
        self.assertEqual(body["data"]["executionAuthorityMode"], "DISABLED")

    def test_malformed_json_body_is_rejected_cleanly(self):
        api, _ = make_api(policy=VALID_POLICY)
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "INVALID_JSON")

    def test_empty_body_is_rejected_cleanly(self):
        api, _ = make_api(policy=VALID_POLICY)
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy", None)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "INVALID_REQUEST_BODY")


class AllowedSymbolTests(unittest.TestCase):
    def test_a_catalog_instrument_can_be_allowed_for_live_trading(self):
        api, conn = make_api(policy=VALID_POLICY)
        symbols = sorted(set(VALID_POLICY["allowed_symbols"]) | {"ETHUSD", "gbpusd"})
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy",
                                   json.dumps({"revision": 1, "allowedSymbols": symbols}).encode())
        self.assertEqual(status, 200, body)
        self.assertTrue({"ETHUSD", "GBPUSD"} <= conn.allowed_symbols)   # canonical, upper-cased

    def test_an_instrument_without_an_active_mapping_is_refused_and_nothing_written(self):
        api, conn = make_api(policy=VALID_POLICY)
        before = set(conn.allowed_symbols)
        status, body = api.execute("POST", "/api/v1/v2-execution/risk-policy",
                                   json.dumps({"revision": 1, "allowedSymbols": [*before, "ADAUSD"]}).encode())
        self.assertEqual((status, body["error"]), (400, "SYMBOL_NOT_TRADABLE"))
        self.assertIn("ADAUSD", body["message"])
        self.assertEqual((conn.allowed_symbols, conn.row["revision"]), (before, 1))

    def test_already_allowed_symbols_are_never_re_validated(self):
        api, conn = make_api(policy=VALID_POLICY)
        conn.provider_mappings = {}
        status, _ = api.execute("POST", "/api/v1/v2-execution/risk-policy",
                                json.dumps({"revision": 1, "riskPerTrade": 0.01}).encode())
        self.assertEqual(status, 200)


class ReadOnlyGuardTests(unittest.TestCase):
    def test_post_to_any_other_path_is_still_rejected(self):
        api, _ = make_api(policy=VALID_POLICY)
        status, body = api.execute("POST", "/api/v1/system", b"{}")
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "READ_ONLY_API")

    def test_put_to_the_risk_policy_path_is_rejected_only_post_is_accepted(self):
        api, _ = make_api(policy=VALID_POLICY)
        status, _ = api.execute("PUT", "/api/v1/v2-execution/risk-policy", b"{}")
        self.assertEqual(status, 405)

    def test_get_still_works_for_every_pre_existing_route(self):
        api, _ = make_api(policy=VALID_POLICY)
        status, _ = api.execute("GET", "/healthz")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()


class CanaryWindowRouteTests(unittest.TestCase):
    def test_open_window_route_is_explicit_and_does_not_arm(self):
        api, _ = make_api(policy=VALID_POLICY)
        window = {"canary_key": "execution:real:188428665:g2", "generation": 2,
                  "state": "ACTIVE", "max_new_executions": 1, "consumed": 0,
                  "remaining": 1}
        with patch("platform_api.v2_risk.open_canary_window", return_value=window):
            status, body = api.execute("POST", "/api/v1/v2-execution/canary-windows",
                                       json.dumps({"accountId": "188428665", "environment": "real",
                                                   "maxNewExecutions": 1, "updatedBy": "operator"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["generation"], 2)
        self.assertEqual(body["data"]["state"], "ACTIVE")
        self.assertEqual(api.execute("GET", "/api/v1/v2-execution/authority")[1]["data"]["state"], "DISABLED")
