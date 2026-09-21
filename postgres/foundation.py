"""V1.2 persistence boundary for canonical evaluations and platform events.

The functions in this module deliberately accept an existing connection.  A
caller chooses the transaction boundary; state plus outbox must be invoked in
the same transaction and nothing here falls back to JSONL.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping

from core.strategies.evaluation import Evaluation, ReasonCode, canonical_bytes, default_reason_codes

DATABASE_SCHEMA_VERSION = "009"


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _reason_rows(evaluation: Evaluation) -> list[ReasonCode]:
    rows = list(evaluation.reason_codes)
    for stage in evaluation.trace.stages:
        if stage.reason_code and stage.reason_code not in rows:
            rows.append(stage.reason_code)
    return rows


def persist_evaluation(conn: Any, evaluation: Evaluation) -> str:
    """Persist an immutable evaluation and its trace; caller commits."""
    evaluation_id = evaluation.evaluation_hash
    with conn.cursor() as cur:
        for reason in _reason_rows(evaluation):
            cur.execute("""INSERT INTO platform.reason_codes(code, version, category, description, terminal)
                          VALUES (%s,%s,%s,%s,%s) ON CONFLICT (code, version) DO NOTHING""",
                        (reason.code, reason.version, reason.category, reason.description, reason.terminal))
        cur.execute("""INSERT INTO strategy.evaluations
            (evaluation_id, strategy_id, strategy_version_id, parameter_set_id, instrument, direction,
             decision_time, candidate_id, decision, trace_fidelity, runtime_version, evaluator_version,
             provenance, canonical_payload, canonical_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
            ON CONFLICT (evaluation_id) DO NOTHING""",
                    (evaluation_id, evaluation.strategy_id, evaluation.strategy_version,
                     evaluation.parameter_set_id, evaluation.instrument, evaluation.direction,
                     evaluation.decision_time, evaluation.candidate_id, evaluation.decision.value,
                     evaluation.trace_fidelity.value, evaluation.runtime_version, evaluation.evaluator_version,
                     _json(evaluation.provenance), _json(evaluation.to_dict()), evaluation.evaluation_hash))
        cur.execute("""INSERT INTO strategy.decision_traces(evaluation_id, trace_version, trace_hash, canonical_payload)
                      VALUES (%s,%s,%s,%s::jsonb) ON CONFLICT (evaluation_id) DO NOTHING""",
                    (evaluation_id, evaluation.trace.trace_version, evaluation.trace.trace_hash,
                     _json(evaluation.trace.to_dict())))
        for ordinal, reason in enumerate(evaluation.reason_codes):
            cur.execute("""INSERT INTO strategy.evaluation_reason_codes(evaluation_id, ordinal, code, version)
                          VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (evaluation_id, ordinal, reason.code, reason.version))
        for ordinal, stage in enumerate(evaluation.trace.stages):
            reason = stage.reason_code
            cur.execute("""INSERT INTO strategy.stage_results
                (evaluation_id, ordinal, stage_id, status, primitive_id, observed, expected, margin,
                 evidence_times, reason_code, reason_version, metadata)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb)
                ON CONFLICT DO NOTHING""",
                        (evaluation_id, ordinal, stage.stage_id, stage.status.value, stage.primitive_id,
                         _json(stage.observed), _json(stage.expected), _json(stage.margin),
                         _json(stage.evidence_times), reason.code if reason else None,
                         reason.version if reason else None, _json(stage.metadata)))
    return evaluation_id


def create_signal_with_outbox(conn: Any, signal: Mapping[str, Any], *, event_id: str,
                              occurred_at: str, correlation_id: str | None = None,
                              causation_id: str | None = None) -> str:
    """Atomically insert a signal projection and its pending event."""
    signal_id = str(signal["signal_id"])
    payload = _json(signal)
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO strategy.signals
            (signal_id, evaluation_id, candidate_id, strategy_id, instrument, direction,
             signal_time, payload, canonical_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
            ON CONFLICT (signal_id) DO NOTHING""",
                    (signal_id, signal.get("evaluation_id"), signal.get("candidate_id"),
                     signal["strategy_id"], signal["instrument"], signal.get("direction"),
                     signal["signal_time"], payload, signal.get("canonical_hash")))
        cur.execute("""INSERT INTO platform.outbox_events
            (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
             schema_version, payload, occurred_at, correlation_id, causation_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT (event_id) DO NOTHING""",
                    (event_id, "signal.entry.created.v1", "signal", signal_id, signal.get("aggregate_version"),
                     "event-envelope.v1", payload, occurred_at, correlation_id, causation_id))
    return signal_id


def claim_inbox(conn: Any, consumer_name: str, event_id: str) -> bool:
    """Claim once; duplicate JetStream delivery returns False."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO platform.inbox_events(consumer_name, event_id)
                      VALUES (%s,%s) ON CONFLICT (consumer_name, event_id) DO NOTHING""",
                    (consumer_name, event_id))
        return cur.rowcount == 1


def mark_inbox_processed(conn: Any, consumer_name: str, event_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""UPDATE platform.inbox_events SET status='PROCESSED', processed_at=now()
                      WHERE consumer_name=%s AND event_id=%s""", (consumer_name, event_id))


def mark_outbox_published(conn: Any, event_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""UPDATE platform.outbox_events
                      SET publish_status='PUBLISHED', published_at=now(), attempts=attempts+1
                      WHERE event_id=%s""", (event_id,))


def mark_outbox_failed(conn: Any, event_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""UPDATE platform.outbox_events
                      SET publish_status='FAILED', last_error=%s, attempts=attempts+1
                      WHERE event_id=%s""", (error, event_id))


def create_execution_intent(conn: Any, *, intent_id: str, idempotency_key: str,
                            intent_type: str, payload: Mapping[str, Any],
                            aggregate_id: str | None = None) -> str:
    """Insert-or-return by idempotency key; broker execution is not performed."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO execution.intents
            (intent_id, idempotency_key, intent_type, aggregate_id, status, payload)
            VALUES (%s,%s,%s,%s,'PENDING',%s::jsonb)
            ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key=EXCLUDED.idempotency_key
            RETURNING intent_id""", (intent_id, idempotency_key, intent_type, aggregate_id, _json(payload)))
        return str(cur.fetchone()[0])


def foundation_health(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), current_user")
        database, user = cur.fetchone()
        cur.execute("SELECT COALESCE(sum((publish_status <> 'PUBLISHED')::int), 0), COALESCE(sum(attempts), 0) FROM platform.outbox_events")
        pending, retries = cur.fetchone()
        cur.execute("SELECT COALESCE(count(*), 0) FROM platform.inbox_events WHERE status='RECEIVED'")
        inbox_pending = cur.fetchone()[0]
    return {"database": database, "user": user, "outbox_unpublished": pending,
            "outbox_attempts": retries, "inbox_received_pending": inbox_pending}
