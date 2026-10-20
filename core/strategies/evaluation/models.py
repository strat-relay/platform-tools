"""Canonical strategy evaluation records.

These objects are deliberately independent of brokers, persistence, and
transport.  Their canonical JSON representation is the future persistence and
event boundary; it is not Python ``repr`` or a database serialization.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping

CANONICAL_SCHEMA_VERSION = "strategy-evaluation.v1"


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class Decision(_ValueEnum):
    SIGNAL = "SIGNAL"
    REJECT = "REJECT"
    NO_CANDIDATE = "NO_CANDIDATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class StageStatus(_ValueEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class TraceFidelity(_ValueEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


FIDELITY_DESCRIPTIONS = {
    TraceFidelity.L0: "final outcome only; no reliable stage evidence",
    TraceFidelity.L1: "coarse legacy event or lifecycle evidence",
    TraceFidelity.L2: "legacy detector evidence for observed stages, with gaps",
    TraceFidelity.L3: "complete ordered stage evidence from a canonical runtime",
}


def _timestamp(value: datetime | date | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime values must include timezone information")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    result = str(value).strip()
    if not result:
        raise ValueError("timestamp must be explicit and non-empty")
    return result


def _json_value(value: Any) -> Any:
    if isinstance(value, _ValueEnum):
        return value.value
    if isinstance(value, (datetime, date)):
        return _timestamp(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical values cannot contain NaN or infinity")
    return value


def canonical_bytes(value: Any) -> bytes:
    """Return stable UTF-8 canonical JSON bytes for supported domain values."""
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class ReasonCode:
    code: str
    version: str
    category: str
    description: str
    terminal: bool = True

    def __post_init__(self) -> None:
        if not self.code or not self.version or not self.category or not self.description:
            raise ValueError("reason code fields must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "version": self.version, "category": self.category,
                "description": self.description, "terminal": self.terminal}


class ReasonCodeRegistry:
    def __init__(self, codes: Mapping[tuple[str, str], ReasonCode] | None = None):
        self._codes = dict(codes or {})

    def get(self, code: str, version: str = "v1") -> ReasonCode:
        try:
            return self._codes[(code, version)]
        except KeyError as exc:
            raise KeyError(f"unknown reason code: {code}@{version}") from exc

    def require(self, reference: ReasonCode | str, version: str = "v1") -> ReasonCode:
        if isinstance(reference, ReasonCode):
            return self.get(reference.code, reference.version)
        return self.get(reference, version)

    def all(self) -> tuple[ReasonCode, ...]:
        return tuple(self._codes[key] for key in sorted(self._codes))


def default_reason_codes() -> ReasonCodeRegistry:
    definitions = (
        ("NO_CANDIDATE", "candidate", "No strategy candidate was available", True),
        ("SWEEP_NOT_FOUND", "stage", "Required liquidity sweep was not observed", True),
        ("DISPLACEMENT_NOT_CONFIRMED", "stage", "Required displacement was not confirmed", True),
        ("STRUCTURE_SHIFT_NOT_CONFIRMED", "stage", "Required micro-structure shift was not confirmed", True),
        ("RETRACEMENT_NOT_FILLED", "stage", "Candidate retracement did not fill within the bounded window", True),
        ("STRUCTURE_INVALIDATED", "stage", "Candidate structure was invalidated before entry", True),
        ("NO_VALID_ENTRY_SIGNAL", "decision", "No valid entry signal was produced", True),
        ("DATA_UNAVAILABLE", "runtime", "Required input data was unavailable", True),
        ("REVIEW_REQUIRED", "review", "A human review gate remains unresolved", False),
        ("LEGACY_TRACE_LIMITED", "trace", "Legacy runtime cannot expose complete stage evidence", False),
        # Versioned Price Action research lifecycle reasons.  These are shared
        # registry entries so traces never collapse distinct failures into the
        # generic NO_VALID_ENTRY_SIGNAL code.
        ("INSUFFICIENT_CONTEXT", "data", "Confirmed higher-timeframe context is not warm", True),
        ("INSUFFICIENT_EXECUTION_WARMUP", "data", "Execution history is not warm", True),
        ("NO_CONFIRMED_LEVEL", "structure", "No confirmed structural level was available", True),
        ("PATTERN_NOT_CONFIRMED", "pattern", "The reversal pattern was not confirmed", True),
        ("ZERO_RANGE_INVALID", "pattern", "Pattern candle has zero range", True),
        ("ZERO_BODY_UNSUPPORTED", "pattern", "Pattern candle has zero body", True),
        ("RANGE_BELOW_MINIMUM", "pattern", "Pattern range is below the frozen minimum", True),
        ("PIN_BAR_GEOMETRY_NOT_CONFIRMED", "pattern", "Pin-bar geometry was not confirmed", True),
        ("NO_PRIOR_DIRECTIONAL_MOVE", "structure", "Required prior directional move was absent", True),
        ("INVALID_STRUCTURAL_LOCATION", "structure", "Pattern did not reject from a valid structural zone", True),
        ("NO_KNOWN_OPPOSING_TARGET", "risk", "No known opposing target was available", True),
        ("INVALID_PLANNED_GEOMETRY", "risk", "Planned stop and target geometry was invalid", True),
        ("INSUFFICIENT_PLANNED_R", "risk", "Planned reward-to-risk was below the frozen minimum", True),
        ("RESEARCH_PROPOSAL_ONLY", "research", "Research proposal is not an executable signal", True),
        ("ENTRY_GAP_CANCELLED", "entry", "The next open exceeded the adverse gap limit", True),
        ("STRUCTURAL_INVALIDATION", "entry", "The next open structurally invalidated the setup", True),
        ("INVALID_ACTUAL_ENTRY_GEOMETRY", "entry", "Actual next-open geometry was invalid", True),
        ("INSUFFICIENT_ACTUAL_ENTRY_R", "entry", "Actual next-open reward-to-risk was below minimum", True),
        ("ENTRY_OBSERVED_RESEARCH_ONLY", "entry", "Next-open paper entry was observed for research only", True),
        ("MISSING_EXPECTED_NEXT_CANDLE", "entry", "The exact expected next candle was unavailable", True),
        ("DUPLICATE_EPISODE", "deduplication", "Candidate episode was already consumed", True),
        ("PATTERN_IDENTITY_INVALID", "data", "Pattern constituent identity was invalid", True),
        ("PATTERN_DATA_GAP", "data", "Pattern constituents were separated by an unsupported gap", True),
        ("CONTEXT_UNAVAILABLE_TIMEOUT", "data", "Event-time boundary expired without confirmed H1 context coverage", True),
    )
    # Additive research-library reasons; existing codes/consumers are unchanged.
    definitions += (
        ("LIBRARY_MANIPULATION_WINDOW_EXPIRED", "research_library", "Manipulation window expired", True),
        ("LIBRARY_NO_SESSION_SWEEP", "research_library", "No session sweep", True),
        ("LIBRARY_AMBIGUOUS_SESSION_SWEEP", "research_library", "Ambiguous session sweep", True),
        ("LIBRARY_SEQUENCE_INVALIDATED", "research_library", "Sequence invalidated", True),
        ("LIBRARY_SEQUENCE_EXPIRED", "research_library", "Sequence expired", True),
        ("LIBRARY_AWAITING_SEQUENCE_CONFIRMATION", "research_library", "Awaiting sequence confirmation", True),
        ("LIBRARY_SEQUENCE_ALREADY_CONFIRMED", "research_library", "Sequence already confirmed", True),
        ("LIBRARY_ACCUMULATION_TOO_WIDE", "research_library", "Accumulation too wide", True),
        ("LIBRARY_BLOCK_INVALIDATED", "research_library", "Block invalidated", True),
        ("LIBRARY_BLOCK_EXPIRED", "research_library", "Block expired", True),
        ("LIBRARY_BLOCK_RETEST_NOT_CONFIRMED", "research_library", "Block retest not confirmed", True),
        ("LIBRARY_ENTRY_LAYER_PROGRESS_REJECTED", "research_library", "Entry lost required layer progress", True),
        ("LIBRARY_BREAKOUT_QUALITY_REJECTED", "research_library", "Breakout quality rejected", True),
        ("LIBRARY_FIRST_RETEST_REJECTED", "research_library", "First retest did not hold", True),
        ("LIBRARY_LAYER_PROGRESS_REJECTED", "research_library", "Insufficient favorable progress for layer", True),
        ("LIBRARY_INSUFFICIENT_WARMUP", "research_library", "Insufficient warmup", True),
        ("LIBRARY_ZERO_VOLATILITY", "research_library", "Zero volatility", True),
        ("LIBRARY_EXECUTION_DATA_GAP", "research_library", "Execution data gap", True),
        ("LIBRARY_OUTSIDE_SESSION", "research_library", "Outside session", True),
        ("LIBRARY_INCOMPLETE_RANGE", "research_library", "Incomplete range", True),
        ("LIBRARY_INVALID_GEOMETRY", "research_library", "Invalid geometry", True),
        ("LIBRARY_RR_BELOW_MINIMUM", "research_library", "Rr below minimum", True),
        ("LIBRARY_PROPOSAL", "research_library", "Proposal", True),
        ("LIBRARY_INCOMPLETE_SESSION", "research_library", "Incomplete session", True),
        ("LIBRARY_VOLUME_UNAVAILABLE", "research_library", "Volume unavailable", True),
        ("LIBRARY_INCOMPLETE_CALENDAR_RANGE", "research_library", "Incomplete calendar range", True),
        ("LIBRARY_NO_STRUCTURAL_BIAS", "research_library", "No structural bias", True),
        ("LIBRARY_NO_SETUP", "research_library", "No setup", True),
        ("LIBRARY_INSTRUMENT_OUTSIDE_SCOPE", "research_library", "Instrument outside scope", True),
        ("LIBRARY_CONTEXT_UNAVAILABLE", "research_library", "Context unavailable", True),
        ("LIBRARY_CONTEXT_DATA_GAP", "research_library", "Context data gap", True),
        ("LIBRARY_ENTRY_ACCEPTED", "research_library", "Entry accepted", True),
        ("LIBRARY_MISSING_NEXT_CANDLE", "research_library", "Missing next candle", True),
        ("LIBRARY_ENTRY_WINDOW_EXPIRED", "research_library", "Entry window expired", True),
        ("LIBRARY_ENTRY_GAP_CANCELLED", "research_library", "Entry gap cancelled", True),
        ("LIBRARY_INVALID_ENTRY_GEOMETRY", "research_library", "Invalid entry geometry", True),
        ("LIBRARY_ENTRY_RR_BELOW_MINIMUM", "research_library", "Entry rr below minimum", True),
        ("LIBRARY_DUPLICATE_EPISODE", "research_library", "Duplicate episode", True),
    )
    return ReasonCodeRegistry({(code, "v1"): ReasonCode(code, "v1", category, description, terminal)
                               for code, category, description, terminal in definitions})


_REGISTRY = default_reason_codes()


@dataclass(frozen=True)
class StageResult:
    stage_id: str
    status: StageStatus
    primitive_id: str | None = None
    observed: Any = None
    expected: Any = None
    margin: Any = None
    evidence_times: tuple[str, ...] = ()
    reason_code: ReasonCode | None = None
    metadata: Mapping[str, Any] = ()

    def __post_init__(self) -> None:
        if not self.stage_id:
            raise ValueError("stage_id must be non-empty")
        object.__setattr__(self, "evidence_times", tuple(_timestamp(x) for x in self.evidence_times))
        if self.reason_code is not None:
            _REGISTRY.require(self.reason_code)

    def to_dict(self) -> dict[str, Any]:
        return {"stage_id": self.stage_id, "status": self.status.value,
                "primitive_id": self.primitive_id, "observed": self.observed,
                "expected": self.expected, "margin": self.margin,
                "evidence_times": self.evidence_times,
                "reason_code": self.reason_code.to_dict() if self.reason_code else None,
                "metadata": dict(self.metadata) if isinstance(self.metadata, Mapping) else {}}


@dataclass(frozen=True)
class DecisionTrace:
    stages: tuple[StageResult, ...]
    decision: Decision
    reason_codes: tuple[ReasonCode, ...] = ()
    fidelity: TraceFidelity = TraceFidelity.L0
    trace_version: str = "v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        for reason in self.reason_codes:
            _REGISTRY.require(reason)

    def to_dict(self) -> dict[str, Any]:
        return {"trace_version": self.trace_version, "fidelity": self.fidelity.value,
                "decision": self.decision.value, "stages": [stage.to_dict() for stage in self.stages],
                "reason_codes": [reason.to_dict() for reason in self.reason_codes]}

    @property
    def trace_hash(self) -> str:
        return canonical_hash(self.to_dict())


@dataclass(frozen=True)
class Evaluation:
    strategy_id: str
    instrument: str
    decision_time: str
    decision: Decision
    trace: DecisionTrace
    direction: str | None = None
    strategy_version: str | None = None
    parameter_set_id: str | None = None
    candidate_id: str | None = None
    reason_codes: tuple[ReasonCode, ...] = ()
    trace_fidelity: TraceFidelity = TraceFidelity.L0
    runtime_version: str = "legacy-adapter.v1"
    evaluator_version: str = "evaluation.v1"
    provenance: Mapping[str, Any] = ()
    schema_version: str = CANONICAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.strategy_id or not self.instrument:
            raise ValueError("strategy_id and instrument are required")
        object.__setattr__(self, "decision_time", _timestamp(self.decision_time))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if self.trace.fidelity != self.trace_fidelity:
            raise ValueError("evaluation and trace fidelity must match")
        for reason in self.reason_codes:
            _REGISTRY.require(reason)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version, "parameter_set_id": self.parameter_set_id,
                "instrument": self.instrument, "direction": self.direction,
                "decision_time": self.decision_time, "candidate_id": self.candidate_id,
                "decision": self.decision.value,
                "reason_codes": [reason.to_dict() for reason in self.reason_codes],
                "trace": self.trace.to_dict(), "trace_fidelity": self.trace_fidelity.value,
                "runtime_version": self.runtime_version, "evaluator_version": self.evaluator_version,
                "provenance": dict(self.provenance) if isinstance(self.provenance, Mapping) else {}}

    @property
    def evaluation_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.to_dict())
