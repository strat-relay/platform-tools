"""TM-NONE: the first TradeManagerVersion. Every eligible observation -> a persisted, auditable
`HOLD`. No stop movement, no partial profit, no exit, no broker action - it decides nothing
about management (A6 07 section 5, A7 04 section 2). The evaluator is a pure function of
`(observation row, managed_trade row)`; persistence + the publication-gate boundary happen in
the same transaction as the decision, consistent with A6 11 section 1 ("persisted BEFORE
anything reacts to it").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction
from postgres.foundation import claim_inbox, mark_inbox_processed

from .ids import decision_id as _decision_id
from .publication_gate import GateInputs, evaluate_publication_gate

DECISION_CONSUMER_NAME = "trade-manager-shadow"

# A6 08 section 4 "Duplicate / gap / stale" vocabulary, TM-NONE's subset (A7 04 section 2):
# NO_MANAGEMENT_POLICY is the default; the others are recorded when the corresponding condition
# is detected, still as an audited HOLD, never a failure.
REASON_NO_MANAGEMENT_POLICY = "NO_MANAGEMENT_POLICY"
REASON_TRADE_CLOSED = "TRADE_CLOSED"


@dataclass(frozen=True)
class DecisionResult:
    status: str  # RECORDED | DUPLICATE | INBOX_DUPLICATE | OBSERVATION_MISSING
    decision_id: str | None
    action: str | None
    reason_codes: tuple[str, ...] = ()


class TmNoneEvaluator:
    """Pure: given the observation and the trade's current state, always HOLD. No wall clock,
    no random, no provider call - deterministic by construction (A6 11 section 5)."""

    evaluator_id = "tm-none.v1"

    def evaluate(self, *, managed_trade_state: str) -> tuple[str, tuple[str, ...]]:
        if managed_trade_state != "OPEN":
            return "HOLD", (REASON_TRADE_CLOSED,)
        return "HOLD", (REASON_NO_MANAGEMENT_POLICY,)


def _load_observation(conn: Any, observation_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT observation_id, managed_trade_id, observation_seq, tm_version_id,
                             effective_at, data_status
                      FROM trade_management.trade_observation WHERE observation_id=%s""",
                    (observation_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("observation_id", "managed_trade_id", "observation_seq", "tm_version_id",
            "effective_at", "data_status")
    return dict(zip(keys, row))


def _load_trade_state(conn: Any, managed_trade_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM trade_management.managed_trade WHERE managed_trade_id=%s",
                    (managed_trade_id,))
        row = cur.fetchone()
    return row[0] if row else None


def record_decision(conn: Any, *, observation_id: str, event_id: str, now_utc: datetime,
                    consumer_name: str = DECISION_CONSUMER_NAME,
                    evaluator: TmNoneEvaluator = TmNoneEvaluator()) -> DecisionResult:
    with transaction(conn):
        if not claim_inbox(conn, consumer_name, event_id):
            return DecisionResult(status="INBOX_DUPLICATE", decision_id=None, action=None)

        observation = _load_observation(conn, observation_id)
        if observation is None:
            mark_inbox_processed(conn, consumer_name, event_id)
            return DecisionResult(status="OBSERVATION_MISSING", decision_id=None, action=None)

        decision_id = _decision_id(managed_trade_id=observation["managed_trade_id"],
                                   observation_id=observation_id, tm_version_id=observation["tm_version_id"])

        with conn.cursor() as cur:
            cur.execute("SELECT action FROM trade_management.trade_manager_decision WHERE decision_id=%s",
                        (decision_id,))
            existing = cur.fetchone()
        if existing is not None:
            mark_inbox_processed(conn, consumer_name, event_id)
            return DecisionResult(status="DUPLICATE", decision_id=decision_id, action=existing[0])

        trade_state = _load_trade_state(conn, observation["managed_trade_id"])
        action, reason_codes = evaluator.evaluate(managed_trade_state=trade_state or "OPEN")

        persisted_at = now_utc.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO trade_management.trade_manager_decision
                (decision_id, managed_trade_id, tm_version_id, observation_id, observation_seq,
                 action, parameters, reason_codes, decision_trace_ref, decision_time,
                 persisted_at, data_status, record_mode)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT (decision_id) DO NOTHING""",
                       (decision_id, observation["managed_trade_id"], observation["tm_version_id"],
                        observation_id, observation["observation_seq"], action, "{}", list(reason_codes),
                        "{}", observation["effective_at"], persisted_at, observation["data_status"], "SHADOW"))

            # Publication gate boundary (A6 12): HOLD is never actionable, so this is always
            # WITHHELD for TM-NONE. The row exists so the gate is exercised end to end without
            # any customer distribution being implemented (mission section 7).
            outcome, reason = evaluate_publication_gate(GateInputs(action=action))
            cur.execute("""INSERT INTO trade_management.publication_decision(decision_id, outcome, reason)
                          VALUES (%s,%s,%s) ON CONFLICT (decision_id) DO NOTHING""",
                       (decision_id, outcome, reason))

        payload = {"schema": "trade-manager-decision.v1", "decision_id": decision_id,
                  "managed_trade_id": observation["managed_trade_id"], "observation_id": observation_id,
                  "observation_seq": observation["observation_seq"], "tm_version_id": observation["tm_version_id"],
                  "action": action, "parameters": {}, "reason_codes": list(reason_codes),
                  "decision_time": str(observation["effective_at"]), "data_status": observation["data_status"]}
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.outbox_events
                (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                 schema_version, payload, occurred_at, correlation_id, causation_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING""",
                       (decision_id, "trade.decision.made.v1", "managed_trade",
                        observation["managed_trade_id"], observation["observation_seq"],
                        "event-envelope.v1", canonical_bytes(payload).decode("utf-8"),
                        persisted_at, observation["managed_trade_id"], observation_id))

        mark_inbox_processed(conn, consumer_name, event_id)
        return DecisionResult(status="RECORDED", decision_id=decision_id, action=action,
                              reason_codes=reason_codes)
