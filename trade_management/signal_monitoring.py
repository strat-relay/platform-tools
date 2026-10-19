"""Theoretical signal state, independent of execution intents or broker positions."""
import json
from datetime import datetime, timezone

from outcome_resolver import EvaluationContract


def monitoring_state(row, *, now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    metadata = row.get("strategy_metadata") or {}
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    evidence = row.get("resolution_evidence") or {}
    evidence = json.loads(evidence) if isinstance(evidence, str) else evidence
    status = row.get("outcome_status")
    if status in {"EXPIRED", "INVALIDATED"} or evidence.get("event") == "EXPIRED_UNFILLED":
        return "EXPIRED" if status != "INVALIDATED" else "INVALIDATED"
    if status not in (None, "OPEN"):
        return "CLOSED"
    if row.get("resolution_state") == "AMBIGUOUS_INTRABAR":
        return "DATA_UNAVAILABLE"
    contract = EvaluationContract.from_signal({"strategy_id": row.get("strategy_id"),
                                               "entry_type": row.get("entry_type"),
                                               "strategy_metadata": metadata})
    if contract.activation == "ENTRY_PRICE_TOUCH":
        activated_at = evidence.get("activated_at")
        if not activated_at:
            return "PENDING_ENTRY"
        activated_at = datetime.fromisoformat(str(activated_at).replace("Z", "+00:00"))
        if activated_at.tzinfo is None or activated_at > now_utc:
            return "DATA_UNAVAILABLE"
    return "ACTIVE_THEORETICAL"


def load_signal_state(conn, managed_trade_id):
    with conn.cursor() as cur:
        cur.execute("""SELECT s.strategy_id, s.entry_type, s.strategy_metadata,
                              o.status, o.resolution_evidence, o.resolution_state
                       FROM trade_management.managed_trade mt
                       JOIN strategy.entry_signals s ON s.signal_id=mt.entry_signal_id
                       LEFT JOIN strategy.entry_signal_outcomes o ON o.signal_id=s.signal_id
                       WHERE mt.managed_trade_id=%s""", (managed_trade_id,))
        row = cur.fetchone()
    if row is None:
        raise ValueError("canonical signal missing for managed trade")
    return dict(zip(("strategy_id", "entry_type", "strategy_metadata", "outcome_status",
                     "resolution_evidence", "resolution_state"), row))
