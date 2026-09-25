from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from platform_api.execution_authority import ExecutionAuthorityApi


def enabled_policy():
    return SimpleNamespace(enabled=True, allowed_accounts=("188428665",))


def healthy_runtime():
    return {
        "status": "RUNNING",
        "account_id": "188428665",
        "execution_bridge": {"status": "HEALTHY"},
        "broker_account": {"status": "CONNECTED"},
    }


def arm_body(revision: int = 1) -> bytes:
    return json.dumps({"state": "ENABLED", "expectedRevision": revision,
                       "updatedBy": "test"}).encode()


def test_failed_check_with_detail_is_reported_without_crashing():
    reasons = ExecutionAuthorityApi._preflight_reasons([
        {"name": "bridge", "passed": False, "detail": "bridge unavailable"},
    ])
    assert reasons == ["bridge unavailable"]


def test_failed_check_without_detail_falls_back_to_name():
    reasons = ExecutionAuthorityApi._preflight_reasons([
        {"name": "execution_runtime", "passed": False},
    ])
    assert reasons == ["execution_runtime"]


def test_multiple_failed_checks_preserve_all_reasons():
    reasons = ExecutionAuthorityApi._preflight_reasons([
        {"name": "execution_runtime", "passed": False},
        {"name": "execution_bridge", "passed": False, "detail": "bridge unavailable"},
        {"name": "broker_account", "passed": True},
    ])
    assert reasons == ["execution_runtime", "bridge unavailable"]


def test_successful_preflight_allows_the_existing_authority_transition():
    api = ExecutionAuthorityApi(runtime_status_fn=healthy_runtime)
    with patch("platform_api.execution_authority.read_effective_policy_record",
               return_value=(enabled_policy(), {})), \
         patch("platform_api.execution_authority.set_authority",
               return_value={"state": "ENABLED", "revision": 2}) as set_authority:
        status, body = api.save(arm_body())

    assert status == 200
    assert body["data"]["state"] == "ENABLED"
    set_authority.assert_called_once()
    assert set_authority.call_args.kwargs["preflight"]["passed"] is True


def test_failed_preflight_is_structured_and_does_not_change_authority():
    api = ExecutionAuthorityApi(runtime_status_fn=lambda: {
        "status": "STOPPED",
        "account_id": "wrong-account",
        "execution_bridge": {"status": "DOWN"},
        "broker_account": {"status": "UNAVAILABLE"},
    })
    with patch("platform_api.execution_authority.read_effective_policy_record",
               return_value=(enabled_policy(), {})), \
         patch("platform_api.execution_authority.set_authority") as set_authority:
        status, body = api.save(arm_body())

    assert status == 409
    assert body["error"] == "PREFLIGHT_FAILED"
    assert body["preflight"]["passed"] is False
    assert "execution_runtime" in body["preflight"]["reasons"]
    assert "bridge health is not HEALTHY" in body["preflight"]["reasons"]
    assert "broker account is not CONNECTED" in body["preflight"]["reasons"]
    assert "runtime account is not allowed by policy" in body["preflight"]["reasons"]
    set_authority.assert_not_called()
