"""Canonical, preflight-gated operator control for global V2 execution authority."""
from __future__ import annotations

import json
from typing import Any, Callable

from execution_v2.authority_store import DISABLED, ENABLED, read_authority, set_authority
from execution_v2.risk import RiskPolicyError
from execution_v2.risk_policy_store import read_effective_policy_record
from postgres.db import connect


class ExecutionAuthorityApi:
    def __init__(self, connect_fn: Callable[..., Any] = connect,
                 runtime_status_fn: Callable[[], dict[str, Any]] | None = None):
        self._connect = connect_fn
        self._runtime_status_fn = runtime_status_fn

    def read(self) -> tuple[int, dict[str, Any]]:
        authority = read_authority(self._connect)
        return 200, {"api_version": "v1", "source": "execution_authority",
                     "status": "ACTIVE", "degraded": False, "read_only": False,
                     "data": authority, "unavailable": []}

    def _runtime_status(self) -> dict[str, Any]:
        if self._runtime_status_fn is not None:
            return self._runtime_status_fn()
        with self._connect(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT status, metadata FROM platform.runtime_instances
                              WHERE component='execution_v2'
                              ORDER BY last_heartbeat_at DESC, instance_id ASC LIMIT 1""")
                row = cur.fetchone()
        if not row:
            return {}
        return {"status": row[0], **(row[1] or {})}

    @staticmethod
    def _preflight_reasons(checks: list[dict[str, Any]]) -> list[str]:
        """Convert failed checks into stable operator-facing reasons.

        Older/runtime-generated checks are allowed to contain only ``name``
        and ``passed``.  Preflight reporting must never turn that valid
        failure shape into an API exception while constructing the response.
        """
        return [str(check.get("detail") or check.get("name") or "preflight_check_failed")
                for check in checks if not check.get("passed")]

    def _preflight(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            policy, _ = read_effective_policy_record(self._connect)
            checks.append({"name": "canonical_risk_policy", "passed": True})
            checks.append({"name": "risk_policy_enabled", "passed": bool(policy.enabled),
                           "detail": "policy.enabled must be true" if not policy.enabled else None})
            account_id = policy.allowed_accounts[0] if policy.allowed_accounts else None
        except Exception as exc:
            checks.append({"name": "canonical_risk_policy", "passed": False, "detail": str(exc)})
            return {"passed": False, "checks": checks, "reasons": self._preflight_reasons(checks)}
        runtime = self._runtime_status()
        bridge_ok = runtime.get("status") == "RUNNING" and (runtime.get("execution_bridge") or {}).get("status") == "HEALTHY"
        broker_ok = (runtime.get("broker_account") or {}).get("status") == "CONNECTED"
        account_ok = str(runtime.get("account_id") or "") == str(account_id or "")
        checks.extend([
            {"name": "execution_runtime", "passed": runtime.get("status") == "RUNNING"},
            {"name": "execution_bridge", "passed": bridge_ok, "detail": "bridge health is not HEALTHY" if not bridge_ok else None},
            {"name": "broker_account", "passed": broker_ok, "detail": "broker account is not CONNECTED" if not broker_ok else None},
            {"name": "allowed_account", "passed": account_ok, "detail": "runtime account is not allowed by policy" if not account_ok else None},
            {"name": "broker_held_fence", "passed": bridge_ok, "detail": "fence endpoint unavailable" if not bridge_ok else None},
        ])
        reasons = self._preflight_reasons(checks)
        return {"passed": not reasons, "checks": checks, "reasons": reasons}

    def save(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            submitted = json.loads((body or b"").decode())
            state = submitted.get("state")
            revision = submitted.get("expectedRevision")
            if state not in (DISABLED, ENABLED) or isinstance(revision, bool) or not isinstance(revision, int):
                raise ValueError("state and integer expectedRevision are required")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            return 400, self._error("INVALID_REQUEST", str(exc))
        preflight = {"passed": True, "checks": [], "reasons": []}
        if state == ENABLED:
            preflight = self._preflight()
            if not preflight["passed"]:
                return 409, self._error("PREFLIGHT_FAILED", "execution authority remains DISABLED", preflight)
        try:
            data = set_authority(state, expected_revision=revision,
                                 changed_by=submitted.get("updatedBy") or "console",
                                 preflight=preflight, connect_fn=self._connect)
        except RiskPolicyError as exc:
            return 409, self._error("AUTHORITY_CONFLICT", str(exc))
        return 200, {"api_version": "v1", "source": "execution_authority", "status": "ACTIVE",
                     "degraded": False, "read_only": False, "data": data, "unavailable": []}

    @staticmethod
    def _error(code: str, message: str, preflight: dict[str, Any] | None = None) -> dict[str, Any]:
        out = {"api_version": "v1", "source": "execution_authority", "status": "UNAVAILABLE",
               "degraded": False, "read_only": False, "error": code, "message": message,
               "unavailable": [{"code": code, "source": "execution_authority", "message": message}]}
        if preflight is not None:
            out["preflight"] = preflight
        return out
