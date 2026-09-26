"""Dispatches every observation to whichever evaluator its trade is actually bound to, then
persists the result through the exact same transaction shape tm_none.py's own `record_decision`
already uses (inbox claim -> load -> evaluate -> decision INSERT -> publication gate -> outbox
event, all in one transaction).

Why this module exists rather than extending tm_none.py: tm_none.py's own module source bytes
are part of TM-NONE-1's frozen `code_manifest` (versions.py's own comment: "the version id
commits to the code that will evaluate it"). Editing tm_none.py at all - even just to add
dispatch - would silently change what TM-NONE-1's already-registered manifest claims to be
running. tm_none.py is therefore never imported for its `record_decision` here, only for its
`TmNoneEvaluator` (a pure, already-frozen, already-tested class this module treats as a black
box) and `DECISION_CONSUMER_NAME` (the shared durable-consumer identity - dispatch happens
*inside* the one existing shadow consumer, never as a second parallel consumer racing it for the
same observations).

Every evaluator's own decision logic stays exactly as deterministic/pure as TM-NONE's (A6 11
section 5) - this module's only job is loading the right rows and calling the right one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction
from postgres.foundation import claim_inbox, mark_inbox_processed

from .ids import decision_id as _decision_id
from .mode import OFF, SKIP_REASON_OFF, current_mode
from .publication_gate import GateInputs, evaluate_publication_gate
from .tm_breakeven_trail import EVALUATOR_ID as BREAKEVEN_TRAIL_EVALUATOR_ID
from .tm_breakeven_trail import BreakevenTrailPolicy, TmBreakevenTrailEvaluator
from .tm_none import DECISION_CONSUMER_NAME, TmNoneEvaluator

__all__ = ["DECISION_CONSUMER_NAME", "DecisionResult", "record_decision"]


@dataclass(frozen=True)
class DecisionResult:
    status: str  # RECORDED | DUPLICATE | INBOX_DUPLICATE | OBSERVATION_MISSING | TRADE_MANAGER_OFF
    decision_id: str | None
    action: str | None
    reason_codes: tuple[str, ...] = ()


class UnknownEvaluator(RuntimeError):
    """A trade is bound to a tm_version_id whose evaluator_id this engine has no dispatch
    entry for. Fail closed - never silently fall back to TM-NONE or guess."""


def _load_observation(conn: Any, observation_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT observation_id, managed_trade_id, observation_seq, tm_version_id,
                             market_snapshot_id, effective_at, data_status
                      FROM trade_management.trade_observation WHERE observation_id=%s""",
                    (observation_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("observation_id", "managed_trade_id", "observation_seq", "tm_version_id",
            "market_snapshot_id", "effective_at", "data_status")
    return dict(zip(keys, row))


def _load_trade(conn: Any, managed_trade_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT direction, reference_entry_price, initial_stop, risk_distance, state
                      FROM trade_management.managed_trade WHERE managed_trade_id=%s""",
                    (managed_trade_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("direction", "reference_entry_price", "initial_stop", "risk_distance", "state")
    return dict(zip(keys, row))


def _load_market_snapshot(conn: Any, market_snapshot_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT bid, ask FROM trade_management.market_snapshot WHERE market_snapshot_id=%s",
                    (market_snapshot_id,))
        row = cur.fetchone()
    return {"bid": row[0], "ask": row[1]} if row else None


def _load_version(conn: Any, tm_version_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT evaluator_id, manifest FROM trade_management.trade_manager_version WHERE tm_version_id=%s",
                    (tm_version_id,))
        row = cur.fetchone()
    if row is None:
        return None
    manifest = row[1]
    if isinstance(manifest, str):
        # Defensive, matching execution_v2/risk_policy_store.py's own identical guard for a jsonb
        # column that may come back pre-parsed or as raw text depending on the driver/fake.
        import json
        manifest = json.loads(manifest)
    return {"evaluator_id": row[0], "manifest": manifest}


def _load_latest_decision(conn: Any, managed_trade_id: str) -> dict[str, Any] | None:
    """Most recent decision for this trade across ALL its observations - used only to derive the
    currently-effective stop (initial_stop, or the last MOVE_TO_BREAKEVEN/TRAIL_STOP's new_stop).
    A trade has exactly one bound tm_version_id for its whole life, so "latest decision" and
    "latest decision under this trade's own evaluator" are the same query."""
    with conn.cursor() as cur:
        cur.execute("""SELECT action, parameters FROM trade_management.trade_manager_decision
                      WHERE managed_trade_id=%s ORDER BY observation_seq DESC LIMIT 1""",
                    (managed_trade_id,))
        row = cur.fetchone()
    if row is None:
        return None
    parameters = row[1]
    if isinstance(parameters, str):
        import json
        parameters = json.loads(parameters)
    return {"action": row[0], "parameters": parameters}


def _current_stop(trade: dict[str, Any], latest_decision: dict[str, Any] | None) -> float:
    if latest_decision and latest_decision["action"] in ("MOVE_TO_BREAKEVEN", "TRAIL_STOP"):
        new_stop = (latest_decision["parameters"] or {}).get("new_stop")
        if new_stop is not None:
            return float(new_stop)
    return float(trade["initial_stop"])


def _mark_price(direction: str, snapshot: dict[str, Any]) -> float:
    # Matches TM-NONE-1's own price_semantics exactly: bid for LONG, ask for SHORT.
    return float(snapshot["bid"]) if direction == "LONG" else float(snapshot["ask"])


def _policy_from_manifest(manifest: dict[str, Any]) -> BreakevenTrailPolicy:
    bundle = dict(manifest.get("policy_bundle") or [])
    return BreakevenTrailPolicy(breakeven_trigger_r=float(bundle["breakeven_trigger_r"]),
                                trail_trigger_r=float(bundle["trail_trigger_r"]),
                                trail_distance_r=float(bundle["trail_distance_r"]))


def _evaluate(*, evaluator_id: str, manifest: dict[str, Any], trade: dict[str, Any],
             snapshot: dict[str, Any], current_stop: float) -> tuple[str, tuple[str, ...], dict]:
    if evaluator_id == TmNoneEvaluator.evaluator_id:
        action, reasons = TmNoneEvaluator().evaluate(managed_trade_state=trade["state"] or "OPEN")
        return action, reasons, {}
    if evaluator_id == BREAKEVEN_TRAIL_EVALUATOR_ID:
        policy = _policy_from_manifest(manifest)
        return TmBreakevenTrailEvaluator().evaluate(
            direction=trade["direction"], entry=float(trade["reference_entry_price"]),
            initial_stop=float(trade["initial_stop"]), risk_distance=trade["risk_distance"],
            current_stop=current_stop, mark_price=_mark_price(trade["direction"], snapshot),
            trade_state=trade["state"] or "OPEN", policy=policy)
    raise UnknownEvaluator(f"no dispatch entry for evaluator_id={evaluator_id!r}")


def record_decision(conn: Any, *, observation_id: str, event_id: str, now_utc: datetime,
                    consumer_name: str = DECISION_CONSUMER_NAME) -> DecisionResult:
    with transaction(conn):
        if not claim_inbox(conn, consumer_name, event_id):
            return DecisionResult(status="INBOX_DUPLICATE", decision_id=None, action=None)

        if current_mode(conn) == OFF:
            mark_inbox_processed(conn, consumer_name, event_id)
            return DecisionResult(status=SKIP_REASON_OFF, decision_id=None, action=None)

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

        trade = _load_trade(conn, observation["managed_trade_id"])
        version = _load_version(conn, observation["tm_version_id"])
        if trade is None or version is None:
            # Fails the transaction (rolled back by the `transaction()` context manager) - the
            # next redelivery retries once the missing row exists. Never records a decision for
            # a trade/version this engine could not actually resolve.
            raise UnknownEvaluator(
                f"cannot evaluate observation {observation_id!r}: "
                f"trade={'missing' if trade is None else 'ok'} version={'missing' if version is None else 'ok'}")

        snapshot = _load_market_snapshot(conn, observation["market_snapshot_id"]) or {}
        latest_decision = _load_latest_decision(conn, observation["managed_trade_id"])
        current_stop = _current_stop(trade, latest_decision)

        action, reason_codes, parameters = _evaluate(
            evaluator_id=version["evaluator_id"], manifest=version["manifest"], trade=trade,
            snapshot=snapshot, current_stop=current_stop)

        persisted_at = now_utc.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO trade_management.trade_manager_decision
                (decision_id, managed_trade_id, tm_version_id, observation_id, observation_seq,
                 action, parameters, reason_codes, decision_trace_ref, decision_time,
                 persisted_at, data_status, record_mode)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s,%s,%s)
                ON CONFLICT (decision_id) DO NOTHING""",
                       (decision_id, observation["managed_trade_id"], observation["tm_version_id"],
                        observation_id, observation["observation_seq"], action,
                        canonical_bytes(parameters).decode("utf-8"), list(reason_codes), "{}",
                        observation["effective_at"], persisted_at, observation["data_status"], "SHADOW"))

            # Same gate call TM-NONE itself makes, same defaults (entry_signal_published=False,
            # tm_version_publication_eligibility="SHADOW_ONLY") - a non-HOLD action from the new
            # evaluator still comes out WITHHELD(TM_VERSION_NOT_PUBLISHABLE) unless a later,
            # separately-authorized change deliberately marks a version PUBLISHABLE. Nothing in
            # this module ever does that.
            outcome, reason = evaluate_publication_gate(GateInputs(action=action))
            cur.execute("""INSERT INTO trade_management.publication_decision(decision_id, outcome, reason)
                          VALUES (%s,%s,%s) ON CONFLICT (decision_id) DO NOTHING""",
                       (decision_id, outcome, reason))

        payload = {"schema": "trade-manager-decision.v1", "decision_id": decision_id,
                  "managed_trade_id": observation["managed_trade_id"], "observation_id": observation_id,
                  "observation_seq": observation["observation_seq"], "tm_version_id": observation["tm_version_id"],
                  "action": action, "parameters": parameters, "reason_codes": list(reason_codes),
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
                              reason_codes=tuple(reason_codes))
