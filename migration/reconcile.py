from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class ReconciliationFinding:
    identity: str
    status: ReconciliationStatus
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"identity": self.identity, "status": self.status.value, "detail": self.detail}


def reconcile(legacy: Iterable[Mapping[str, Any]], database: Iterable[Mapping[str, Any]], *, key: str = "id") -> dict[str, Any]:
    left, right = {str(x[key]): x for x in legacy}, {str(x[key]): x for x in database}
    findings: list[ReconciliationFinding] = []
    for identity in sorted(set(left) | set(right)):
        a, b = left.get(identity), right.get(identity)
        if a is None: status, detail = ReconciliationStatus.MISSING_LEGACY, "database row has no legacy record"
        elif b is None: status, detail = ReconciliationStatus.MISSING_DATABASE, "legacy record has no database row"
        elif a.get("hash") is not None and b.get("hash") is not None and a["hash"] != b["hash"]:
            status, detail = ReconciliationStatus.HASH_MISMATCH, "canonical hashes differ"
        elif any(a.get(field) is not None and b.get(field) is not None and a.get(field) != b.get(field)
                 for field in ("strategy_id", "instrument", "direction", "decision_time", "decision")):
            status, detail = ReconciliationStatus.HASH_MISMATCH, "canonical signal semantic fields differ"
        elif a.get("version") is not None and b.get("version") is not None and a["version"] != b["version"]:
            status, detail = ReconciliationStatus.VERSION_MISMATCH, "aggregate versions differ"
        elif a.get("terminal_state") is not None and b.get("terminal_state") is not None and a["terminal_state"] != b["terminal_state"]:
            status, detail = ReconciliationStatus.TERMINAL_STATE_MISMATCH, "terminal states differ"
        elif a.get("generation") is not None and b.get("generation") is not None and a["generation"] != b["generation"]:
            status, detail = ReconciliationStatus.GENERATION_MISMATCH, "ownership generations differ"
        else: status, detail = ReconciliationStatus.MATCH, ""
        findings.append(ReconciliationFinding(identity, status, detail))
    counts = {status.value: sum(x.status == status for x in findings) for status in ReconciliationStatus}
    return {"findings": [x.to_dict() for x in findings], "summary": counts, "clean": all(x.status == ReconciliationStatus.MATCH for x in findings)}
