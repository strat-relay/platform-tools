"""Structured signal-to-broker tracing for execution-v2.

Every trace record is a single JSON log line so Kubernetes can aggregate and
measure the complete path without parsing free-form messages.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("execution_v2.trace")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def elapsed_ms(start: Any, end: Any | None = None) -> float | None:
    if start is None:
        return None
    try:
        if not isinstance(start, datetime):
            start = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = end or utc_now()
        return round((end - start).total_seconds() * 1000, 3)
    except (TypeError, ValueError, AttributeError):
        return None


def emit(stage: str, *, signal_id: str | None = None,
         intent_id: str | None = None, attempt_id: str | None = None,
         event_id: str | None = None, signal_emitted_at: Any = None,
         outcome: str | None = None, error: str | None = None,
         **fields: Any) -> None:
    payload = {
        "event": "execution_trace",
        "stage": stage,
        "timestamp": utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "signal_id": signal_id,
        "execution_intent_id": intent_id,
        "attempt_id": attempt_id,
        "event_id": event_id,
        "signal_to_stage_ms": elapsed_ms(signal_emitted_at),
        "outcome": outcome,
        "error": error,
        **fields,
    }
    log.info("%s", json.dumps(payload, default=str, separators=(",", ":")))
