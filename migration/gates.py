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


def evaluate_signal_gates(*, reconciliation_clean: bool, mismatch_count: int,
                         ingested_records: int, malformed_records: int,
                         shadow_consumed: int, duplicate_effects: int,
                         evidence_references: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Evaluate only P2 gates; authority/retirement gates remain explicit N-A."""
    refs = list(evidence_references)
    return {
        CutoverGate.SHADOW_WRITE_READY.value: {"status": "PASS" if ingested_records and malformed_records == 0 else "FAIL", "threshold": "ingested_records > 0 and malformed_records == 0", "observed": {"ingested_records": ingested_records, "malformed_records": malformed_records}, "evidence": refs},
        CutoverGate.DUAL_WRITE_RECONCILED.value: {"status": "PASS" if reconciliation_clean and mismatch_count == 0 else "FAIL", "threshold": "zero semantic mismatches", "observed": {"reconciliation_clean": reconciliation_clean, "mismatch_count": mismatch_count}, "evidence": refs},
        CutoverGate.DB_AUTHORITY_READY.value: {"status": "NOT_EVALUATED", "threshold": "separate authority handoff", "observed": {}, "evidence": []},
        CutoverGate.JETSTREAM_SHADOW_READY.value: {"status": "PASS" if shadow_consumed > 0 and duplicate_effects == 0 else "FAIL", "threshold": "shadow consumption with zero duplicate effects", "observed": {"shadow_consumed": shadow_consumed, "duplicate_effects": duplicate_effects}, "evidence": refs},
        CutoverGate.JETSTREAM_PRIMARY_READY.value: {"status": "NOT_EVALUATED", "threshold": "separate event-authority handoff", "observed": {}, "evidence": []},
        CutoverGate.LEGACY_READ_RETIRE_READY.value: {"status": "NOT_EVALUATED", "threshold": "runtime access audit", "observed": {}, "evidence": []},
        CutoverGate.LEGACY_WRITE_RETIRE_READY.value: {"status": "NOT_EVALUATED", "threshold": "runtime write audit and S1 completion", "observed": {}, "evidence": []},
    }
