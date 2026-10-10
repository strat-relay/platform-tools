"""Revisioned global Trade Manager policy. Empty policy means no global rules."""
from __future__ import annotations

import json
from typing import Any, Callable

from postgres.db import connect

SOURCE = "trade_manager_policy"
FIELDS = {"breakeven_r", "trail_trigger_r", "trail_distance_r", "time_exit_minutes",
          "net_profit_target_usd", "profit_target_pips", "profit_target_r", "reentry_enabled",
          "enabled_setup_events"}


class TradeManagerPolicyApi:
    def __init__(self, connect_fn: Callable[..., Any] = connect): self._connect = connect_fn

    def _envelope(self, data: dict[str, Any], error: str | None = None) -> dict[str, Any]:
        out = {"api_version": "v1", "source": SOURCE, "status": "UNAVAILABLE" if error else "ACTIVE",
               "degraded": bool(error), "read_only": False, "data": data, "unavailable": []}
        if error: out.update({"error": "INVALID_REQUEST", "message": error})
        return out

    def read(self) -> tuple[int, dict[str, Any]]:
        try:
            with self._connect(readonly=True) as conn, conn.cursor() as cur:
                cur.execute("SELECT policy, revision, updated_at, updated_by FROM trade_management.global_policy WHERE policy_id='current'")
                row = cur.fetchone()
            if row is None: raise RuntimeError("global policy row missing")
            return 200, self._envelope({"policy": row[0], "revision": row[1], "updated_at": row[2], "updated_by": row[3]})
        except Exception as exc:
            return 503, self._envelope({}, str(exc))

    def save(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            submitted = json.loads((body or b"{}").decode())
            policy, expected = submitted.get("policy"), submitted.get("expectedRevision")
            if not isinstance(policy, dict) or not isinstance(expected, int) or isinstance(expected, bool): raise ValueError("policy and integer expectedRevision are required")
            unknown = set(policy) - FIELDS
            if unknown: raise ValueError(f"unsupported global policy fields: {', '.join(sorted(unknown))}")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            return 400, self._envelope({}, str(exc))
        try:
            with self._connect(readonly=False) as conn, conn.cursor() as cur:
                cur.execute("UPDATE trade_management.global_policy SET policy=%s, revision=revision+1, updated_at=now(), updated_by=%s WHERE policy_id='current' AND revision=%s RETURNING policy, revision, updated_at, updated_by", (json.dumps(policy), str(submitted.get("updatedBy") or "console"), expected))
                row = cur.fetchone()
                if row is None: return 409, self._envelope({}, "stale global policy revision")
            return 200, self._envelope({"policy": row[0], "revision": row[1], "updated_at": row[2], "updated_by": row[3]})
        except Exception as exc:
            return 503, self._envelope({}, str(exc))
