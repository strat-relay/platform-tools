from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class CutoverGate(str, Enum):
    SHADOW_WRITE_READY = "SHADOW_WRITE_READY"
    DUAL_WRITE_RECONCILED = "DUAL_WRITE_RECONCILED"
    DB_AUTHORITY_READY = "DB_AUTHORITY_READY"
    JETSTREAM_SHADOW_READY = "JETSTREAM_SHADOW_READY"
    JETSTREAM_PRIMARY_READY = "JETSTREAM_PRIMARY_READY"
    LEGACY_READ_RETIRE_READY = "LEGACY_READ_RETIRE_READY"
    LEGACY_WRITE_RETIRE_READY = "LEGACY_WRITE_RETIRE_READY"


@dataclass(frozen=True)
class GateEvidence:
    references: tuple[str, ...] = ()
    measures: Mapping[str, Any] = field(default_factory=dict)


def evaluate_gate(gate: CutoverGate, evidence: GateEvidence) -> dict[str, Any]:
    if not evidence.references:
        return {"gate": gate.value, "approved": False, "reason": "evidence reference required", "evidence": {}}
    blocking = {"mismatch_count", "unresolved_sites", "outbox_oldest_age_seconds"}
    bad = {k: v for k, v in evidence.measures.items() if k in blocking and isinstance(v, (int, float)) and v > 0}
    return {"gate": gate.value, "approved": not bad, "reason": "blocking measures present" if bad else "evidence satisfied", "evidence": dict(evidence.measures), "references": list(evidence.references)}
