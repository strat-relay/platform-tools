"""Operator control for the Trade Manager mode: GET/POST /api/v1/trade-manager/mode.

Modes: OFF, SHADOW, LIVE. OFF and SHADOW have no broker effects. LIVE lets the execution plane act on
Trade Manager decisions for platform positions (reduce-only SL/TP changes and closes), and still
only while execution authority is ENABLED - so execution authority, not this switch, remains the
gate for any broker write. A change is revision-checked (optimistic concurrency) and audited in
trade_management.trade_manager_mode_change.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from postgres.db import connect
from trade_management.mode import MODES, TradeManagerModeError, read_mode_record, set_mode

SOURCE = "trade_manager_mode"


class TradeManagerModeApi:
    def __init__(self, connect_fn: Callable[..., Any] = connect):
        self._connect = connect_fn

    @staticmethod
    def _ok(data: dict[str, Any]) -> dict[str, Any]:
        return {"api_version": "v1", "source": SOURCE, "status": "ACTIVE", "degraded": "error" in data,
                "read_only": False, "data": data, "unavailable": []}

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"api_version": "v1", "source": SOURCE, "status": "UNAVAILABLE", "degraded": False,
                "read_only": False, "error": code, "message": message,
                "unavailable": [{"code": code, "source": SOURCE, "message": message}]}

    def read(self) -> tuple[int, dict[str, Any]]:
        try:
            with self._connect(readonly=True) as conn:
                record = read_mode_record(conn)
        except Exception:
            record = {"mode": "OFF", "revision": 0, "error": "MODE_UNAVAILABLE"}
        return 200, self._ok({**record, "modes": list(MODES), "broker_effects": False})

    def save(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        try:
            submitted = json.loads((body or b"").decode())
            mode = submitted.get("mode")
            revision = submitted.get("expectedRevision")
            if mode not in MODES or isinstance(revision, bool) or not isinstance(revision, int):
                raise ValueError("mode (OFF, SHADOW or LIVE) and integer expectedRevision are required")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            return 400, self._error("INVALID_REQUEST", str(exc))
        try:
            conn = self._connect(readonly=False)
            try:
                data = set_mode(conn, mode, expected_revision=revision,
                                changed_by=str(submitted.get("updatedBy") or "console"),
                                reason=submitted.get("reason"))
            finally:
                conn.close()
        except TradeManagerModeError as exc:
            return 409, self._error("MODE_CONFLICT", str(exc))
        except Exception:
            return 503, self._error("SOURCE_UNAVAILABLE", "trade manager mode could not be saved")
        return 200, self._ok({**data, "modes": list(MODES), "broker_effects": False})
