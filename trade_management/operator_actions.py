"""Audited, idempotent operator recommendations; broker writes stay in execution-v2."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from postgres.db import transaction
from .decision_engine import _effective_levels, _load_market_snapshot, _load_trade
from .mode import current_mode
from .signal_monitoring import load_signal_state, monitoring_state


class ActionConflict(ValueError):
    pass


def validate_request(request: Any) -> dict:
    if not isinstance(request, dict):
        raise ValueError("An action object is required")
    for key in ("requestId", "managedTradeId", "reason"):
        if not isinstance(request.get(key), str) or not request[key].strip() or len(request[key]) > 500:
            raise ValueError(f"{key} is required (maximum 500 characters)")
    if request.get("action") not in ("MOVE_TO_BREAKEVEN", "EXIT"):
        raise ValueError("action must be MOVE_TO_BREAKEVEN or EXIT")
    seq = request.get("expectedObservationSeq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError("expectedObservationSeq must be a nonnegative integer")
    if not isinstance(request.get("allowLiveExecution", False), bool):
        raise ValueError("allowLiveExecution must be boolean")
    return {key: request.get(key, default) for key, default in (
        ("requestId", None), ("managedTradeId", None), ("reason", None), ("action", None),
        ("expectedObservationSeq", None), ("allowLiveExecution", False))}


def breakeven_price(trade: dict, snapshot: dict | None, effective_stop: float, now: datetime) -> float:
    if not snapshot:
        raise ActionConflict("A fresh market quote is required for Move to BE")
    timestamp = snapshot["source_timestamp"]
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or not 0 <= (now - timestamp).total_seconds() <= 120:
        raise ActionConflict("Market quote is stale; refresh before Move to BE")
    entry = float(trade["reference_entry_price"])
    bid, ask = float(snapshot["bid"]), float(snapshot["ask"])
    if not all(math.isfinite(x) and x > 0 for x in (entry, bid, ask, effective_stop)) or bid > ask:
        raise ActionConflict("Invalid price geometry")
    long = trade["direction"] == "LONG"
    if (long and (bid <= entry or effective_stop >= entry)) or (not long and (ask >= entry or effective_stop <= entry)):
        raise ActionConflict("Breakeven must improve the current stop and remain behind the market")
    return entry


def request_action(conn: Any, submitted: dict, *, now: datetime | None = None) -> dict:
    request = validate_request(submitted)
    now = now or datetime.now(timezone.utc)
    digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    decision_id = "TMOP_" + hashlib.sha256(request["requestId"].encode()).hexdigest()[:24]
    trade_id = request["managedTradeId"]
    with transaction(conn):
        with conn.cursor() as cur:
            # Lock the aggregate before replay/concurrency checks and sequence assignment.
            cur.execute("SELECT tm_version_id,last_observation_seq,entry_signal_id FROM trade_management.managed_trade WHERE managed_trade_id=%s FOR UPDATE", (trade_id,))
            row = cur.fetchone()
            if not row:
                raise ActionConflict("Managed trade is unavailable")
            version, seq, signal_id = row
            cur.execute("SELECT decision_trace_ref FROM trade_management.trade_manager_decision WHERE decision_id=%s", (decision_id,))
            existing = cur.fetchone()
            if existing:
                trace = existing[0]
                trace = json.loads(trace) if isinstance(trace, str) else trace
                if trace.get("request_hash") != digest:
                    raise ActionConflict("requestId was already used for a different request")
                return {"decision_id": decision_id, "status": "RECORDED", "replayed": True, "broker_confirmed": False}
            if seq < request["expectedObservationSeq"]:
                raise ActionConflict("Trade changed; refresh and review the action again")
            if seq > request["expectedObservationSeq"]:
                cur.execute("""SELECT decision_id FROM trade_management.trade_manager_decision
                    WHERE managed_trade_id=%s AND observation_seq>%s AND action<>'HOLD' LIMIT 1""",
                    (trade_id, request["expectedObservationSeq"]))
                if cur.fetchone():
                    raise ActionConflict("Management changed; refresh and review the action again")
            mode = current_mode(conn)
            if mode == "OFF" or (request["allowLiveExecution"] and mode != "LIVE"):
                raise ActionConflict("Trade Manager mode does not permit this action scope")
            trade = _load_trade(conn, trade_id)
            if trade["state"] != "OPEN" or monitoring_state(load_signal_state(conn, trade_id), now_utc=now) != "ACTIVE_THEORETICAL":
                raise ActionConflict("Only activated, open theoretical signals can be managed")
            cur.execute("SELECT decision_id FROM trade_management.trade_manager_decision WHERE managed_trade_id=%s AND action='EXIT' LIMIT 1", (trade_id,))
            if cur.fetchone():
                raise ActionConflict("An exit recommendation is already recorded")
            params = {"operator_action": True, "execution_scope": "LINKED_LIVE" if request["allowLiveExecution"] else "THEORETICAL_ONLY"}
            snapshot_id = None
            if request["action"] == "MOVE_TO_BREAKEVEN":
                if trade.get("time_exit_at") is not None and trade["time_exit_at"] <= now:
                    raise ActionConflict("The theoretical deadline elapsed; review Exit instead")
                cur.execute("SELECT market_snapshot_id FROM trade_management.trade_observation WHERE managed_trade_id=%s AND market_snapshot_id IS NOT NULL ORDER BY observation_seq DESC LIMIT 1", (trade_id,))
                observed = cur.fetchone()
                snapshot_id = observed[0] if observed else None
                stop, _ = _effective_levels(conn, trade, trade_id)
                params["new_stop"] = breakeven_price(trade, _load_market_snapshot(conn, snapshot_id) if snapshot_id else None, stop, now)
            else:
                params["exit_reason"] = "OPERATOR_EXIT"
            seq += 1
            observation_id = decision_id + "_OBS"
            cur.execute("""INSERT INTO trade_management.trade_observation
                (observation_id,managed_trade_id,observation_seq,market_snapshot_id,tm_version_id,observed_at,effective_at,bars_ref,data_status,payload_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'FORWARD',%s)""",
                (observation_id,trade_id,seq,snapshot_id,version,now,now,json.dumps({"kind":"OPERATOR_ACTION"}),digest))
            cur.execute("UPDATE trade_management.managed_trade SET last_observation_seq=%s WHERE managed_trade_id=%s", (seq,trade_id))
            trace = {"source":"OPERATOR", "submitted_by":"console", "reason":request["reason"], "request_id":request["requestId"], "request_hash":digest}
            cur.execute("""INSERT INTO trade_management.trade_manager_decision
                (decision_id,managed_trade_id,tm_version_id,observation_id,observation_seq,action,parameters,reason_codes,decision_trace_ref,decision_time,persisted_at,data_status,record_mode)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,'FORWARD','SHADOW')""",
                (decision_id,trade_id,version,observation_id,seq,request["action"],json.dumps(params),["OPERATOR_REQUEST"],json.dumps(trace),now,now))
            published = bool(trade.get("entry_signal_published"))
            cur.execute("INSERT INTO trade_management.publication_decision(decision_id,outcome,reason) VALUES (%s,%s,%s)",
                        (decision_id,"PUBLISHED" if published else "WITHHELD","OPERATOR_REQUEST" if published else "ENTRY_NOT_PUBLISHED"))
            payload = {"schema":"trade-manager-decision.v1", "decision_id":decision_id,"managed_trade_id":trade_id,
                       "observation_id":observation_id,"observation_seq":seq,"tm_version_id":version,
                       "action":request["action"],"parameters":params,"reason_codes":["OPERATOR_REQUEST"],
                       "decision_time":now.isoformat(),"data_status":"FORWARD"}
            # Never send unpublished entries to execution. Subscriber publication is durable,
            # but delivery remains the responsibility of the notification consumer.
            events = []
            if published:
                events.append((decision_id,"trade.decision.made.v1",payload))
                events.append((decision_id+"_SIGNAL","signal.management.published.v1",{**payload,
                    "schema":"management-signal.v1","signal_id":signal_id,"entry_signal_id":signal_id,
                    "management_signal_id":decision_id+"_SIGNAL","outcome_kind":"STRATEGY_THEORETICAL",
                    "exit_reason":params.get("exit_reason")}))
            for event_id, event_type, event_payload in events:
                cur.execute("""INSERT INTO platform.outbox_events
                    (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,occurred_at,correlation_id,causation_id)
                    VALUES (%s,%s,'managed_trade',%s,%s,'event-envelope.v1',%s::jsonb,%s,%s,%s)""",
                    (event_id,event_type,trade_id,seq,json.dumps(event_payload),now,signal_id,observation_id))
    return {"decision_id":decision_id,"status":"RECORDED","replayed":False,"broker_confirmed":False,
            "publication_outcome":"PUBLISHED" if published else "WITHHELD"}
