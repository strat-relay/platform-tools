"""Database-backed runtime configuration for Context Structure Retrace V2.

V1 is deliberately not imported as an authority for configuration.  V2 reads one
immutable ParameterSet selected by its registered strategy instance.  A new
ParameterSet is required for every semantic change; open trades keep the values
captured when they were created.
"""
from __future__ import annotations

import os
from typing import Any


INSTANCE_ID = os.environ.get("CONTEXT_V2_INSTANCE_ID", "context-v2-research")

PARAMETER_SCHEMA_ID = "context-structure-retrace-v2-runtime-v1"
DEFAULTS: dict[str, Any] = {
    "minimum_required_r": 1.0,
    "target_extension_fraction": 0.50,
    "stop_atr_buffer_fraction": 0.10,
    "stop_spread_buffer_multiplier": 1.25,
    "retracement_entry_fraction": 0.20,
    "max_retrace_candles": 12,
    "max_hold_minutes": 1440,
    "enabled_setup_events": [
        "BULLISH_ENGULFING", "BEARISH_ENGULFING", "MORNING_STAR", "EVENING_STAR",
        "BULLISH_REJECTION_WICK", "BEARISH_REJECTION_WICK",
    ],
    "reentry_enabled": True,
    "target_selection_policy": "NEAREST_VALID_STRUCTURE",
}

PARAMETER_FIELDS: dict[str, dict[str, Any]] = {
    "minimum_required_r": {"type": "decimal", "required": True, "minimum": 0.1, "maximum": 10.0,
                            "label": "Minimum planned R:R", "unit": "R"},
    "target_extension_fraction": {"type": "decimal", "required": True, "minimum": 0.0, "maximum": 5.0,
                                   "label": "Target extension", "unit": "setup range"},
    "stop_atr_buffer_fraction": {"type": "decimal", "required": True, "minimum": 0.0, "maximum": 5.0,
                                  "label": "Stop ATR buffer", "unit": "ATR"},
    "stop_spread_buffer_multiplier": {"type": "decimal", "required": True, "minimum": 0.0, "maximum": 10.0,
                                       "label": "Stop spread buffer", "unit": "spread"},
    "retracement_entry_fraction": {"type": "decimal", "required": True, "minimum": 0.0, "maximum": 1.0,
                                    "label": "Retracement entry fraction", "unit": "setup range"},
    "max_retrace_candles": {"type": "integer", "required": True, "minimum": 1, "maximum": 100,
                             "label": "Maximum retracement candles", "unit": "M5 candles"},
    "max_hold_minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 10080,
                          "label": "Maximum hold", "unit": "minutes"},
    "enabled_setup_events": {"type": "text_list", "required": True, "label": "Enabled setup patterns"},
    "reentry_enabled": {"type": "boolean", "required": True, "label": "Allow re-entry"},
    "target_selection_policy": {"type": "enum", "required": True,
                                 "enum": ["NEAREST_VALID_STRUCTURE", "EXTENSION_ONLY", "OPPOSING_STRUCTURE_ONLY"],
                                 "label": "Target selection policy"},
}


def _validate(values: dict[str, Any]) -> dict[str, Any]:
    result = {**DEFAULTS, **values}
    for key, spec in PARAMETER_FIELDS.items():
        value = result[key]
        if spec.get("type") == "text_list":
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                raise ValueError(f"{key} must be a non-empty string list")
            continue
        if spec.get("type") == "boolean":
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
            continue
        if "enum" in spec and value not in spec["enum"]:
            raise ValueError(f"invalid {key}")
        if spec.get("type") == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"{key} must be integer")
        if spec.get("type") == "decimal" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ValueError(f"{key} must be numeric")
        if "minimum" in spec and value < spec["minimum"]:
            raise ValueError(f"{key} below minimum")
        if "maximum" in spec and value > spec["maximum"]:
            raise ValueError(f"{key} above maximum")
    return result


def load_from_database() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(values, identity)`` from the registered V2 instance.

    Local tests and explicitly database-less research runs use the defaults. A
    configured database is authoritative and failures are propagated so the
    runner cannot silently use stale or invented strategy parameters.
    """
    from postgres.config import PostgresConfig
    from postgres.db import connect

    cfg = PostgresConfig.from_env()
    if not ((cfg.dsn and cfg.dsn.strip()) or cfg.host):
        values = _validate(dict(DEFAULTS))
        return values, {"instance_id": INSTANCE_ID, "parameter_set_id": "context-v2-runtime-default",
                        "fingerprint": "LOCAL_DEFAULTS", "revision": 0, "source": "LOCAL_DEFAULTS"}
    with connect(cfg, readonly=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT v.id, p.parameter_set_id, p.fingerprint, p.values,
                          p.updated_at, i.updated_at
                   FROM strategy_mgmt.strategy_instance_v2 i
                   JOIN strategy_mgmt.strategy_version v ON v.id = i.strategy_version_id
                   JOIN strategy_mgmt.parameter_set p ON p.id = i.parameter_set_id
                   WHERE coalesce(i.attributes->>'instance_id', i.id::text) = %s AND i.online = true""",
                (INSTANCE_ID,),
            )
            row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"registered ONLINE Context V2 instance not found: {INSTANCE_ID}")
    values = _validate(dict(row[3] or {}))
    return values, {"instance_id": INSTANCE_ID, "parameter_set_id": row[1],
                    "fingerprint": row[2], "updated_at": row[4].isoformat() if row[4] else None,
                    "instance_updated_at": row[5].isoformat() if row[5] else None,
                    "source": "POSTGRES_STRATEGY_MGMT"}
