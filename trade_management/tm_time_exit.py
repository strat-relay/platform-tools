"""Deterministic Trade Manager time-exit policy.

The policy value is captured on ``managed_trade`` when the signal is opened.  It is
therefore immutable for an existing trade even when the instance policy changes later.
This evaluator only decides; the existing execution-v2 management consumer performs the
reduce-only close after the decision is published and the safety checks pass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

EVALUATOR_ID = "tm-context-time-exit.v1"
REASON_NOT_DUE = "TIME_EXIT_NOT_DUE"
REASON_DUE = "TIME_EXIT_DUE"
REASON_NO_POLICY = "TIME_EXIT_NOT_CONFIGURED"
REASON_TRADE_CLOSED = "TRADE_CLOSED"


def evaluate_time_exit(*, trade_state: str, time_exit_at: Any, as_of: Any) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    """Return EXIT only once the captured deadline has passed."""
    if trade_state != "OPEN":
        return "HOLD", (REASON_TRADE_CLOSED,), {}
    if time_exit_at is None:
        return "HOLD", (REASON_NO_POLICY,), {}
    deadline = _as_utc(time_exit_at)
    observed = _as_utc(as_of)
    if observed < deadline:
        return "HOLD", (REASON_NOT_DUE,), {"time_exit_at": deadline.isoformat()}
    return "EXIT", (REASON_DUE,), {"exit_reason": "TIME_EXIT", "time_exit_at": deadline.isoformat()}


def _as_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None
            else result.astimezone(timezone.utc))
