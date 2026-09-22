from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.strategies.evaluation import Decision, DecisionTrace, Evaluation, StageResult, StageStatus, TraceFidelity, canonical_hash
from postgres.foundation import persist_evaluation
from postgres.db import transaction
from .tailer import AppendOnlyTailer, QuarantinedRecordError, TailerResult


def _stable(prefix: str, value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


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
    signal_id = str(raw["signal_id"])
    strategy_id = str(raw["strategy_id"])
    strategy_version = str(raw.get("strategy_version") or "UNKNOWN")
    instrument = str(raw.get("canonical_symbol") or raw.get("symbol") or raw.get("broker_symbol_hint"))
    decision_time = _utc(raw.get("decision_time") or raw.get("signal_timestamp") or raw.get("created_at"))
    candidate_id = str(raw.get("candidate_id") or _stable("CAND", {"strategy_id": strategy_id, "strategy_version": strategy_version, "strategy_instance_id": raw.get("strategy_instance_id"), "source_event_id": raw.get("source_event_id"), "entry_opportunity_id": raw.get("entry_opportunity_id")}))
    strategy_metadata = dict(raw.get("strategy_metadata") or {})
    parameter_set_ref = raw.get("parameter_set_ref") or raw.get("parameter_set_id")
    parameter_status = "EXPLICIT" if parameter_set_ref else "LEGACY_IMPLICIT_IN_STRATEGY_ID"
    source_ref = {"source_reference": source_reference} if isinstance(source_reference, str) else dict(source_reference)
    source_provenance = dict(raw.get("provenance") or {})
    hashed_provenance = {k: v for k, v in source_provenance.items() if k in _PROVENANCE_ALLOWLIST}
    metadata = {k: raw.get(k) for k in ("symbol", "canonical_symbol", "entry_type", "timeframe", "lower_timeframe", "higher_timeframes") if k in raw}
    reason = None
    stage = StageResult("legacy_signal", StageStatus.PASS, primitive_id="legacy.strategy_signal", observed={"signal_id": signal_id}, metadata=metadata)
    trace = DecisionTrace((stage,), Decision.SIGNAL, fidelity=TraceFidelity.L1)
    evaluation = Evaluation(strategy_id=strategy_id, instrument=instrument, decision_time=decision_time, decision=Decision.SIGNAL, trace=trace, direction=raw.get("direction"), strategy_version=strategy_version, parameter_set_id=parameter_set_ref, candidate_id=candidate_id, reason_codes=(), trace_fidelity=TraceFidelity.L1, runtime_version=str(raw.get("runtime_version") or "legacy-signal-tailer.v1"), evaluator_version="p2-a1-signal-ingest.v1", provenance=hashed_provenance)
    signal_emitted_at = raw.get("signal_emitted_at") or raw.get("created_at")
    if signal_emitted_at is not None:
        signal_emitted_at = _utc(signal_emitted_at)
    fields = {"signal_id": signal_id, "candidate_id": candidate_id, "evaluation_id": evaluation.evaluation_hash, "evaluation_hash": evaluation.evaluation_hash, "trace_hash": trace.trace_hash, "strategy_ref": f"{strategy_id}@{strategy_version}", "strategy_id": strategy_id, "strategy_version": strategy_version, "parameter_set_ref": parameter_set_ref, "parameter_set_status": parameter_status, "strategy_instance_id": raw.get("strategy_instance_id"), "instrument": instrument, "direction": raw.get("direction"), "decision_time": decision_time, "signal_emitted_at": signal_emitted_at, "entry_type": raw.get("entry_type"), "entry_price": raw.get("entry_price"), "entry_mechanism": raw.get("entry_mechanism"), "stop_price": raw.get("stop_price"), "risk_distance": raw.get("risk_distance"), "target_price": raw.get("target_price"), "target_distance": raw.get("target_distance"), "target_r": raw.get("target_r"), "economic_position_id": raw.get("economic_position_id"), "entry_opportunity_id": raw.get("entry_opportunity_id"), "setup_id": raw.get("setup_id"), "source_event_id": raw.get("source_event_id")}
    semantic = {k: v for k, v in fields.items() if k not in {"evaluation_id", "evaluation_hash", "trace_hash"}}
    fields["entry_signal_hash"] = canonical_hash({**semantic, "strategy_metadata": strategy_metadata, "source_provenance": hashed_provenance})
    source_hash = str(raw.get("source_hash") or canonical_hash(raw))
    return CanonicalSignal(signal_id, candidate_id, evaluation, source_ref, source_hash, raw.get("as_of") or raw.get("signal_timestamp"), "ENTRY_SIGNAL_CREATED", fields, strategy_metadata, source_provenance)


def ingest_signal(conn: Any, signal: CanonicalSignal, *, occurred_at: str | None = None) -> bool:
    occurred_at = occurred_at or signal.fields.get("signal_emitted_at") or signal.evaluation.decision_time
    f = signal.fields
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.candidates (candidate_id,strategy_id,strategy_version_id,instrument,detected_at,status,source_event_id,payload,canonical_hash,lifecycle_state)
                VALUES (%s,%s,NULL,%s,%s,'DETECTED',%s,%s::jsonb,%s,'ENTRY_SIGNAL_CREATED') ON CONFLICT (candidate_id) DO NOTHING""", (signal.candidate_id, f["strategy_id"], f["instrument"], f["decision_time"], f["source_event_id"], json.dumps(f, sort_keys=True), signal.canonical_hash))
        persist_evaluation(conn, signal.evaluation, strategy_version_id=None)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.entry_signals (signal_id,candidate_id,evaluation_id,strategy_ref,strategy_id,strategy_version,parameter_set_ref,parameter_set_status,strategy_instance_id,instrument,direction,decision_time,signal_emitted_at,entry_type,entry_price,entry_mechanism,stop_price,risk_distance,target_price,target_distance,target_r,economic_position_id,entry_opportunity_id,setup_id,source_event_id,source_ref,source_provenance,runtime_provenance,evaluation_hash,trace_hash,terminal_state,strategy_metadata,entry_signal_hash)
                VALUES (%(signal_id)s,%(candidate_id)s,%(evaluation_id)s,%(strategy_ref)s,%(strategy_id)s,%(strategy_version)s,%(parameter_set_ref)s,%(parameter_set_status)s,%(strategy_instance_id)s,%(instrument)s,%(direction)s,%(decision_time)s,%(signal_emitted_at)s,%(entry_type)s,%(entry_price)s,%(entry_mechanism)s,%(stop_price)s,%(risk_distance)s,%(target_price)s,%(target_distance)s,%(target_r)s,%(economic_position_id)s,%(entry_opportunity_id)s,%(setup_id)s,%(source_event_id)s,%(source_ref)s::jsonb,%(source_provenance)s::jsonb,%(runtime_provenance)s::jsonb,%(evaluation_hash)s,%(trace_hash)s,%(terminal_state)s,%(strategy_metadata)s::jsonb,%(entry_signal_hash)s) ON CONFLICT (signal_id) DO NOTHING""", {**f, "source_ref": json.dumps(signal.source_reference, sort_keys=True), "source_provenance": json.dumps(signal.source_provenance, sort_keys=True), "runtime_provenance": json.dumps({"runtime_version": signal.evaluation.runtime_version, "evaluator_version": signal.evaluation.evaluator_version}, sort_keys=True), "terminal_state": signal.terminal_state, "strategy_metadata": json.dumps(signal.strategy_metadata, sort_keys=True)})
            cur.execute("""INSERT INTO strategy.signals (signal_id,evaluation_id,candidate_id,strategy_id,instrument,direction,signal_time,payload,canonical_hash,lifecycle_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'ENTRY_SIGNAL_CREATED') ON CONFLICT (signal_id) DO NOTHING""", (signal.signal_id, f["evaluation_id"], signal.candidate_id, f["strategy_id"], f["instrument"], f["direction"], f["decision_time"], json.dumps(f, sort_keys=True), signal.entry_signal_hash))
            event_payload = json.dumps({k: f[k] for k in ("signal_id", "candidate_id", "evaluation_id", "evaluation_hash", "trace_hash", "entry_signal_hash", "strategy_ref", "strategy_id", "instrument", "direction", "decision_time", "signal_emitted_at")}, sort_keys=True)
            for event_id, event_type, aggregate_type, aggregate_id in ((f"{signal.candidate_id}:candidate.detected", "strategy.candidate.detected.v1", "candidate", signal.candidate_id), (f"{signal.signal_id}:entry.created", "signal.entry.created.v1", "signal", signal.signal_id)):
                cur.execute("""INSERT INTO platform.outbox_events (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,occurred_at) VALUES (%s,%s,%s,%s,1,'event-envelope.v1',%s::jsonb,%s) ON CONFLICT (event_id) DO NOTHING""", (event_id, event_type, aggregate_type, aggregate_id, event_payload, occurred_at))
    return True


class LegacySignalTailer:
    def __init__(self, source: Path, checkpoint: Path, conn: Any, *, evidence_class: str = "LIVE"):
        self.conn, self.source = conn, source
        self.evidence_class = evidence_class
        self.tailer = AppendOnlyTailer(source, checkpoint, ingest=self._ingest, quarantine=self._quarantine)
        self.last_malformed: list[dict[str, Any]] = []

    def _ingest(self, raw: dict[str, Any]) -> None:
        try:
            evidence_class = "ROTATION_RECOVERY" if raw.get("_source_rotation_replay") else self.evidence_class
            signal = canonical_signal(raw, source_reference={"source_id": str(self.source), "source_offset": raw.get("source_offset"), "evidence_class": evidence_class})
        except (ValueError, TypeError, KeyError) as exc:
            raise QuarantinedRecordError(str(exc)) from exc
        ingest_signal(self.conn, signal, occurred_at=raw.get("signal_emitted_at") or raw.get("created_at"))

    def _quarantine(self, item: dict[str, Any]) -> None:
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO platform.signal_ingest_quarantine(source_id,source_offset,raw_sha256,raw_payload,error) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (source_id,source_offset,raw_sha256) DO NOTHING""", (str(self.source), item["source_offset"], item["raw_sha256"], item["raw"], item["error"]))

    def run_once(self) -> TailerResult:
        result = self.tailer.run_once(); self.last_malformed = result.malformed
        return result
