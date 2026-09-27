"""Structured, stdout-friendly audit events for strategy runners."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger("stratrelay.strategy_audit")


def configure_strategy_audit_logging() -> None:
    level_name = os.environ.get("STRATEGY_AUDIT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    LOGGER.setLevel(level)
    LOGGER.propagate = True
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(stream=sys.stdout, level=level)
    elif root.level > level:
        root.setLevel(level)


def audit(event: str, *, runner: str, **fields: Any) -> None:
    payload = {
        "audit": "strategy",
        "event": event,
        "runner": runner,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **fields,
    }
    LOGGER.info(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))

