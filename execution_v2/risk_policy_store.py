"""Canonical relational PostgreSQL authority for the V2 risk policy.

The JSON policy is bootstrap/reference material only. Runtime reads never fall back to it.
All values are reconstructed through the existing ``parse_policy_dict`` validator.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable

from postgres.db import connect, transaction

from .risk import RiskPolicy, RiskPolicyError, RiskPolicyRevisionConflict, parse_policy_dict

SOURCE_POSTGRES = "POSTGRES"
SOURCE_FILE_BASELINE = "LEGACY_BOOTSTRAP_ONLY"


def _fingerprint(raw: dict[str, Any]) -> str:
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _policy_raw(policy: RiskPolicy) -> dict[str, Any]:
    return {"version": policy.version, "enabled": policy.enabled,
            "allowed_accounts": list(policy.allowed_accounts),
            "allowed_strategies": list(policy.allowed_strategies),
            "allowed_symbols": list(policy.allowed_symbols or ()),
            "risk_per_trade": policy.risk_per_trade, "max_volume": policy.max_volume,
            "max_signal_age_seconds": policy.max_signal_age_seconds,
            "max_daily_loss": policy.max_daily_loss,
            "max_concurrent_positions": policy.max_concurrent_positions,
            "max_concurrent_orders": policy.max_concurrent_orders,
            "max_account_exposure": policy.max_account_exposure,
            "duplicate_position_policy": policy.duplicate_position_policy,
            "canary_max_new_executions": policy.canary_max_new_executions}


def policy_fingerprint(policy: RiskPolicy) -> str:
    """Return the same canonical fingerprint exposed by the policy API."""
    return _fingerprint(_policy_raw(policy))


def _read_row(cur: Any, *, for_update: bool = False) -> tuple[dict[str, Any], str]:
    suffix = " FOR UPDATE" if for_update else ""
    cur.execute("""SELECT policy_id, revision, version, enabled, risk_per_trade, max_volume,
                          max_signal_age_seconds, max_daily_loss, max_concurrent_positions,
                          max_concurrent_orders, max_account_exposure, duplicate_position_policy,
                          canary_max_new_executions, updated_at
                   FROM execution_v2.risk_policy WHERE policy_id='current'""" + suffix)
    row = cur.fetchone()
    if row is None:
        raise RiskPolicyError("canonical PostgreSQL V2 risk policy is missing")
    keys = ("policy_id", "revision", "version", "enabled", "risk_per_trade", "max_volume",
            "max_signal_age_seconds", "max_daily_loss", "max_concurrent_positions",
            "max_concurrent_orders", "max_account_exposure", "duplicate_position_policy",
            "canary_max_new_executions", "updated_at")
    data = dict(zip(keys, row))
    policy_id = data["policy_id"]
    cur.execute("SELECT account_id FROM execution_v2.risk_policy_allowed_account WHERE policy_id=%s ORDER BY account_id", (policy_id,))
    data["allowed_accounts"] = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT strategy_ref FROM execution_v2.risk_policy_allowed_strategy WHERE policy_id=%s ORDER BY strategy_ref", (policy_id,))
    data["allowed_strategies"] = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT symbol FROM execution_v2.risk_policy_allowed_symbol WHERE policy_id=%s ORDER BY symbol", (policy_id,))
    data["allowed_symbols"] = [r[0] for r in cur.fetchall()]
    return data, policy_id


def _raw(data: dict[str, Any]) -> dict[str, Any]:
    return {"version": int(data["version"]), "enabled": bool(data["enabled"]),
            "allowed_accounts": list(data["allowed_accounts"]),
            "allowed_strategies": list(data["allowed_strategies"]),
            "allowed_symbols": list(data["allowed_symbols"]),
            "risk_per_trade": float(data["risk_per_trade"]), "max_volume": float(data["max_volume"]),
            "max_signal_age_seconds": float(data["max_signal_age_seconds"]),
            "max_daily_loss": float(data["max_daily_loss"]),
            "max_concurrent_positions": int(data["max_concurrent_positions"]),
            "max_concurrent_orders": int(data["max_concurrent_orders"]),
            "max_account_exposure": float(data["max_account_exposure"]),
            "duplicate_position_policy": data["duplicate_position_policy"],
            "canary_max_new_executions": int(data["canary_max_new_executions"])}


def read_effective_policy_record(connect_fn: Callable[..., Any] = connect) -> tuple[RiskPolicy, dict[str, Any]]:
    try:
        with connect_fn(readonly=True) as conn:
            with conn.cursor() as cur:
                data, policy_id = _read_row(cur)
    except RiskPolicyError:
        raise
    except Exception as exc:
        raise RiskPolicyError("canonical PostgreSQL V2 risk policy is unavailable") from exc
    raw = _raw(data)
    policy = parse_policy_dict(raw, source=SOURCE_POSTGRES)
    return policy, {"policy_id": policy_id, "revision": int(data["revision"]),
                    "updated_at": data["updated_at"], "source": SOURCE_POSTGRES,
                    "fingerprint": _fingerprint(raw)}


def read_effective_policy(connect_fn: Callable[..., Any] = connect, **_: Any) -> tuple[RiskPolicy, str]:
    policy, meta = read_effective_policy_record(connect_fn)
    return policy, meta["source"]


def persist_policy(raw_policy: Any, *, expected_revision: int | None = None,
                   updated_by: str | None = None, connect_fn: Callable[..., Any] = connect) -> tuple[RiskPolicy, dict[str, Any]]:
    policy = parse_policy_dict(raw_policy, source=SOURCE_POSTGRES)
    raw = {"version": policy.version, "enabled": policy.enabled,
           "allowed_accounts": list(policy.allowed_accounts), "allowed_strategies": list(policy.allowed_strategies),
           "allowed_symbols": list(policy.allowed_symbols or ()), "risk_per_trade": policy.risk_per_trade,
           "max_volume": policy.max_volume, "max_signal_age_seconds": policy.max_signal_age_seconds,
           "max_daily_loss": policy.max_daily_loss, "max_concurrent_positions": policy.max_concurrent_positions,
           "max_concurrent_orders": policy.max_concurrent_orders, "max_account_exposure": policy.max_account_exposure,
           "duplicate_position_policy": policy.duplicate_position_policy,
           "canary_max_new_executions": policy.canary_max_new_executions}
    conn = connect_fn(readonly=False)
    with transaction(conn):
        with conn.cursor() as cur:
            current, policy_id = _read_row(cur, for_update=True) if expected_revision is not None else (None, "current")
            if current is not None and int(current["revision"]) != expected_revision:
                raise RiskPolicyRevisionConflict("stale policy revision")
            previous_revision = int(current["revision"]) if current is not None else None
            revision = (previous_revision or 0) + 1
            cur.execute("""INSERT INTO execution_v2.risk_policy
                (policy_id, revision, version, enabled, risk_per_trade, max_volume, max_signal_age_seconds,
                 max_daily_loss, max_concurrent_positions, max_concurrent_orders, max_account_exposure,
                 duplicate_position_policy, canary_max_new_executions, updated_by)
                VALUES ('current',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (policy_id) DO UPDATE SET revision=EXCLUDED.revision, version=EXCLUDED.version,
                 enabled=EXCLUDED.enabled, risk_per_trade=EXCLUDED.risk_per_trade, max_volume=EXCLUDED.max_volume,
                 max_signal_age_seconds=EXCLUDED.max_signal_age_seconds, max_daily_loss=EXCLUDED.max_daily_loss,
                 max_concurrent_positions=EXCLUDED.max_concurrent_positions, max_concurrent_orders=EXCLUDED.max_concurrent_orders,
                 max_account_exposure=EXCLUDED.max_account_exposure, duplicate_position_policy=EXCLUDED.duplicate_position_policy,
                 canary_max_new_executions=EXCLUDED.canary_max_new_executions, updated_at=now(), updated_by=EXCLUDED.updated_by""",
                       (revision, policy.version, policy.enabled, policy.risk_per_trade, policy.max_volume,
                        policy.max_signal_age_seconds, policy.max_daily_loss, policy.max_concurrent_positions,
                        policy.max_concurrent_orders, policy.max_account_exposure, policy.duplicate_position_policy,
                        policy.canary_max_new_executions, updated_by))
            for table, column, values in (("risk_policy_allowed_account", "account_id", policy.allowed_accounts),
                                          ("risk_policy_allowed_strategy", "strategy_ref", policy.allowed_strategies),
                                          ("risk_policy_allowed_symbol", "symbol", policy.allowed_symbols or ())):
                cur.execute(f"DELETE FROM execution_v2.{table} WHERE policy_id='current'")
                for value in values:
                    cur.execute(f"INSERT INTO execution_v2.{table} (policy_id,{column}) VALUES ('current',%s)", (value,))
            changed = {k: v for k, v in raw.items() if current is None or current.get(k) != v}
            cur.execute("""INSERT INTO execution_v2.risk_policy_change
                         (change_id, policy_id, previous_revision, new_revision, changed_fields, changed_by)
                         VALUES (%s,'current',%s,%s,%s::jsonb,%s)""",
                        (str(uuid.uuid4()), previous_revision, revision, json.dumps(changed), updated_by))
    return policy, {"policy_id": "current", "revision": revision, "updated_at": None,
                    "source": SOURCE_POSTGRES, "fingerprint": _fingerprint(raw)}


def write_policy_override(raw_policy: Any, *, updated_by: str | None = None,
                          expected_revision: int | None = None, connect_fn: Callable[..., Any] = connect) -> RiskPolicy:
    policy, _ = persist_policy(raw_policy, expected_revision=expected_revision, updated_by=updated_by, connect_fn=connect_fn)
    return policy


def read_canary_status(connect_fn: Callable[..., Any] = connect, *, canary_key: str,
                       configured_max: int) -> dict[str, int]:
    with connect_fn(readonly=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT max_new_executions, consumed FROM execution_v2.canary_state WHERE canary_key=%s", (canary_key,))
            row = cur.fetchone()
    if row is None:
        return {"max_new_executions": configured_max, "consumed": 0, "remaining": max(0, configured_max)}
    maximum, consumed = int(row[0]), int(row[1])
    return {"max_new_executions": maximum, "consumed": consumed, "remaining": max(0, maximum - consumed)}
