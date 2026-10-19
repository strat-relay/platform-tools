"""Quote-free theoretical deadlines; broker identity/authority never participate."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from outcome_resolver import EvaluationContract
from postgres.db import transaction
from .mode import OFF, current_mode


def _utc(value: Any) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def theoretical_deadline(*, signal_time: Any, strategy_id: str, entry_type: str | None,
                         metadata: Any, captured_minutes: int | None,
                         outcome_status: str | None = None, outcome_exit: Any = None, activation_evidence: Any = None) -> datetime | None:
    """Match the canonical resolver's signal-time hold clock.

    Pending entries need canonical activation or TIME_EXIT evidence: an elapsed clock
    alone does not prove a limit/retrace entry activated. Market entries activate at signal time.
    No broker fill or notification/publication timestamp is an entry-time substitute.
    """
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata or {}
    contract = EvaluationContract.from_signal({"strategy_id": strategy_id,
                                               "entry_type": entry_type, "strategy_metadata": metadata})
    minutes = captured_minutes if captured_minutes is not None else contract.max_hold_minutes
    if minutes is None or minutes <= 0:
        return None
    deadline = _utc(signal_time) + timedelta(minutes=minutes)
    if contract.activation == "ENTRY_PRICE_TOUCH":
        evidence = json.loads(activation_evidence) if isinstance(activation_evidence, str) else activation_evidence or {}
        return deadline if (evidence.get("activated_at") or (outcome_status == "TIME_EXIT" and outcome_exit is not None)) else None
    return deadline


def publish_time_exit(conn: Any, *, managed_trade_id: str, decision_payload: dict, occurred_at: str) -> None:
    """One notification event per managed trade/deadline, even across repeated observations."""
    deadline = decision_payload["parameters"]["time_exit_at"]
    event_id = "TMEXIT_" + hashlib.sha256(f"{managed_trade_id}|{deadline}".encode()).hexdigest()[:24]
    with conn.cursor() as cur:
        cur.execute("SELECT entry_signal_id FROM trade_management.managed_trade WHERE managed_trade_id=%s",
                    (managed_trade_id,))
        signal_id = cur.fetchone()[0]
        payload = {**decision_payload, "schema": "management-signal.v1", "signal_id": signal_id,
                   "entry_signal_id": signal_id, "management_signal_id": event_id,
                   "outcome_kind": "STRATEGY_THEORETICAL", "exit_reason": "TIME_EXIT"}
        cur.execute("""INSERT INTO platform.outbox_events
            (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,
             occurred_at,correlation_id,causation_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING""",
                    (event_id, "signal.management.published.v1", "managed_trade", managed_trade_id,
                     decision_payload["observation_seq"], "event-envelope.v1", json.dumps(payload),
                     occurred_at, signal_id, decision_payload["decision_id"]))


def process_deadlines(conn: Any, *, now_utc: datetime) -> int:
    """Before quote fetching/lifecycle reconciliation, persist due clock observations.

    A deterministic observation id plus the normal decision inbox makes restart,
    redelivery and a crash between observation and decision safe. No synthetic prices.
    """
    from .decision_engine import record_decision
    if current_mode(conn) == OFF:
        return 0
    with conn.cursor() as cur:
        cur.execute("""SELECT mt.managed_trade_id, mt.decision_time, mt.strategy_id, s.entry_type,
                              s.strategy_metadata, mt.time_exit_minutes, o.status, o.exit_timestamp, o.resolution_evidence
                       FROM trade_management.managed_trade mt
                       JOIN strategy.entry_signals s ON s.signal_id=mt.entry_signal_id
                       LEFT JOIN strategy.entry_signal_outcomes o ON o.signal_id=mt.entry_signal_id
                       WHERE mt.state='OPEN'
                         AND (o.status IS NULL OR o.status IN ('OPEN','TIME_EXIT'))
                       ORDER BY mt.managed_trade_id""")
        candidates = cur.fetchall()
    processed = 0
    for trade_id, signal_time, strategy_id, entry_type, metadata, minutes, status, exit_at, evidence in candidates:
        deadline = theoretical_deadline(signal_time=signal_time, strategy_id=strategy_id,
                                        entry_type=entry_type, metadata=metadata, captured_minutes=minutes,
                                        outcome_status=status, outcome_exit=exit_at, activation_evidence=evidence)
        if deadline is None:
            continue
        observation_id = "TMCLOCK_" + hashlib.sha256(f"{trade_id}|{deadline.isoformat()}".encode()).hexdigest()[:24]
        with transaction(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT state, tm_version_id, last_observation_seq FROM trade_management.managed_trade WHERE managed_trade_id=%s FOR UPDATE", (trade_id,))
                state, version_id, last_seq = cur.fetchone()
                if state != "OPEN":
                    continue
                cur.execute("UPDATE trade_management.managed_trade SET time_exit_at=%s WHERE managed_trade_id=%s", (deadline, trade_id))
                if now_utc < deadline:
                    continue
                cur.execute("SELECT observation_seq FROM trade_management.trade_observation WHERE observation_id=%s", (observation_id,))
                if cur.fetchone() is None:
                    seq = last_seq + 1
                    cur.execute("""INSERT INTO trade_management.trade_observation
                        (observation_id,managed_trade_id,observation_seq,market_snapshot_id,tm_version_id,
                         observed_at,effective_at,bars_ref,data_status,payload_hash)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
                                (observation_id, trade_id, seq, None, version_id, now_utc, now_utc,
                                 json.dumps({"kind": "THEORETICAL_DEADLINE", "time_exit_at": deadline.isoformat()}),
                                 "FORWARD", observation_id))
                    cur.execute("UPDATE trade_management.managed_trade SET last_observation_seq=%s WHERE managed_trade_id=%s", (seq, trade_id))
        result = record_decision(conn, observation_id=observation_id, event_id=observation_id, now_utc=now_utc)
        processed += int(result.status == "RECORDED")
    return processed
