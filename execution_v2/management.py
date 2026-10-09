"""Personal management execution: Trade Manager decision -> REDUCE_ONLY broker write (P5).

The Trade Manager never talks to a broker (tests/test_trade_management_isolation.py). It publishes
`trade.decision.made.v1`; this module, in the execution plane, turns an actionable decision into at
most one fenced broker write for the platform position that decision's trade is linked to.

Authorization (the legacy trade_manager/central.authorize() rules, docs/migration/07, now in
PostgreSQL) - every rule fails closed, and anything short of the last step writes nothing:
  1. the decision action is actionable (MOVE_TO_BREAKEVEN / TRAIL_STOP / MOVE_STOP / MOVE_TARGET /
     EXIT); HOLD and anything else is ignored
  2. the Trade Manager mode is LIVE (SHADOW decisions are never acted on)
  3. the managed trade is linked to a platform position: managed_trade.entry_signal_id ->
     execution_intent (this account) -> FILLED execution_result -> broker ticket. Unlinked trades
     (paper signals, manual positions) are never touched
  4. execution authority is ENABLED, now and again right before the write
  5. the decision is fresh (<= MAX_DECISION_AGE_SECONDS, the legacy 120 s rule)
  6. the position is present at the broker right now, in the trade's direction
  7. stop safety: the resulting stop is set, and never looser than the broker's current stop
  8. a request equal to the broker's current SL/TP is NO_CHANGE (no write)
  9. the action key (ticket, action, stop, target) has not been authorized before
Then: shared ownership lease/generation (same resource as entries), a REDUCE_ONLY WriteAuthorization
bound to the exact request fingerprint, one bridge submit. An ambiguous outcome is reconciled by
goal state (docs/runtime_boundaries/06): the position's SL/TP equal the request, or the ticket is
gone after a close.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from postgres.db import transaction

from .bridge_fence_errors import InvalidSignature, RequestFingerprintMismatch, WrongAccount
from .exit_valuation import (ExitValuationError, estimate_initial_monetary_risk,
                             estimate_net_liquidation_profit)

MANAGEMENT_ACTIONS = {"MOVE_TO_BREAKEVEN": "MODIFY", "TRAIL_STOP": "MODIFY", "MOVE_STOP": "MODIFY",
                      "MOVE_TARGET": "MODIFY", "EXIT": "CLOSE"}
BROKER_TOOLS = {"MODIFY": "mt5_position_modify", "CLOSE": "mt5_close_position"}
MAX_DECISION_AGE_SECONDS = 120.0


def management_request_fingerprint(tool: str, args: dict[str, Any]) -> str:
    """Identical to mt5_bridge.fence_boundary.management_request_fingerprint (bridge repository):
    the platform signs it, the bridge recomputes it from the arguments it sends."""
    body: dict[str, Any] = {"tool": tool, "ticket": int(args["ticket"])}
    if tool == "mt5_position_modify":
        body.update(stop_loss=float(args["stop_loss"]), take_profit=float(args["take_profit"]))
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= max(1e-9, abs(float(b)) * 1e-8)


def stop_is_safe(direction: str, broker_stop: float, requested_stop: float) -> bool:
    """legacy trade_manager.central.stop_is_safe, plus: a stop can never be removed."""
    if requested_stop is None or requested_stop <= 0:
        return False
    if not broker_stop:
        return True
    return requested_stop >= broker_stop if direction == "LONG" else requested_stop <= broker_stop


@dataclass(frozen=True)
class ManagementOutcome:
    status: str                      # IGNORED | DUPLICATE | REJECTED | NO_CHANGE | APPLIED | BROKER_REJECTED | FENCED | UNKNOWN_RECONCILIATION_REQUIRED
    management_intent_id: str | None = None
    reason: str | None = None


class ManagementWorker:
    """Uses the entry ExecutionWorker's lease, fence authority, bridge client and authority switch -
    one account resource, one generation, one set of credentials."""

    def __init__(self, execution_worker: Any, *, tm_mode_reader: Callable[[], str] | None = None) -> None:
        self.w = execution_worker
        self.conn = execution_worker.conn
        self.tm_mode_reader = tm_mode_reader or self._read_tm_mode

    # ---- reads -----------------------------------------------------------------------------
    def _read_tm_mode(self) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT mode FROM trade_management.trade_manager_mode WHERE mode_id='current'")
            row = cur.fetchone()
        return row[0] if row else "OFF"

    def _platform_link(self, managed_trade_id: str) -> dict[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT mt.direction, ei.execution_intent_id, r.broker_order_id, r.broker_position_id, r.symbol
                           FROM trade_management.managed_trade mt
                           JOIN execution_v2.execution_intent ei
                             ON ei.entry_signal_id = mt.entry_signal_id AND ei.account_id = %s
                           JOIN execution_v2.execution_result r
                             ON r.execution_intent_id = ei.execution_intent_id AND r.outcome = 'FILLED'
                           WHERE mt.managed_trade_id = %s
                           ORDER BY r.created_at DESC LIMIT 1""", (self.w.account_id, managed_trade_id))
            row = cur.fetchone()
        if row is None:
            return None
        direction, intent_id, order_id, position_id, symbol = row
        ticket = position_id or order_id          # hedging: a position's ticket is its opening order's
        return {"direction": direction, "execution_intent_id": intent_id, "ticket": str(ticket) if ticket else None,
                "symbol": symbol} if ticket else None

    def _existing(self, decision_id: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM execution_v2.management_intent WHERE decision_id=%s", (decision_id,))
            row = cur.fetchone()
        return row[0] if row else None

    def _existing_close(self, managed_trade_id: str) -> str | None:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT status FROM execution_v2.management_intent
                           WHERE managed_trade_id=%s AND broker_action='CLOSE'
                             AND status IN ('AUTHORIZED','SENDING','APPLIED','UNKNOWN_RECONCILIATION_REQUIRED')
                           ORDER BY created_at DESC LIMIT 1""", (managed_trade_id,))
            row = cur.fetchone()
        return row[0] if row else None

    # ---- writes ----------------------------------------------------------------------------
    @staticmethod
    def _value(column: str, value: Any) -> Any:
        return json.dumps(value, sort_keys=True, default=str) if column in {"broker_response", "exit_policy_snapshot", "observed_quote"} else value

    @staticmethod
    def _placeholder(column: str) -> str:
        return "%s::jsonb" if column in {"broker_response", "exit_policy_snapshot", "observed_quote"} else "%s"

    def _record(self, row: dict[str, Any]) -> bool:
        columns = ", ".join(row)
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(f"""INSERT INTO execution_v2.management_intent ({columns})
                                VALUES ({", ".join(self._placeholder(c) for c in row)}) ON CONFLICT DO NOTHING
                                RETURNING management_intent_id""",
                            [self._value(c, v) for c, v in row.items()])
                return cur.fetchone() is not None

    def _update(self, intent_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}={self._placeholder(k)}" for k in fields)
        values = [self._value(k, v) for k, v in fields.items()]
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(f"UPDATE execution_v2.management_intent SET {sets} WHERE management_intent_id=%s",
                            values + [intent_id])

    def _reject(self, base: dict[str, Any], reason: str, **extra: Any) -> ManagementOutcome:
        self._record({**base, **extra, "status": "REJECTED", "reason": reason, "completed_at": datetime.now(timezone.utc)})
        return ManagementOutcome("REJECTED", base["management_intent_id"], reason)

    def _read_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        reader = getattr(self.w.bridge, "_read_tool", None)
        if reader is None:
            raise ExitValuationError("BROKER_READ_INTERFACE_UNAVAILABLE")
        return reader(tool, arguments)

    def _value_net_profit(self, *, position: dict[str, Any], direction: str, symbol: str,
                          params: dict[str, Any]) -> dict[str, Any]:
        quote = params.get("observed_quote") or {"bid": params.get("observed_bid"), "ask": params.get("observed_ask")}
        account = self._read_tool("mt5_account_info", {})
        spec = self._read_tool("mt5_symbol_info", {"symbol": symbol})
        history = self._read_tool("mt5_history", {"limit": 500})
        rows = ((history.get("deals") or history.get("history") or history.get("rows"))
                if isinstance(history, dict) else history)
        charged = position.get("commission")
        if charged is None and isinstance(rows, list):
            ticket = str(position.get("ticket"))
            matched = [r for r in rows if isinstance(r, dict) and ticket in {str(r.get("ticket")), str(r.get("order")), str(r.get("position_id"))}]
            if matched:
                charged = sum(float(r.get("commission") or 0.0) for r in matched)
        valuation = estimate_net_liquidation_profit(direction=direction, position=position, symbol_info=spec,
                                                     account_info=account, bid=float(quote["bid"]), ask=float(quote["ask"]),
                                                     charged_commission=charged)
        return valuation.to_dict()

    def _value_profit_r(self, *, managed_trade_id: str, position: dict[str, Any], direction: str,
                        symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        quote = params.get("observed_quote") or {"bid": params.get("observed_bid"), "ask": params.get("observed_ask")}
        account = self._read_tool("mt5_account_info", {})
        spec = self._read_tool("mt5_symbol_info", {"symbol": symbol})
        history = self._read_tool("mt5_history", {"limit": 500})
        rows = ((history.get("deals") or history.get("history") or history.get("rows"))
                if isinstance(history, dict) else history)
        charged = position.get("commission")
        if charged is None and isinstance(rows, list):
            ticket = str(position.get("ticket"))
            matched = [r for r in rows if isinstance(r, dict) and ticket in {str(r.get("ticket")), str(r.get("order")), str(r.get("position_id"))}]
            if matched:
                charged = sum(float(r.get("commission") or 0.0) for r in matched)
        valuation = estimate_net_liquidation_profit(direction=direction, position=position, symbol_info=spec,
                                                     account_info=account, bid=float(quote["bid"]), ask=float(quote["ask"]),
                                                     charged_commission=charged, require_usd=False)
        with self.conn.cursor() as cur:
            cur.execute("SELECT initial_stop FROM trade_management.managed_trade WHERE managed_trade_id=%s",
                        (managed_trade_id,))
            row = cur.fetchone()
        if row is None or row[0] is None:
            raise ExitValuationError("INITIAL_STOP_UNAVAILABLE")
        initial_risk = estimate_initial_monetary_risk(position=position, symbol_info=spec,
                                                      initial_stop=float(row[0]),
                                                      charged_commission=float(valuation.charged_commission),
                                                      estimated_close_commission=float(valuation.estimated_close_commission))
        return {**valuation.to_dict(), "initial_risk_amount": initial_risk,
                "observed_profit_r": valuation.estimated_net_profit / initial_risk}

    def _record_exit_outcome(self, managed_trade_id: str, *, exit_price: float | None,
                             exit_reason: str = "TIME_EXIT", realized_net_profit: float | None = None) -> None:
        """Project a close outcome only after broker confirmation of the reduce-only close."""
        with self.conn.cursor() as cur:
            cur.execute("""SELECT mt.entry_signal_id, mt.strategy_id, mt.direction,
                                  mt.reference_entry_price, mt.risk_distance
                             FROM trade_management.managed_trade mt
                            WHERE mt.managed_trade_id=%s""", (managed_trade_id,))
            row = cur.fetchone()
            if row is None or row[1] != "CONTEXT_STRUCTURE_RETRACE_V1":
                return
            signal_id, strategy_id, direction, entry, risk = row
            if exit_price is None or entry is None or not risk:
                return
            signed = float(exit_price) - float(entry) if direction == "LONG" else float(entry) - float(exit_price)
            realized_r = signed / float(risk)
            status = "TIME_EXIT" if exit_reason == "TIME_EXIT" else "PROFIT_EXIT"
            cur.execute("""INSERT INTO strategy.entry_signal_outcomes
                    (signal_id, outcome_type, status, realized_r, exit_timestamp, source)
                VALUES (%s, 'ENTRY_ONLY', %s, %s, now(), %s)
                ON CONFLICT (signal_id) DO UPDATE SET status=EXCLUDED.status, realized_r=EXCLUDED.realized_r,
                    exit_timestamp=EXCLUDED.exit_timestamp, updated_at=now()
                WHERE strategy.entry_signal_outcomes.status='OPEN'""",
                        (signal_id, status, realized_r, strategy_id))

    # ---- entry point -----------------------------------------------------------------------
    def process_decision(self, payload: dict[str, Any], *, now_utc: datetime) -> ManagementOutcome:
        tm_action = str(payload.get("action") or "")
        broker_action = MANAGEMENT_ACTIONS.get(tm_action)
        decision_id, managed_trade_id = payload.get("decision_id"), payload.get("managed_trade_id")
        if broker_action is None or not decision_id or not managed_trade_id:
            return ManagementOutcome("IGNORED", reason="NOT_ACTIONABLE")
        if self.tm_mode_reader() != "LIVE":
            return ManagementOutcome("IGNORED", reason="TRADE_MANAGER_NOT_LIVE")
        link = self._platform_link(managed_trade_id)
        if link is None:
            return ManagementOutcome("IGNORED", reason="NOT_A_PLATFORM_POSITION")
        link["managed_trade_id"] = managed_trade_id
        if self._existing(decision_id) is not None:
            return ManagementOutcome("DUPLICATE", reason="DECISION_ALREADY_HANDLED")
        if broker_action == "CLOSE" and self._existing_close(managed_trade_id) is not None:
            return ManagementOutcome("DUPLICATE", reason="CLOSE_ALREADY_IN_FLIGHT")

        intent_id = "MGMT_" + hashlib.sha256(f"{decision_id}|{self.w.account_id}".encode()).hexdigest()[:24]
        decision_time = payload.get("decision_time")
        base = {"management_intent_id": intent_id, "decision_id": decision_id, "managed_trade_id": managed_trade_id,
                "execution_intent_id": link["execution_intent_id"], "account_id": self.w.account_id,
                "broker_ticket": link["ticket"], "broker_symbol": link["symbol"], "tm_action": tm_action,
                "broker_action": broker_action, "decision_time": decision_time}

        if self.w.authority_provider is not None and self.w.authority_provider() != "ENABLED":
            return self._reject(base, "EXECUTION_AUTHORITY_DISABLED")
        try:
            decided = datetime.fromisoformat(str(decision_time).replace("Z", "+00:00"))
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return self._reject(base, "DECISION_TIME_INVALID")
        if (now_utc - decided).total_seconds() > MAX_DECISION_AGE_SECONDS:
            return self._reject(base, "STALE_DECISION")

        try:
            positions = self.w.bridge.read_positions()
        except Exception as exc:  # noqa: BLE001 - unknown broker state: never act
            return self._reject(base, "BROKER_POSITIONS_UNAVAILABLE", broker_response={"error": str(exc)[:300]})
        position = next((p for p in positions if str(p.get("ticket")) == link["ticket"]), None)
        if position is None:
            return self._reject(base, "POSITION_NOT_FOUND")
        position_direction = "LONG" if int(position.get("type", -1)) == 0 else "SHORT"
        if position_direction != link["direction"]:
            return self._reject(base, "POSITION_DIRECTION_MISMATCH")
        broker_stop = float(position.get("sl") or 0.0)
        broker_target = float(position.get("tp") or 0.0)
        base.update(broker_stop_before=broker_stop, broker_target_before=broker_target)

        params = payload.get("parameters") or {}
        exit_reason = str(params.get("exit_reason") or "TIME_EXIT")
        if broker_action == "CLOSE":
            base.update(exit_policy_snapshot=params.get("exit_policy") or {},
                        observed_quote=params.get("observed_quote") or {
                            "bid": params.get("observed_bid"), "ask": params.get("observed_ask")},
                        exit_trigger_reason=params.get("exit_trigger_reason") or exit_reason)
            if exit_reason == "NET_PROFIT_USD":
                try:
                    valuation = self._value_net_profit(position=position, direction=link["direction"],
                                                       symbol=link["symbol"], params=params)
                except (ExitValuationError, KeyError, TypeError, ValueError) as exc:
                    return self._reject(base, str(exc), broker_response={"valuation_error": str(exc)[:300]})
                base["estimated_net_profit"] = valuation["estimated_net_profit"]
                base["observed_quote"] = {"bid": params.get("observed_bid"), "ask": params.get("observed_ask"),
                                           "close_price": valuation["close_price"]}
                target = float(params.get("net_profit_target_usd") or
                               (params.get("exit_policy") or {}).get("net_profit_target_usd") or 0)
                if target <= 0 or valuation["estimated_net_profit"] < target:
                    return self._reject(base, "NET_PROFIT_TARGET_NOT_REACHED", broker_response=valuation)
            elif exit_reason == "PROFIT_R":
                try:
                    valuation = self._value_profit_r(managed_trade_id=managed_trade_id, position=position,
                                                     direction=link["direction"], symbol=link["symbol"], params=params)
                except (ExitValuationError, KeyError, TypeError, ValueError) as exc:
                    return self._reject(base, "PROFIT_R_VALUATION_UNAVAILABLE",
                                        broker_response={"valuation_error": str(exc)[:300]})
                base["estimated_net_profit"] = valuation["estimated_net_profit"]
                base["initial_risk_amount"] = valuation["initial_risk_amount"]
                base["observed_quote"] = {"bid": params.get("observed_bid"), "ask": params.get("observed_ask"),
                                           "close_price": valuation["close_price"],
                                           "observed_profit_r": valuation["observed_profit_r"]}
                target = float(params.get("profit_target_r") or
                               (params.get("exit_policy") or {}).get("profit_target_r") or 0)
                if target <= 0 or valuation["observed_profit_r"] < target:
                    return self._reject(base, "PROFIT_R_TARGET_NOT_REACHED", broker_response=valuation)
            elif exit_reason == "PROFIT_PIPS":
                try:
                    quote = params.get("observed_quote") or {"bid": params.get("observed_bid"), "ask": params.get("observed_ask")}
                    spec = self._read_tool("mt5_symbol_info", {"symbol": link["symbol"]})
                    pip_size = float(spec.get("pip_size") or spec.get("point") or 0)
                    close_price = float(quote["bid"] if link["direction"] == "LONG" else quote["ask"])
                    entry_price = float(position["price_open"])
                    movement = close_price - entry_price if link["direction"] == "LONG" else entry_price - close_price
                    observed_pips = movement / pip_size if pip_size > 0 else 0
                    target = float(params.get("profit_target_pips") or
                                  (params.get("exit_policy") or {}).get("profit_target_pips") or 0)
                except (ExitValuationError, KeyError, TypeError, ValueError) as exc:
                    return self._reject(base, "PROFIT_PIPS_VALUATION_UNAVAILABLE",
                                        broker_response={"valuation_error": str(exc)[:300]})
                base["observed_quote"] = {"bid": quote.get("bid"), "ask": quote.get("ask"),
                                           "close_price": close_price, "pip_size": pip_size,
                                           "observed_profit_pips": observed_pips}
                if target <= 0 or observed_pips < target:
                    return self._reject(base, "PROFIT_PIPS_TARGET_NOT_REACHED",
                                        broker_response={"observed_profit_pips": observed_pips, "target": target})
        if broker_action == "MODIFY":
            requested_stop = float(params["new_stop"]) if params.get("new_stop") is not None else broker_stop
            requested_target = float(params["new_target"]) if params.get("new_target") is not None else broker_target
            base.update(requested_stop=requested_stop, requested_target=requested_target)
            if not stop_is_safe(link["direction"], broker_stop, requested_stop):
                return self._reject(base, "RISK_INCREASING_STOP_CHANGE")
            if requested_target < 0:
                return self._reject(base, "INVALID_TARGET")
            if _same(requested_stop, broker_stop) and _same(requested_target, broker_target):
                self._record({**base, "status": "NO_CHANGE", "completed_at": datetime.now(timezone.utc)})
                return ManagementOutcome("NO_CHANGE", intent_id)
            args = {"ticket": int(link["ticket"]), "stop_loss": requested_stop, "take_profit": requested_target,
                    "confirm": True}
        else:
            args = {"ticket": int(link["ticket"]), "confirm": True}

        tool = BROKER_TOOLS[broker_action]
        action_key = "MA_" + hashlib.sha256(json.dumps(
            {"ticket": link["ticket"], "action": broker_action, "stop": args.get("stop_loss"),
             "target": args.get("take_profit")}, sort_keys=True).encode()).hexdigest()[:32]
        if not self._record({**base, "action_key": action_key, "status": "AUTHORIZED", "attempt_id": intent_id}):
            return self._reject({**base, "management_intent_id": intent_id}, "DUPLICATE_MANAGEMENT_ACTION")
        return self._execute(intent_id, tool, args, broker_action, link,
                             exit_reason=exit_reason,
                             exit_price=float(position.get("price_current") or position.get("price_open") or 0.0)
                             if broker_action == "CLOSE" else None)

    # ---- fenced write --------------------------------------------------------------------------
    def _execute(self, intent_id: str, tool: str, args: dict[str, Any], broker_action: str,
                 link: dict[str, Any], exit_reason: str = "TIME_EXIT",
                 exit_price: float | None = None) -> ManagementOutcome:
        try:
            generation = self.w._acquire_generation()
        except Exception as exc:  # noqa: BLE001
            self._update(intent_id, status="FENCED", reason=f"OWNERSHIP_ACQUISITION_FAILED: {exc}"[:300],
                         completed_at=datetime.now(timezone.utc))
            return ManagementOutcome("FENCED", intent_id, "OWNERSHIP_ACQUISITION_FAILED")
        if self.w.authority_provider is not None and self.w.authority_provider() != "ENABLED":
            self._update(intent_id, status="FENCED", reason="AUTHORITY_DISABLED_BEFORE_SUBMISSION",
                         generation=generation, completed_at=datetime.now(timezone.utc))
            return ManagementOutcome("FENCED", intent_id, "AUTHORITY_DISABLED_BEFORE_SUBMISSION")
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("SELECT platform.assert_generation(%s,%s)", (self.w.resource, generation))
                cur.execute("UPDATE execution_v2.management_intent SET status='SENDING', generation=%s "
                            "WHERE management_intent_id=%s", (generation, intent_id))
        authorization = self.w.fence_authority.mint_authorization(
            resource=self.w.resource, generation=generation, attempt_id=intent_id, tool=tool,
            request_fingerprint=management_request_fingerprint(tool, args), scope_class="REDUCE_ONLY")
        try:
            result = self.w.bridge.submit_management(authorization=authorization, request_args=args)
        except (WrongAccount, InvalidSignature, RequestFingerprintMismatch) as exc:
            self._update(intent_id, status="FENCED", reason=f"BRIDGE_REJECTED_BEFORE_DISPATCH: {exc}"[:300],
                         completed_at=datetime.now(timezone.utc))
            return ManagementOutcome("FENCED", intent_id, "BRIDGE_REJECTED_BEFORE_DISPATCH")
        except Exception as exc:  # noqa: BLE001 - transport: broker effect unknown
            return self._reconcile(intent_id, broker_action, args, link, exit_reason,
                                   {"transport_error": str(exc)[:300]})

        response = result.broker_response
        if result.state == "DISPATCHED" and isinstance(response, dict):
            data = response.get("data") if isinstance(response.get("data"), dict) else response
            if data.get("ok") is True:
                realized_net = data.get("realized_net_profit") or data.get("profit")
                self._update(intent_id, status="APPLIED", broker_response=data,
                             realized_net_profit=realized_net, completed_at=datetime.now(timezone.utc))
                if broker_action == "CLOSE":
                    broker_price = data.get("actual_price") or data.get("close_price") or exit_price
                    self._record_exit_outcome(link["managed_trade_id"],
                                              exit_price=float(broker_price) if broker_price else None,
                                              exit_reason=exit_reason,
                                              realized_net_profit=realized_net)
                return ManagementOutcome("APPLIED", intent_id)
            # A refusal (EA validation error) or a request the broker did not accept (sent=false) is
            # a definite rejection; sent-but-not-verifiably-applied falls through to reconciliation.
            if data.get("ok") is False and (data.get("error") or data.get("sent") is False):
                reason = str(data.get("error") or data.get("retcode_description") or f"retcode {data.get('retcode')}")
                self._update(intent_id, status="BROKER_REJECTED", reason=reason[:300],
                             broker_response=data, completed_at=datetime.now(timezone.utc))
                return ManagementOutcome("BROKER_REJECTED", intent_id, reason)
        return self._reconcile(intent_id, broker_action, args, link, exit_reason,
                               {"bridge_state": result.state, "broker_response": response})

    def _reconcile(self, intent_id: str, broker_action: str, args: dict[str, Any], link: dict[str, Any],
                   exit_reason: str, evidence: dict[str, Any]) -> ManagementOutcome:
        """Goal-state reconciliation: never retry a write whose outcome is unknown."""
        try:
            position = next((p for p in self.w.bridge.read_positions() if str(p.get("ticket")) == link["ticket"]), None)
        except Exception as exc:  # noqa: BLE001
            position, evidence = "UNREADABLE", {**evidence, "reconcile_error": str(exc)[:300]}
        if position != "UNREADABLE":
            reached = (position is None if broker_action == "CLOSE" else
                       position is not None and _same(float(position.get("sl") or 0), args["stop_loss"])
                       and _same(float(position.get("tp") or 0), args["take_profit"]))
            if reached:
                self._update(intent_id, status="APPLIED", reason="GOAL_STATE_RECONCILED",
                             broker_response={**evidence, "position": position}, completed_at=datetime.now(timezone.utc))
                if broker_action == "CLOSE":
                    self._record_exit_outcome(link["managed_trade_id"],
                                              exit_price=None if position is None else
                                              float(position.get("price_current") or position.get("price_open") or 0.0),
                                              exit_reason=exit_reason)
                return ManagementOutcome("APPLIED", intent_id, "GOAL_STATE_RECONCILED")
        self._update(intent_id, status="UNKNOWN_RECONCILIATION_REQUIRED", reason="OUTCOME_UNKNOWN",
                     broker_response=evidence)
        return ManagementOutcome("UNKNOWN_RECONCILIATION_REQUIRED", intent_id, "OUTCOME_UNKNOWN")
