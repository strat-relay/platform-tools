"""The V2 Risk & Execution Console's ONE mutation endpoint (mission
`CLAUDE-V2-RISK-EXECUTION-CONSOLE` section 10: "implement the SMALLEST Platform API mutation
endpoint necessary"). Deliberately isolated in its own module, separate from
`platform_api/control.py`'s otherwise strictly read-only route table, so the one place this API
surface accepts a write is easy to find, read, and audit in full.

Never touches `EXECUTION_AUTHORITY_MODE` - that stays a Kubernetes Deployment env var, read-only
end to end (mission section 13). Writing the risk policy can only ever change whether the V2
risk EVALUATOR is permitted to approve a candidate; it cannot by itself move anything closer to
a broker write, because `EXECUTION_AUTHORITY_MODE` remains the separate, unconditional gate
`execution_v2/runtime/consumer.py` checks independently (mission section 19, `BROKER_WRITES=0`).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from execution_v2.canary_windows import CanaryWindowError, open_canary_window, read_active_canary_status
from execution_v2.risk import RiskPolicy, RiskPolicyError, RiskPolicyRevisionConflict
from execution_v2.risk_policy_store import (read_effective_policy_record, write_policy_override)
from postgres.db import connect

MAX_BODY_BYTES = 65_536  # a risk policy document is a few hundred bytes; this is generous, not tight


def _masked_account(account_id: str) -> str:
    return "•" * max(0, len(account_id) - 4) + account_id[-4:]


def _policy_to_wire(policy: RiskPolicy, *, source: str, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": policy.version,
        "enabled": policy.enabled,
        "allowedAccounts": [_masked_account(a) for a in policy.allowed_accounts],
        "allowedStrategies": list(policy.allowed_strategies),
        "allowedSymbols": list(policy.allowed_symbols or ()),
        "riskPerTrade": policy.risk_per_trade,
        "maxVolume": policy.max_volume,
        "maxSignalAgeSeconds": policy.max_signal_age_seconds,
        "maxDailyLoss": policy.max_daily_loss,
        "maxConcurrentPositions": policy.max_concurrent_positions,
        "maxConcurrentOrders": policy.max_concurrent_orders,
        "maxAccountExposure": policy.max_account_exposure,
        "duplicatePositionPolicy": policy.duplicate_position_policy,
        "canaryMaxNewExecutions": policy.canary_max_new_executions,
        "source": source,
        **({"policyId": provenance["policy_id"], "revision": provenance["revision"],
            "updatedAt": provenance["updated_at"].isoformat() if hasattr(provenance.get("updated_at"), "isoformat") else provenance.get("updated_at"),
            "policySource": provenance["source"], "fingerprint": provenance["fingerprint"]} if provenance else {}),
    }


def _wire_to_raw(body: dict[str, Any]) -> dict[str, Any]:
    """The inverse of `_policy_to_wire`'s field naming, for the request body only - never
    receives (or needs) `allowedAccounts` masked-for-display back, since account identity is not
    editable from this page (mission section 6: "a single connected account... for V1"); the
    account list is carried over from the currently effective policy, untouched, not from the
    request body at all (see `save_risk_policy`)."""
    mapping = {
        "version": "version", "enabled": "enabled", "allowedStrategies": "allowed_strategies",
        "allowedSymbols": "allowed_symbols", "riskPerTrade": "risk_per_trade", "maxVolume": "max_volume",
        "maxSignalAgeSeconds": "max_signal_age_seconds", "maxDailyLoss": "max_daily_loss",
        "maxConcurrentPositions": "max_concurrent_positions", "maxConcurrentOrders": "max_concurrent_orders",
        "maxAccountExposure": "max_account_exposure", "duplicatePositionPolicy": "duplicate_position_policy",
        "canaryMaxNewExecutions": "canary_max_new_executions",
    }
    return {snake: body[camel] for camel, snake in mapping.items() if camel in body}


class V2RiskExecutionApi:
    def __init__(self, connect_fn: Callable[..., Any] = connect, environ: dict[str, str] | None = None):
        self._connect = connect_fn
        self.environ = os.environ if environ is None else environ

    def execution_authority_mode(self) -> str:
        return self.environ.get("EXECUTION_AUTHORITY_MODE", "DISABLED")

    def read(self) -> tuple[int, dict[str, Any]]:
        try:
            policy, provenance = read_effective_policy_record(self._connect)
        except RiskPolicyError as exc:
            return 503, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                        "degraded": True, "error": "POLICY_UNAVAILABLE", "message": str(exc), "unavailable": [
                            {"code": "POLICY_UNAVAILABLE", "source": "execution_v2_risk_policy", "message": str(exc)}]}
        account_id = policy.allowed_accounts[0] if policy.allowed_accounts else None
        environment = self.environ.get("V2_EXECUTION_BRIDGE_MODE", "real").strip().lower()
        try:
            canary = (read_active_canary_status(self._connect, environment=environment,
                                                account_id=account_id) if account_id else None)
        except CanaryWindowError as exc:
            return 503, {"api_version": "v1", "source": "execution_v2_canary_window",
                         "status": "UNAVAILABLE", "degraded": True,
                         "error": "CANARY_WINDOW_UNAVAILABLE", "message": str(exc),
                         "unavailable": [{"code": "CANARY_WINDOW_UNAVAILABLE",
                                           "source": "execution_v2_canary_window", "message": str(exc)}]}
        data = _policy_to_wire(policy, source=provenance["source"], provenance=provenance)
        data["executionAuthorityMode"] = self.execution_authority_mode()
        data["canary"] = ({"key": canary["canary_key"], "generation": canary["generation"],
                           "state": canary["state"], "maxNewExecutions": canary["max_new_executions"],
                           "consumed": canary["consumed"], "remaining": canary["remaining"], "required": False}
                          if canary else {"key": None, "generation": None, "state": "NONE",
                                          "maxNewExecutions": policy.canary_max_new_executions,
                                          "consumed": 0, "remaining": 0, "required": False})
        return 200, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "ACTIVE",
                    "degraded": False, "read_only": False, "data": data, "unavailable": []}

    def open_window(self, body_bytes: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            body = json.loads((body_bytes or b"").decode("utf-8"))
            account_id = str(body.get("accountId") or "")
            environment = str(body.get("environment") or "").lower()
            maximum = body.get("maxNewExecutions")
            changed_by = str(body.get("updatedBy") or "")
            if isinstance(maximum, bool) or not isinstance(maximum, int):
                raise CanaryWindowError("maxNewExecutions must be an integer")
            policy, _ = read_effective_policy_record(self._connect)
            if account_id not in policy.allowed_accounts:
                raise CanaryWindowError("account is not allowed by the canonical policy")
            window = open_canary_window(account_id=account_id, environment=environment,
                                        max_new_executions=maximum, changed_by=changed_by,
                                        connect_fn=self._connect)
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, CanaryWindowError) as exc:
            return 400, {"api_version": "v1", "source": "execution_v2_canary_window",
                         "status": "UNAVAILABLE", "error": "CANARY_WINDOW_INVALID",
                         "message": str(exc), "unavailable": []}
        except Exception as exc:
            return 503, {"api_version": "v1", "source": "execution_v2_canary_window",
                         "status": "UNAVAILABLE", "degraded": True,
                         "error": "CANARY_WINDOW_UNAVAILABLE", "message": str(exc), "unavailable": []}
        return 200, {"api_version": "v1", "source": "execution_v2_canary_window",
                     "status": "ACTIVE", "degraded": False, "read_only": False,
                     "data": window, "unavailable": []}

    def _provider_symbol(self, canonical: str) -> str | None:
        with self._connect(readonly=True) as conn, conn.cursor() as cur:
            cur.execute("""SELECT provider_symbol FROM platform.instrument_provider_mapping
                           WHERE provider = 'MT5' AND canonical_instrument = %s AND state = 'ACTIVE'""", (canonical,))
            row = cur.fetchone()
        return row[0] if row else None

    def save(self, body_bytes: bytes | None) -> tuple[int, dict[str, Any]]:
        # 400 responses here deliberately never set "degraded": true - that field means "a
        # backend source is unavailable", not "the request you sent was invalid". Matches the
        # existing convention documented in src/api/envelope.ts's own docstring (a 400 example
        # carries no "degraded" key), so classifyEnvelope's "error" branch (-> BackendApiError),
        # not its "degraded" branch (-> DegradedDataError), is what the Console actually sees.
        if not body_bytes or len(body_bytes) > MAX_BODY_BYTES:
            return 400, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                        "error": "INVALID_REQUEST_BODY",
                        "message": "request body must be a non-empty JSON object, within size limits"}
        try:
            submitted = json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return 400, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                        "error": "INVALID_JSON", "message": str(exc)}
        if not isinstance(submitted, dict):
            return 400, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                        "error": "INVALID_REQUEST_BODY", "message": "body must be a JSON object"}

        # Account identity is never accepted from the request body (mission section 6: not a
        # plain-text field) - the currently effective policy's own allowed_accounts carries over
        # unchanged. Everything else the submitted body supplies overrides the current value;
        # anything it omits also carries over from the current effective policy, so a partial
        # save (e.g. only risk_per_trade changed) can never accidentally blank out a field the
        # operator didn't touch.
        try:
            current, provenance = read_effective_policy_record(self._connect)
        except RiskPolicyError as exc:
            return 503, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                        "degraded": True, "error": "POLICY_UNAVAILABLE", "message": str(exc), "unavailable": []}
        merged = {
            "version": current.version, "enabled": current.enabled, "max_volume": current.max_volume,
            "allowed_symbols": list(current.allowed_symbols or ()), "allowed_accounts": list(current.allowed_accounts),
            "allowed_strategies": list(current.allowed_strategies), "max_signal_age_seconds": current.max_signal_age_seconds,
            "risk_per_trade": current.risk_per_trade, "max_daily_loss": current.max_daily_loss,
            "max_concurrent_positions": current.max_concurrent_positions, "max_concurrent_orders": current.max_concurrent_orders,
            "max_account_exposure": current.max_account_exposure, "duplicate_position_policy": current.duplicate_position_policy,
            "canary_max_new_executions": current.canary_max_new_executions,
        }
        merged.update(_wire_to_raw(submitted))
        merged["allowed_accounts"] = list(current.allowed_accounts)  # never operator-editable here
        if isinstance(merged.get("allowed_symbols"), list):
            merged["allowed_symbols"] = [s.strip().upper() if isinstance(s, str) else s for s in merged["allowed_symbols"]]
            added = sorted({s for s in merged["allowed_symbols"] if isinstance(s, str)}
                           - set(current.allowed_symbols or ()))
            try:
                untradable = [s for s in added if self._provider_symbol(s) is None]
            except Exception as exc:  # noqa: BLE001
                return 503, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                             "degraded": True, "error": "INSTRUMENT_CATALOG_UNAVAILABLE", "message": str(exc),
                             "unavailable": []}
            if untradable:
                return 400, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                             "error": "SYMBOL_NOT_TRADABLE",
                             "message": f"no active MT5 provider mapping for {', '.join(untradable)}; "
                                        "live execution could not resolve a broker symbol"}

        expected_revision = submitted.get("revision")
        if expected_revision is None:
            return 409, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                         "error": "REVISION_REQUIRED", "message": "policy revision is required for update"}
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            return 400, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                         "error": "INVALID_REVISION", "message": "revision must be an integer"}
        try:
            saved = write_policy_override(merged, expected_revision=expected_revision, connect_fn=self._connect)
        except RiskPolicyRevisionConflict as exc:
            return 409, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                         "error": "REVISION_CONFLICT", "message": str(exc)}
        except RiskPolicyError as exc:
            return 400, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "UNAVAILABLE",
                        "error": "POLICY_VALIDATION_FAILED", "message": str(exc)}
        _saved, saved_meta = read_effective_policy_record(self._connect)
        data = _policy_to_wire(saved, source=saved_meta["source"], provenance=saved_meta)
        data["executionAuthorityMode"] = self.execution_authority_mode()
        return 200, {"api_version": "v1", "source": "execution_v2_risk_policy", "status": "ACTIVE",
                    "degraded": False, "read_only": False, "data": data, "unavailable": []}
