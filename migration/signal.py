from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from core.strategies.evaluation import Decision, DecisionTrace, Evaluation, StageResult, StageStatus, TraceFidelity, canonical_hash
from postgres.foundation import persist_evaluation
from postgres.db import transaction
from .tailer import AppendOnlyTailer, QuarantinedRecordError, TailerResult


def _stable(prefix: str, value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def _optional_text(raw: Mapping[str, Any], field: str) -> str | None:
    value = raw.get(field)
    if value is not None and not isinstance(value, str):
        raise QuarantinedRecordError(f"{field} must be text when present", failure_class="CANONICAL_VALIDATION")
    return value


def _validate_numeric_fields(raw: Mapping[str, Any]) -> None:
    for field in ("entry_price", "stop_price", "risk_distance", "target_price", "target_distance", "target_r"):
        value = raw.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or isinstance(value, (dict, list)):
            raise QuarantinedRecordError(f"{field} must be a finite numeric value", failure_class="CANONICAL_VALIDATION")
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise QuarantinedRecordError(f"{field} must be a finite numeric value", failure_class="CANONICAL_VALIDATION") from exc
        if not numeric.is_finite():
            raise QuarantinedRecordError(f"{field} must be a finite numeric value", failure_class="CANONICAL_VALIDATION")


def _utc(value: Any) -> str:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        raise ValueError("decision_time must be an ISO-8601 timestamp, not epoch-only")
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("decision_time is required")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("decision_time requires timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


_PROVENANCE_ALLOWLIST = frozenset({"classification", "source_config_hash", "source_data_age", "source_market_data_timestamp", "source_process", "source_strategy_fingerprint", "gap_recovery", "orchestrator_freeze_timestamp"})
_ENTRY_MECHANISM_FIELD = "entry_mechanisms"


def _entry_mechanisms(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate and canonicalize the producer's set-like mechanism collection."""
    # Current StrategySignal.to_dict() still uses the singular wire key while
    # carrying a tuple (JSON array). Map that exact producer contract into the
    # plural canonical domain field; scalar values remain invalid.
    if _ENTRY_MECHANISM_FIELD in raw and "entry_mechanism" in raw:
        raise QuarantinedRecordError("provide only entry_mechanisms", failure_class="CANONICAL_VALIDATION")
    if _ENTRY_MECHANISM_FIELD not in raw and "entry_mechanism" not in raw:
        raise QuarantinedRecordError("entry_mechanisms is required", failure_class="CANONICAL_VALIDATION")
    value = raw.get(_ENTRY_MECHANISM_FIELD, raw.get("entry_mechanism"))
    if not isinstance(value, (list, tuple)):
        raise QuarantinedRecordError("entry_mechanisms must be an array of text values", failure_class="CANONICAL_VALIDATION")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise QuarantinedRecordError("entry_mechanisms elements must be non-empty text", failure_class="CANONICAL_VALIDATION")
    if len(set(value)) != len(value):
        raise QuarantinedRecordError("entry_mechanisms must not contain duplicates", failure_class="CANONICAL_VALIDATION")
    # ContextStructureRetrace emits sorted(set(...)); other active adapters
    # emit singleton tuples. Mechanisms are membership facts, not a sequence.
    return tuple(sorted(value))


@dataclass(frozen=True)
class CanonicalSignal:
    signal_id: str
    candidate_id: str
    evaluation: Evaluation
    source_reference: Mapping[str, Any]
    source_hash: str
    as_of: str | None
    terminal_state: str
    fields: Mapping[str, Any]
    strategy_metadata: Mapping[str, Any]
    source_provenance: Mapping[str, Any]

    @property
    def canonical_hash(self) -> str:
        return self.evaluation.evaluation_hash

    @property
    def entry_signal_hash(self) -> str:
        return str(self.fields["entry_signal_hash"])


def canonical_signal(raw: Mapping[str, Any], *, source_reference: str | Mapping[str, Any] = "") -> CanonicalSignal:
    if not isinstance(raw, Mapping):
        raise QuarantinedRecordError("signal record must be a JSON object", failure_class="CANONICAL_VALIDATION")
    try:
        raw_hash = canonical_hash(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QuarantinedRecordError(f"signal record is not canonical JSON: {exc}", failure_class="CANONICAL_VALIDATION") from exc
    for field in ("signal_id", "strategy_id"):
        if field not in raw:
            raise QuarantinedRecordError(f"required signal identity field is missing: {field}", failure_class="CANONICAL_VALIDATION")
        if not isinstance(raw[field], str):
            raise QuarantinedRecordError(f"{field} must be text", failure_class="CANONICAL_VALIDATION")
    signal_id = raw["signal_id"]
    strategy_id = raw["strategy_id"]
    if not signal_id.strip():
        raise QuarantinedRecordError("signal_id must be non-empty", failure_class="CANONICAL_VALIDATION")
    if not strategy_id.strip():
        raise QuarantinedRecordError("strategy_id must be non-empty", failure_class="CANONICAL_VALIDATION")
    _validate_numeric_fields(raw)
    strategy_version_value = raw.get("strategy_version")
    if strategy_version_value is not None and not isinstance(strategy_version_value, str):
        raise QuarantinedRecordError("strategy_version must be text", failure_class="CANONICAL_VALIDATION")
    strategy_version = strategy_version_value or "UNKNOWN"
    instrument_value = raw.get("canonical_symbol") or raw.get("symbol") or raw.get("broker_symbol_hint")
    if not isinstance(instrument_value, str) or not instrument_value.strip():
        raise QuarantinedRecordError("instrument/symbol is required", failure_class="CANONICAL_VALIDATION")
    instrument = instrument_value
    try:
        decision_value = raw.get("decision_time")
        if decision_value is None:
            decision_value = raw.get("signal_timestamp")
        if decision_value is None:
            decision_value = raw.get("created_at")
        decision_time = _utc(decision_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise QuarantinedRecordError(f"invalid decision_time: {exc}", failure_class="CANONICAL_VALIDATION") from exc
    candidate_id_value = raw.get("candidate_id")
    if candidate_id_value is not None and not isinstance(candidate_id_value, str):
        raise QuarantinedRecordError("candidate_id must be text when present", failure_class="CANONICAL_VALIDATION")
    candidate_id = candidate_id_value or _stable("CAND", {"strategy_id": strategy_id, "strategy_version": strategy_version, "strategy_instance_id": raw.get("strategy_instance_id"), "source_event_id": raw.get("source_event_id"), "entry_opportunity_id": raw.get("entry_opportunity_id")})
    metadata_value = raw.get("strategy_metadata", {})
    provenance_value = raw.get("provenance", {})
    if metadata_value is None:
        metadata_value = {}
    if provenance_value is None:
        provenance_value = {}
    if not isinstance(metadata_value, Mapping):
        raise QuarantinedRecordError("strategy_metadata must be an object", failure_class="CANONICAL_VALIDATION")
    if not isinstance(provenance_value, Mapping):
        raise QuarantinedRecordError("provenance must be an object", failure_class="CANONICAL_VALIDATION")
    if raw.get("direction") is not None and not isinstance(raw.get("direction"), str):
        raise QuarantinedRecordError("direction must be text when present", failure_class="CANONICAL_VALIDATION")
    for field in ("strategy_instance_id", "entry_type", "economic_position_id", "entry_opportunity_id", "setup_id", "source_event_id"):
        _optional_text(raw, field)
    entry_mechanisms = _entry_mechanisms(raw)
    strategy_metadata = dict(metadata_value)
    parameter_set_ref = raw.get("parameter_set_ref") or raw.get("parameter_set_id")
    if parameter_set_ref is not None and not isinstance(parameter_set_ref, str):
        raise QuarantinedRecordError("parameter_set_ref must be text when present", failure_class="CANONICAL_VALIDATION")
    parameter_status = "EXPLICIT" if parameter_set_ref else "LEGACY_IMPLICIT_IN_STRATEGY_ID"
    try:
        source_ref = {"source_reference": source_reference} if isinstance(source_reference, str) else dict(source_reference)
    except (TypeError, ValueError) as exc:
        raise QuarantinedRecordError(f"invalid source reference: {exc}", failure_class="CANONICAL_VALIDATION") from exc
    source_provenance = dict(provenance_value)
    hashed_provenance = {k: v for k, v in source_provenance.items() if k in _PROVENANCE_ALLOWLIST}
    metadata = {k: raw.get(k) for k in ("symbol", "canonical_symbol", "entry_type", "timeframe", "lower_timeframe", "higher_timeframes") if k in raw}
    reason = None
    stage = StageResult("legacy_signal", StageStatus.PASS, primitive_id="legacy.strategy_signal", observed={"signal_id": signal_id}, metadata=metadata)
    trace = DecisionTrace((stage,), Decision.SIGNAL, fidelity=TraceFidelity.L1)
    evaluation = Evaluation(strategy_id=strategy_id, instrument=instrument, decision_time=decision_time, decision=Decision.SIGNAL, trace=trace, direction=raw.get("direction"), strategy_version=strategy_version, parameter_set_id=parameter_set_ref, candidate_id=candidate_id, reason_codes=(), trace_fidelity=TraceFidelity.L1, runtime_version=str(raw.get("runtime_version") or "legacy-signal-tailer.v1"), evaluator_version="p2-a1-signal-ingest.v1", provenance=hashed_provenance)
    signal_emitted_at = raw.get("signal_emitted_at")
    if signal_emitted_at is None or signal_emitted_at == "":
        signal_emitted_at = raw.get("created_at")
    if signal_emitted_at is not None:
        try:
            signal_emitted_at = _utc(signal_emitted_at)
        except (TypeError, ValueError, OverflowError) as exc:
            raise QuarantinedRecordError(f"invalid signal_emitted_at: {exc}", failure_class="CANONICAL_VALIDATION") from exc
    fields = {"signal_id": signal_id, "candidate_id": candidate_id, "evaluation_id": evaluation.evaluation_hash, "evaluation_hash": evaluation.evaluation_hash, "trace_hash": trace.trace_hash, "strategy_ref": f"{strategy_id}@{strategy_version}", "strategy_id": strategy_id, "strategy_version": strategy_version, "parameter_set_ref": parameter_set_ref, "parameter_set_status": parameter_status, "strategy_instance_id": raw.get("strategy_instance_id"), "instrument": instrument, "direction": raw.get("direction"), "decision_time": decision_time, "signal_emitted_at": signal_emitted_at, "entry_type": raw.get("entry_type"), "entry_price": raw.get("entry_price"), "entry_mechanisms": entry_mechanisms, "stop_price": raw.get("stop_price"), "risk_distance": raw.get("risk_distance"), "target_price": raw.get("target_price"), "target_distance": raw.get("target_distance"), "target_r": raw.get("target_r"), "economic_position_id": raw.get("economic_position_id"), "entry_opportunity_id": raw.get("entry_opportunity_id"), "setup_id": raw.get("setup_id"), "source_event_id": raw.get("source_event_id")}
    semantic = {k: v for k, v in fields.items() if k not in {"evaluation_id", "evaluation_hash", "trace_hash"}}
    fields["entry_signal_hash"] = canonical_hash({**semantic, "strategy_metadata": strategy_metadata, "source_provenance": hashed_provenance})
    source_hash = str(raw.get("source_hash") or raw_hash)
    return CanonicalSignal(signal_id, candidate_id, evaluation, source_ref, source_hash, raw.get("as_of") or raw.get("signal_timestamp"), "ENTRY_SIGNAL_CREATED", fields, strategy_metadata, source_provenance)


def ingest_signal(conn: Any, signal: CanonicalSignal, *, occurred_at: str | None = None) -> bool:
    occurred_at = occurred_at or signal.fields.get("signal_emitted_at") or signal.evaluation.decision_time
    f = signal.fields
    with transaction(conn):
        with conn.cursor() as cur:
            candidate_payload = {key: value for key, value in f.items() if key != "entry_mechanisms"}
            cur.execute("""INSERT INTO strategy.candidates (candidate_id,strategy_id,strategy_version_id,instrument,detected_at,status,source_event_id,payload,canonical_hash,lifecycle_state)
                VALUES (%s,%s,NULL,%s,%s,'DETECTED',%s,%s::jsonb,%s,'ENTRY_SIGNAL_CREATED') ON CONFLICT (candidate_id) DO NOTHING""", (signal.candidate_id, f["strategy_id"], f["instrument"], f["decision_time"], f["source_event_id"], json.dumps(candidate_payload, sort_keys=True), signal.canonical_hash))
        persist_evaluation(conn, signal.evaluation, strategy_version_id=None)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.entry_signals (signal_id,candidate_id,evaluation_id,strategy_ref,strategy_id,strategy_version,parameter_set_ref,parameter_set_status,strategy_instance_id,instrument,direction,decision_time,signal_emitted_at,entry_type,entry_price,stop_price,risk_distance,target_price,target_distance,target_r,economic_position_id,entry_opportunity_id,setup_id,source_event_id,source_id,source_offset,evidence_class,cutoff_id,source_provenance,evaluation_hash,trace_hash,terminal_state,strategy_metadata,entry_signal_hash)
                VALUES (%(signal_id)s,%(candidate_id)s,%(evaluation_id)s,%(strategy_ref)s,%(strategy_id)s,%(strategy_version)s,%(parameter_set_ref)s,%(parameter_set_status)s,%(strategy_instance_id)s,%(instrument)s,%(direction)s,%(decision_time)s,%(signal_emitted_at)s,%(entry_type)s,%(entry_price)s,%(stop_price)s,%(risk_distance)s,%(target_price)s,%(target_distance)s,%(target_r)s,%(economic_position_id)s,%(entry_opportunity_id)s,%(setup_id)s,%(source_event_id)s,%(source_id)s,%(source_offset)s,%(evidence_class)s,%(cutoff_id)s,%(source_provenance)s::jsonb,%(evaluation_hash)s,%(trace_hash)s,%(terminal_state)s,%(strategy_metadata)s::jsonb,%(entry_signal_hash)s) ON CONFLICT (signal_id) DO NOTHING""", {**f, "source_id": signal.source_reference.get("source_id"), "source_offset": signal.source_reference.get("source_offset"), "evidence_class": signal.source_reference.get("evidence_class"), "cutoff_id": signal.source_reference.get("cutoff_id"), "source_provenance": json.dumps(signal.source_provenance, sort_keys=True), "terminal_state": signal.terminal_state, "strategy_metadata": json.dumps(signal.strategy_metadata, sort_keys=True)})
            cur.execute("""INSERT INTO strategy.signals (signal_id,evaluation_id,candidate_id,strategy_id,instrument,direction,signal_time,payload,canonical_hash,lifecycle_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'ENTRY_SIGNAL_CREATED') ON CONFLICT (signal_id) DO NOTHING""", (signal.signal_id, f["evaluation_id"], signal.candidate_id, f["strategy_id"], f["instrument"], f["direction"], f["decision_time"], json.dumps(candidate_payload, sort_keys=True), signal.entry_signal_hash))
            for position, mechanism in enumerate(f["entry_mechanisms"]):
                cur.execute("""INSERT INTO strategy.entry_signal_mechanisms
                    (entry_signal_id, mechanism, position) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING""", (signal.signal_id, mechanism, position))
            event_data = {k: f[k] for k in ("signal_id", "candidate_id", "evaluation_id", "evaluation_hash", "trace_hash", "entry_signal_hash", "strategy_ref", "strategy_id", "instrument", "direction", "decision_time", "signal_emitted_at")}
            event_data["entry_mechanisms"] = list(f["entry_mechanisms"])
            event_payload = json.dumps(event_data, sort_keys=True, separators=(",", ":"))
            for event_id, event_type, aggregate_type, aggregate_id in ((f"{signal.candidate_id}:candidate.detected", "strategy.candidate.detected.v1", "candidate", signal.candidate_id), (f"{signal.signal_id}:entry.created", "signal.entry.created.v1", "signal", signal.signal_id)):
                cur.execute("""INSERT INTO platform.outbox_events (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,occurred_at) VALUES (%s,%s,%s,%s,1,'event-envelope.v1',%s::jsonb,%s) ON CONFLICT (event_id) DO NOTHING""", (event_id, event_type, aggregate_type, aggregate_id, event_payload, occurred_at))
    return True


def load_entry_mechanisms(conn: Any, signal_id: str) -> tuple[str, ...]:
    """Reconstruct the canonical order from normalized child rows."""
    with conn.cursor() as cur:
        cur.execute("""SELECT mechanism FROM strategy.entry_signal_mechanisms
            WHERE entry_signal_id=%s ORDER BY position""", (signal_id,))
        return tuple(row[0] for row in cur.fetchall())


class LegacySignalTailer:
    def __init__(self, source: Path, checkpoint: Path, conn: Any, *, evidence_class: str = "LIVE",
                 cutoff_offset: int | None = None, cutoff_id: str | None = None):
        self.conn, self.source = conn, source
        self.evidence_class = evidence_class
        self.cutoff_offset = cutoff_offset
        self.cutoff_id = cutoff_id
        self.tailer = AppendOnlyTailer(source, checkpoint, ingest=self._ingest, quarantine=self._quarantine,
                                       reject_rotation=cutoff_offset is not None)
        self.last_malformed: list[dict[str, Any]] = []

    def _ingest(self, raw: dict[str, Any]) -> None:
        evidence_class = "ROTATION_RECOVERY" if raw.get("_source_rotation_replay") else self.evidence_class
        signal = canonical_signal(raw, source_reference={"source_id": str(self.source), "source_offset": raw.get("source_offset"), "evidence_class": evidence_class, "cutoff_id": self.cutoff_id})
        ingest_signal(self.conn, signal, occurred_at=raw.get("signal_emitted_at") or raw.get("created_at"))

    def _quarantine(self, item: dict[str, Any]) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                source_id = str(self.source)
                diagnostic = f"{item.get('failure_class', 'SOURCE_REJECTION')}: {item['error']}"
                cur.execute("""INSERT INTO platform.signal_ingest_quarantine(source_id,source_offset,raw_sha256,raw_payload,error) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (source_id,source_offset,raw_sha256) DO NOTHING""", (source_id, item["source_offset"], item["raw_sha256"], item["raw"], diagnostic))

    def run_once(self) -> TailerResult:
        if self.cutoff_offset is not None and self.tailer.checkpoint.exists():
            state = json.loads(self.tailer.checkpoint.read_text(encoding="utf-8"))
            if int(state.get("offset", -1)) < self.cutoff_offset:
                raise RuntimeError("durable checkpoint is before the immutable P2 cutoff")
        result = self.tailer.run_once(); self.last_malformed = result.malformed
        return result
