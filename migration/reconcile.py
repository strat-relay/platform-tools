from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping


class ReconciliationStatus(str, Enum):
    MATCH = "MATCH"
    MISSING_LEGACY = "MISSING_LEGACY"
    MISSING_DATABASE = "MISSING_DATABASE"
    HASH_MISMATCH = "HASH_MISMATCH"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    TERMINAL_STATE_MISMATCH = "TERMINAL_STATE_MISMATCH"
    GENERATION_MISMATCH = "GENERATION_MISMATCH"
    UNRESOLVED = "UNRESOLVED"
    EXPECTED_LAG = "EXPECTED_LAG"
    KNOWN_LEGACY_ANOMALY = "KNOWN_LEGACY_ANOMALY"
    MALFORMED_LEGACY_LINE = "MALFORMED_LEGACY_LINE"


# A7 proposes two 1-second tailer polls plus 30 seconds of transport/processing
# allowance. Callers can provide another timedelta for measured environments.
DEFAULT_RECONCILIATION_DELTA = timedelta(seconds=32)


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _within_reconciliation_delta(record: Mapping[str, Any], *, delta: timedelta,
                                 as_of: datetime) -> bool:
    """Use source/event emission time, never decision/reference-fill time."""
    source_time = _parse_utc_timestamp(record.get("signal_emitted_at"))
    if source_time is None:
        return False
    age = as_of - source_time
    # Future timestamps are data-quality findings, not evidence of freshness.
    return timedelta(0) <= age <= delta


@dataclass(frozen=True)
class ReconciliationFinding:
    identity: str
    status: ReconciliationStatus
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"identity": self.identity, "status": self.status.value, "detail": self.detail}


def reconcile(legacy: Iterable[Mapping[str, Any]], database: Iterable[Mapping[str, Any]], *, key: str = "id",
              delta: timedelta = DEFAULT_RECONCILIATION_DELTA,
              as_of: datetime | None = None) -> dict[str, Any]:
    if delta < timedelta(0):
        raise ValueError("reconciliation Delta must not be negative")
    comparison_time = as_of or datetime.now(timezone.utc)
    if comparison_time.tzinfo is None:
        raise ValueError("reconciliation as_of must include timezone information")
    comparison_time = comparison_time.astimezone(timezone.utc)
    left, right = {str(x[key]): x for x in legacy}, {str(x[key]): x for x in database}
    findings: list[ReconciliationFinding] = []
    for identity in sorted(set(left) | set(right)):
        a, b = left.get(identity), right.get(identity)
        if a is None: status, detail = ReconciliationStatus.MISSING_LEGACY, "database row has no legacy record"
        elif b is None and _within_reconciliation_delta(a, delta=delta, as_of=comparison_time):
            status, detail = ReconciliationStatus.EXPECTED_LAG, "legacy signal_emitted_at is within the configured reconciliation Delta"
        elif b is None: status, detail = ReconciliationStatus.MISSING_DATABASE, "legacy record has no database row beyond Delta or without a valid source timestamp"
        elif a.get("version") is not None and b.get("version") is not None and a["version"] != b["version"]:
            status, detail = ReconciliationStatus.VERSION_MISMATCH, "strategy versions differ"
        elif a.get("hash") is not None and b.get("hash") is not None and a["hash"] != b["hash"]:
            status, detail = ReconciliationStatus.HASH_MISMATCH, "canonical hashes differ"
        elif any(a.get(field) is not None and b.get(field) is not None and a.get(field) != b.get(field)
                 for field in ("strategy_id", "strategy_ref", "version", "parameter_set_ref", "strategy_instance_id",
                               "instrument", "direction", "decision_time", "signal_emitted_at", "decision", "entry_type",
                               "entry_mechanisms",
                               "entry_price", "stop_price", "target_price", "risk_distance", "target_distance", "target_r",
                               "economic_position_id", "entry_opportunity_id", "setup_id", "source_event_id", "terminal_state")):
            status, detail = ReconciliationStatus.HASH_MISMATCH, "canonical signal semantic fields differ"
        elif a.get("terminal_state") is not None and b.get("terminal_state") is not None and a["terminal_state"] != b["terminal_state"]:
            status, detail = ReconciliationStatus.TERMINAL_STATE_MISMATCH, "terminal states differ"
        elif a.get("generation") is not None and b.get("generation") is not None and a["generation"] != b["generation"]:
            status, detail = ReconciliationStatus.GENERATION_MISMATCH, "ownership generations differ"
        else: status, detail = ReconciliationStatus.MATCH, ""
        findings.append(ReconciliationFinding(identity, status, detail))
    counts = {status.value: sum(x.status == status for x in findings) for status in ReconciliationStatus}
    return {"findings": [x.to_dict() for x in findings], "summary": counts, "clean": all(x.status == ReconciliationStatus.MATCH for x in findings)}
