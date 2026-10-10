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
from typing import Any, Callable

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction
from postgres.foundation import claim_inbox, mark_inbox_processed

from .ids import decision_id as _decision_id
from .mode import OFF, SKIP_REASON_OFF, current_mode
from .publication_gate import GateInputs, evaluate_publication_gate
from .tm_breakeven_trail import EVALUATOR_ID as BREAKEVEN_TRAIL_EVALUATOR_ID
from .tm_breakeven_trail import BreakevenTrailPolicy, TmBreakevenTrailEvaluator
from .tm_none import DECISION_CONSUMER_NAME, TmNoneEvaluator
from .tm_time_exit import EVALUATOR_ID as TIME_EXIT_EVALUATOR_ID
from .tm_time_exit import evaluate_time_exit
from .tm_profit_exit import EVALUATOR_ID as EXIT_POLICY_EVALUATOR_ID
from .tm_profit_exit import ExitPolicy, evaluate_exit_policy
from .tm_structure import EVALUATOR_ID as STRUCTURE_EVALUATOR_ID
from .tm_structure import StructurePolicy, TmStructureEvaluator

# (instrument, timeframe) -> completed bar rows (oldest first); injected by the runtime.
BarsProvider = Callable[[str, str], "list[dict[str, Any]]"]

__all__ = ["DECISION_CONSUMER_NAME", "DecisionResult", "record_decision"]


@dataclass(frozen=True)
class DecisionResult:
    status: str  # RECORDED | DUPLICATE | INBOX_DUPLICATE | OBSERVATION_MISSING | TRADE_MANAGER_OFF | TRADE_NOT_OPEN
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
        cur.execute("""SELECT mt.direction, mt.reference_entry_price, mt.initial_stop, mt.risk_distance, mt.state,
                             mt.instrument, mt.initial_target, mt.decision_time, mt.time_exit_at,
                             mt.net_profit_target_usd, mt.profit_target_pips, mt.profit_target_r, mt.pip_size,
                             (publication.publish_status = 'PUBLISHED')
                      FROM trade_management.managed_trade mt
                      JOIN strategy.entry_signals s ON s.signal_id = mt.entry_signal_id
                      LEFT JOIN LATERAL (
                          SELECT o.publish_status
                            FROM platform.outbox_events o
                           WHERE o.aggregate_type = 'signal'
                             AND o.aggregate_id = s.signal_id
                             AND o.event_type = 'signal.entry.created.v1'
                           ORDER BY o.created_at DESC, o.event_id
                           LIMIT 1
                      ) AS publication ON TRUE
                     WHERE mt.managed_trade_id=%s""",
                    (managed_trade_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("direction", "reference_entry_price", "initial_stop", "risk_distance", "state",
            "instrument", "initial_target", "decision_time", "time_exit_at", "net_profit_target_usd",
            "profit_target_pips", "profit_target_r", "pip_size", "entry_signal_published")
    return dict(zip(keys, row))


def _load_market_snapshot(conn: Any, market_snapshot_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT bid, ask, source_timestamp FROM trade_management.market_snapshot WHERE market_snapshot_id=%s",
                    (market_snapshot_id,))
        row = cur.fetchone()
    return {"bid": row[0], "ask": row[1], "source_timestamp": row[2]} if row else None


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


def _effective_levels(conn: Any, trade: dict[str, Any], managed_trade_id: str) -> tuple[float, float | None]:
    """The stop/target in force: the most recent decision that set each one (a later HOLD or a
    target-only move never resets the stop), else the trade's initial stop/target."""
    stop, target = float(trade["initial_stop"]), (float(trade["initial_target"]) if trade.get("initial_target") is not None else None)
    with conn.cursor() as cur:
        cur.execute("""SELECT parameters FROM trade_management.trade_manager_decision
                      WHERE managed_trade_id=%s AND action IN ('MOVE_TO_BREAKEVEN','TRAIL_STOP','MOVE_STOP','MOVE_TARGET')
                      ORDER BY observation_seq DESC""", (managed_trade_id,))
        rows = cur.fetchall()
    found_stop = found_target = False
    for (parameters,) in rows:
        if isinstance(parameters, str):
            import json
            parameters = json.loads(parameters)
        parameters = parameters or {}
        if not found_stop and parameters.get("new_stop") is not None:
            stop, found_stop = float(parameters["new_stop"]), True
        if not found_target and parameters.get("new_target") is not None:
            target, found_target = float(parameters["new_target"]), True
        if found_stop and found_target:
            break
    return stop, target


def _mark_price(direction: str, snapshot: dict[str, Any]) -> float:
    # Matches TM-NONE-1's own price_semantics exactly: bid for LONG, ask for SHORT.
    return float(snapshot["bid"]) if direction == "LONG" else float(snapshot["ask"])


def _policy_from_manifest(manifest: dict[str, Any]) -> BreakevenTrailPolicy:
    bundle = dict(manifest.get("policy_bundle") or [])
    return BreakevenTrailPolicy(breakeven_trigger_r=float(bundle["breakeven_trigger_r"]),
                                trail_trigger_r=float(bundle["trail_trigger_r"]),
                                trail_distance_r=float(bundle["trail_distance_r"]))


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _evaluate(*, evaluator_id: str, manifest: dict[str, Any], trade: dict[str, Any],
             snapshot: dict[str, Any], current_stop: float, current_target: float | None = None,
             as_of: Any = None, bars_provider: BarsProvider | None = None) -> tuple[str, tuple[str, ...], dict]:
    if evaluator_id == STRUCTURE_EVALUATOR_ID:
        bundle = dict(manifest.get("policy_bundle") or [])
        bars = None
        if bars_provider is not None:
            try:
                bars = {tf: bars_provider(trade["instrument"], tf) for tf in ("M5", "M15", "H1")}
            except Exception:  # noqa: BLE001 - no bars: the evaluator degrades to R-only breakeven/HOLD
                bars = None
        return TmStructureEvaluator().evaluate(
            direction=trade["direction"], entry=float(trade["reference_entry_price"]),
            risk_distance=float(trade["risk_distance"]) if trade["risk_distance"] else None,
            current_stop=current_stop, current_target=current_target,
            bid=float(snapshot["bid"]), ask=float(snapshot["ask"]), trade_state=trade["state"] or "OPEN",
            entry_time=_epoch(trade["decision_time"]), as_of=_epoch(as_of), bars=bars,
            policy=StructurePolicy(**{k: float(v) for k, v in bundle.items()}))
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
    if evaluator_id == TIME_EXIT_EVALUATOR_ID:
        return evaluate_time_exit(trade_state=trade["state"] or "OPEN",
                                  time_exit_at=trade.get("time_exit_at"), as_of=as_of)
    if evaluator_id == EXIT_POLICY_EVALUATOR_ID:
        source_timestamp = snapshot.get("source_timestamp")
        quote_age = None
        if source_timestamp is not None and as_of is not None:
            try:
                quote_age = max(0.0, (_epoch(as_of) - _epoch(source_timestamp)))
            except (TypeError, ValueError):
                quote_age = None
        return evaluate_exit_policy(
            trade_state=trade["state"] or "OPEN", direction=trade["direction"],
            entry_price=float(trade["reference_entry_price"]), bid=snapshot.get("bid"),
            ask=snapshot.get("ask"), risk_distance=trade.get("risk_distance"),
            pip_size=trade.get("pip_size"), as_of=as_of,
            policy=ExitPolicy(time_exit_at=trade.get("time_exit_at"),
                              net_profit_target_usd=trade.get("net_profit_target_usd"),
                              profit_target_pips=trade.get("profit_target_pips"),
                              profit_target_r=trade.get("profit_target_r")),
            quote_age_seconds=quote_age)
    raise UnknownEvaluator(f"no dispatch entry for evaluator_id={evaluator_id!r}")


def record_decision(conn: Any, *, observation_id: str, event_id: str, now_utc: datetime,
                    consumer_name: str = DECISION_CONSUMER_NAME,
                    bars_provider: BarsProvider | None = None) -> DecisionResult:
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

        # A terminal (CLOSED) trade gets no further decisions, including for observations that
        # were recorded or queued before it closed.
        if (trade["state"] or "OPEN") != "OPEN":
            mark_inbox_processed(conn, consumer_name, event_id)
            return DecisionResult(status="TRADE_NOT_OPEN", decision_id=None, action=None)

        snapshot = _load_market_snapshot(conn, observation["market_snapshot_id"]) or {}
        current_stop, current_target = _effective_levels(conn, trade, observation["managed_trade_id"])

        action, reason_codes, parameters = _evaluate(
            evaluator_id=version["evaluator_id"], manifest=version["manifest"], trade=trade,
            snapshot=snapshot, current_stop=current_stop, current_target=current_target,
            as_of=observation["effective_at"], bars_provider=bars_provider)

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

            # Only the explicitly publishable Context time-exit version may reach the existing
            # execution-v2 management consumer. All legacy versions retain the old shadow gate.
            publishable = version["evaluator_id"] in (TIME_EXIT_EVALUATOR_ID, EXIT_POLICY_EVALUATOR_ID) and bool(trade.get("entry_signal_published"))
            outcome, reason = evaluate_publication_gate(
                GateInputs(action=action, entry_signal_published=publishable,
                           tm_version_publication_eligibility="PUBLISHABLE" if publishable else "SHADOW_ONLY"))
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
