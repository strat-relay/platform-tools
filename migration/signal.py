from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.strategies.evaluation import Decision, DecisionTrace, Evaluation, StageResult, StageStatus, TraceFidelity, canonical_hash, default_reason_codes
from postgres.foundation import persist_evaluation
from postgres.db import transaction
from .tailer import AppendOnlyTailer, TailerResult


def _stable(prefix: str, value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:24]}"


def _utc(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None: raise ValueError("decision_time requires timezone")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return str(value)


@dataclass(frozen=True)
class CanonicalSignal:
    signal_id: str
    candidate_id: str
    evaluation: Evaluation
    source_reference: str
    source_hash: str
    as_of: str | None
    terminal_state: str

    @property
    def canonical_hash(self) -> str:
        return self.evaluation.evaluation_hash


def canonical_signal(raw: Mapping[str, Any], *, source_reference: str = "") -> CanonicalSignal:
    """Map existing StrategySignal-shaped output without adding strategy semantics."""
    signal_id = str(raw["signal_id"])
    candidate_id = str(raw.get("candidate_id") or _stable("CAND", {
        "strategy_id": raw.get("strategy_id"), "strategy_version": raw.get("strategy_version"),
        "strategy_instance_id": raw.get("strategy_instance_id"), "source_event_id": raw.get("source_event_id"),
        "entry_opportunity_id": raw.get("entry_opportunity_id"),
    }))
    reason = default_reason_codes().get("NO_VALID_ENTRY_SIGNAL")
    metadata = {k: raw.get(k) for k in ("symbol", "canonical_symbol", "entry_type", "timeframe", "lower_timeframe", "higher_timeframes") if k in raw}
    stage = StageResult("legacy_signal", StageStatus.PASS, primitive_id="legacy.strategy_signal", observed={"signal_id": signal_id}, metadata=metadata)
    trace = DecisionTrace((stage,), Decision.SIGNAL, fidelity=TraceFidelity.L1)
    provenance = dict(raw.get("provenance") or {})
    provenance.update({"legacy_source_reference": source_reference, "legacy_source_hash": raw.get("source_hash"), "as_of": raw.get("as_of") or raw.get("signal_timestamp")})
    # Explicitly exclude outcome/future fields from decision evidence.
    provenance = {k: v for k, v in provenance.items() if k not in {"outcome", "future_return", "pnl", "exit", "fill"}}
    evaluation = Evaluation(
        strategy_id=str(raw["strategy_id"]), instrument=str(raw.get("canonical_symbol") or raw.get("symbol") or raw.get("broker_symbol_hint")),
        decision_time=_utc(raw.get("decision_time") or raw.get("signal_timestamp") or raw.get("created_at")),
        decision=Decision.SIGNAL, trace=trace, direction=raw.get("direction"), strategy_version=raw.get("strategy_version"),
        parameter_set_id=raw.get("parameter_set_id"), candidate_id=candidate_id, reason_codes=(), trace_fidelity=TraceFidelity.L1,
        runtime_version=str(raw.get("runtime_version") or "legacy-signal-tailer.v1"), evaluator_version="p2-signal-ingest.v1", provenance=provenance)
    source_hash = str(raw.get("source_hash") or canonical_hash(raw))
    return CanonicalSignal(signal_id, candidate_id, evaluation, source_reference, source_hash, raw.get("as_of") or raw.get("signal_timestamp"), "ENTRY_SIGNAL_CREATED")


def ingest_signal(conn: Any, signal: CanonicalSignal, *, occurred_at: str | None = None) -> bool:
    """Persist candidate/evaluation/signal plus outbox in one transaction."""
    occurred_at = occurred_at or signal.evaluation.decision_time
    with transaction(conn):
        with conn.cursor() as cur:
            if signal.evaluation.strategy_version:
                # Existing persistence uses strategy_version_id as the FK.  A
                # legacy version is registered verbatim, preserving identity
                # without inventing a new strategy semantic.
                cur.execute("""INSERT INTO platform.strategy_versions
                    (strategy_version_id, strategy_version, schema_version, source_hash)
                    VALUES (%s,%s,%s,%s) ON CONFLICT (strategy_version_id) DO NOTHING""",
                    (signal.evaluation.strategy_version, signal.evaluation.strategy_version,
                     signal.evaluation.schema_version, signal.source_hash))
            cur.execute("""INSERT INTO strategy.candidates
                (candidate_id,strategy_id,strategy_version_id,instrument,detected_at,status,source_event_id,payload,canonical_hash,lifecycle_state)
                VALUES (%s,%s,%s,%s,%s,'DETECTED',%s,%s::jsonb,%s,'ENTRY_SIGNAL_CREATED')
                ON CONFLICT (candidate_id) DO NOTHING""",
                (signal.candidate_id, signal.evaluation.strategy_id, signal.evaluation.strategy_version,
                 signal.evaluation.instrument, signal.evaluation.decision_time,
                 signal.source_reference, json.dumps(signal.evaluation.to_dict(), sort_keys=True), signal.canonical_hash))
        persist_evaluation(conn, signal.evaluation)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.signals
                (signal_id,evaluation_id,candidate_id,strategy_id,instrument,direction,signal_time,payload,canonical_hash,lifecycle_state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'ENTRY_SIGNAL_CREATED')
                ON CONFLICT (signal_id) DO NOTHING""",
                (signal.signal_id, signal.evaluation.evaluation_hash, signal.candidate_id, signal.evaluation.strategy_id,
                 signal.evaluation.instrument, signal.evaluation.direction, signal.evaluation.decision_time,
                 json.dumps(signal.evaluation.to_dict(), sort_keys=True), signal.canonical_hash))
            event_payload = json.dumps({"signal_id": signal.signal_id, "candidate_id": signal.candidate_id,
                                        "evaluation_id": signal.evaluation.evaluation_hash,
                                        "evaluation_hash": signal.evaluation.evaluation_hash,
                                        "trace_hash": signal.evaluation.trace.trace_hash,
                                        "source_reference": signal.source_reference}, sort_keys=True)
            for event_id, event_type in ((f"{signal.candidate_id}:candidate.detected", "strategy.candidate.detected.v1"),
                                         (f"{signal.signal_id}:entry.created", "signal.entry.created.v1")):
                cur.execute("""INSERT INTO platform.outbox_events
                    (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,occurred_at)
                    VALUES (%s,%s,%s,%s,1,'event-envelope.v1',%s::jsonb,%s)
                    ON CONFLICT (event_id) DO NOTHING""",
                    (event_id, event_type, "signal", signal.signal_id, event_payload, occurred_at))
    return True


class LegacySignalTailer:
    """S0 seam: frozen append-only signal output -> canonical ingest."""
    def __init__(self, source: Path, checkpoint: Path, conn: Any):
        self.conn = conn
        self.tailer = AppendOnlyTailer(source, checkpoint, ingest=self._ingest)
        self.source = source
        self.last_malformed: list[dict[str, Any]] = []

    def _ingest(self, raw: dict[str, Any]) -> None:
        source_ref = f"{self.source}:{raw.get('source_line', '?')}"
        ingest_signal(self.conn, canonical_signal(raw, source_reference=source_ref), occurred_at=raw.get("signal_emitted_at") or raw.get("signal_timestamp"))

    def run_once(self) -> TailerResult:
        result = self.tailer.run_once(); self.last_malformed = result.malformed
        return result
