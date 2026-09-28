"""Live, broker-backed Trade Manager projection (read-only).

A ManagedTrade row is created for every canonical EntrySignal in SHADOW mode and its internal
`state` stays OPEN - nothing in the TM domain ever closes it, because the TM domain is
deliberately broker-free (tests/test_trade_management_isolation.py). That row therefore says
nothing about whether a broker position exists. This module answers that question at read time:

    ManagedTrade.entry_signal_id
      -> execution_v2.execution_intent -> execution_attempt -> execution_result (FILLED)
      -> execution_result.broker_position_id + account_id      (authoritative identity)
      -> read-only MT5 bridge `mt5_positions` ticket on that same account  (current truth)

ACTIVE requires both: a broker position identity for the trade AND that position present in
the current broker read. Nothing is inferred from symbol, direction or time. Linked trades whose
position is absent are CLOSED (history); trades with no broker identity are
HISTORICAL_UNRECONCILED. If the broker cannot be read, no position is asserted open.

Position identity is `execution_result.broker_position_id` when recorded. MT5 order-placement
results carry only order and deal tickets, so a FILLED result may have only
`broker_order_id`. On a HEDGING account every market deal opens its own position whose ticket
is the opening order's ticket (MT5 invariant), so the order ticket is then the position id
(`position_id_source = ORDER_TICKET_HEDGING`). On netting accounts, or when the account's
margin mode cannot be read, a deal may join an existing position, so order-ticket-only results
stay unlinked.

No broker writes: the only bridge calls are the allow-listed read-only tools of
`platform_api.control.ReadOnlyBridgeReader`.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable

LIVE = "LIVE"
DEGRADED = "DEGRADED"
DISCONNECTED = "DISCONNECTED"

DEFAULT_STALE_AFTER_SECONDS = 120.0

# Linkage bucket (repository.managed_trade_linkage_counts) for FILLED results that recorded an
# order ticket but no position id; counted as linked only on a HEDGING account.
ORDER_TICKET_ONLY = "BROKER_FILLED_ORDER_TICKET_ONLY"

# MT5 ACCOUNT_TRADE_MODE / POSITION_TYPE constants as returned by the bridge.
_ACCOUNT_TRADE_MODE = {0: "DEMO", 1: "CONTEST", 2: "REAL"}
_POSITION_TYPE_DIRECTION = {0: "LONG", 1: "SHORT"}
_RESOURCE_MODE_ENVIRONMENT = {"real": "REAL", "demo": "DEMO"}


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def is_hedging_account(account: dict[str, Any] | None) -> bool:
    """ACCOUNT_MARGIN_MODE_RETAIL_HEDGING (2), as reported by the bridge's account read."""
    account = account or {}
    if str(account.get("position_mode") or "").upper() == "HEDGING":
        return True
    try:
        return int(account.get("account_margin_mode_raw", account.get("margin_mode"))) == 2
    except (TypeError, ValueError):
        return False


def position_identity(row: dict[str, Any], *, hedging: bool) -> tuple[str | None, str | None]:
    """(position id, source) for a FILLED result, or (None, None) when it cannot be proven."""
    if row.get("broker_position_id") is not None:
        return str(row["broker_position_id"]), "EXECUTION_RESULT"
    if hedging and row.get("broker_order_id") is not None:
        return str(row["broker_order_id"]), "ORDER_TICKET_HEDGING"
    return None, None


def execution_environment(resource: str | None) -> str | None:
    """`execution_attempt.resource` is `execution:{mode}:{account_id}` (execution_v2/worker.py)."""
    parts = (resource or "").split(":")
    if len(parts) >= 3 and parts[0] == "execution":
        return _RESOURCE_MODE_ENVIRONMENT.get(parts[1].lower())
    return None


def broker_environment(account: dict[str, Any] | None) -> str | None:
    mode = (account or {}).get("type", (account or {}).get("trade_mode"))
    try:
        return _ACCOUNT_TRADE_MODE.get(int(mode))
    except (TypeError, ValueError):
        return None


def excursion(direction: str, entry: float | None, risk: float | None, *, current: float | None,
              best: float | None, worst: float | None) -> dict[str, float | None]:
    """Signed by direction; `best`/`worst` are the most favourable/adverse close-side prices."""
    sign = 1.0 if direction == "LONG" else -1.0

    def move(price: float | None) -> float | None:
        return None if price is None or entry is None else sign * (price - entry)

    def r(price_move: float | None) -> float | None:
        return None if price_move is None or not risk else price_move / risk

    current_move, mfe, mae = move(current), move(best), move(worst)
    if mfe is not None:
        mfe = max(mfe, 0.0)
    if mae is not None:
        mae = min(mae, 0.0)
    return {"current_move": current_move, "current_r": r(current_move),
            "mfe_price": mfe, "mfe_r": r(mfe), "mae_price": mae, "mae_r": r(mae)}


class TradeManagerLiveProjection:
    def __init__(self, repository: Any, bridge_reader: Any, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS):
        self.repository = repository
        self.bridge_reader = bridge_reader
        self.clock = clock
        self.stale_after_seconds = stale_after_seconds

    def _read_broker(self) -> dict[str, Any]:
        observed_at = self.clock()
        calls = {
            "account": "mt5_account_info",
            "positions": "mt5_positions",
            "orders": "mt5_orders",
        }
        futures = {}
        read = getattr(self.bridge_reader, "read", None)
        observed: dict[str, float] = {}

        def fetch(key: str, tool: str) -> Any:
            if read is None:
                return self.bridge_reader.call(tool)
            data, at, _source = read(tool)
            observed[key] = at
            return data

        executor = ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="tm-live-read")
        try:
            futures = {key: executor.submit(fetch, key, tool)
                       for key, tool in calls.items()}
            timeout = max(float(getattr(self.bridge_reader, "timeout", 5.0)), 0.1)
            done, _ = wait(futures.values(), timeout=timeout)
            results: dict[str, Any] = {}
            errors: list[str] = []
            for key, future in futures.items():
                if future not in done:
                    errors.append(f"{key} read timed out after {timeout:g}s")
                    continue
                try:
                    results[key] = future.result()
                except Exception as exc:  # noqa: BLE001 - failed read means broker truth is unknown
                    errors.append(f"{key} read failed: {exc}")
            if errors:
                return {"status": "UNAVAILABLE", "observed_at": _iso(observed_at),
                        "reason": "; ".join(errors), "account_id": None, "environment": None,
                        "positions": None, "pending_orders": None}
            account = results["account"]
            positions = results["positions"]
            orders = results["orders"]
            if not isinstance(account, dict) or account.get("login") is None:
                raise RuntimeError("broker account identity unavailable")
            if not isinstance(positions, list) or not isinstance(orders, list):
                raise RuntimeError("malformed broker position/order read")
            if len(observed) == len(calls):
                # A cached view is as old as its oldest component read.
                observed_at = datetime.fromtimestamp(min(observed.values()), timezone.utc)
        except Exception as exc:  # noqa: BLE001 - any failed read means broker truth is unknown
            return {"status": "UNAVAILABLE", "observed_at": _iso(observed_at), "reason": str(exc),
                    "account_id": None, "environment": None, "positions": None, "pending_orders": None}
        finally:
            # Do not hold the HTTP request open for a stalled bridge worker.
            executor.shutdown(wait=False, cancel_futures=True)
        return {"status": "CONNECTED", "observed_at": _iso(observed_at), "reason": None,
                "account_id": str(account["login"]), "environment": broker_environment(account),
                "hedging": is_hedging_account(account),
                "server": account.get("server"), "positions": positions, "pending_orders": orders}

    def _observation(self, row: dict[str, Any], now: datetime) -> dict[str, Any]:
        observed_at = _ts(row.get("latest_observed_at"))
        if observed_at is None:
            return {"status": "UNAVAILABLE", "observed_at": None, "age_seconds": None,
                    "quote_timestamp": None, "count_since_open": int(row.get("observation_count") or 0)}
        age = (now - observed_at).total_seconds()
        return {"status": "FRESH" if age <= self.stale_after_seconds else "STALE",
                "observed_at": _iso(observed_at), "age_seconds": age,
                "quote_timestamp": _iso(_ts(row.get("latest_quote_timestamp"))),
                "count_since_open": int(row.get("observation_count") or 0),
                "stale_after_seconds": self.stale_after_seconds}

    def _trade(self, row: dict[str, Any], position: dict[str, Any] | None, broker_account_env: str | None,
               now: datetime, *, position_id: str | None = None,
               position_id_source: str | None = None) -> dict[str, Any]:
        direction = row["direction"]
        long_side = direction == "LONG"
        entry = _num((position or {}).get("price_open")) or _num(row.get("fill_price")) \
            or _num(row.get("reference_entry_price"))
        initial_stop = _num(row.get("initial_stop"))
        risk = abs(entry - initial_stop) if entry is not None and initial_stop is not None else None
        latest = _num(row.get("latest_bid") if long_side else row.get("latest_ask"))
        best = _num(row.get("max_bid") if long_side else row.get("min_ask"))
        worst = _num(row.get("min_bid") if long_side else row.get("max_ask"))
        observation = self._observation(row, now)
        stop = _num((position or {}).get("sl"))
        target = _num((position or {}).get("tp"))
        stop = stop if stop else None  # MT5 reports "no stop" as 0.0
        target = target if target else None
        opened_at = _ts(row.get("confirmed_at")) or _ts(row.get("submitted_at"))
        exec_env = execution_environment(row.get("attempt_resource"))
        env_conflict = bool(exec_env and broker_account_env and exec_env != broker_account_env)
        protected = bool(stop is not None and entry is not None
                         and (stop >= entry if long_side else stop <= entry))
        action = row.get("latest_action")
        return {
            "managed_trade_id": row["managed_trade_id"], "entry_signal_id": row["entry_signal_id"],
            "strategy_id": row["strategy_id"], "strategy_version": row.get("strategy_version"),
            "instrument": row["instrument"], "direction": direction,
            "environment": None if env_conflict else exec_env,
            "environment_source": "EXECUTION_ATTEMPT_RESOURCE" if exec_env and not env_conflict else None,
            "environment_conflict": env_conflict,
            "broker": {"account_id": row["account_id"], "position_id": position_id,
                       "position_id_source": position_id_source,
                       "order_id": row.get("broker_order_id"), "deal_id": row.get("broker_deal_id"),
                       "symbol": (position or {}).get("symbol"), "volume": _num((position or {}).get("volume"))
                       if position else _num(row.get("fill_volume")),
                       "profit": _num((position or {}).get("profit")),
                       "open_time_broker": (position or {}).get("time")},
            "execution": {"execution_intent_id": row["execution_intent_id"], "attempt_id": row["attempt_id"],
                          "execution_result_id": row["execution_result_id"], "outcome": row.get("outcome")},
            "entry": entry, "initial_stop": initial_stop, "current_stop": stop,
            "current_stop_source": "BROKER_POSITION" if position else None,
            "target": target if position else _num(row.get("initial_target")),
            "initial_target": _num(row.get("initial_target")),
            "current_price": latest, "current_price_side": "BID" if long_side else "ASK",
            "opened_at": _iso(opened_at),
            "age_seconds": (now - opened_at).total_seconds() if opened_at else None,
            "excursion": excursion(direction, entry, risk, current=latest, best=best, worst=worst),
            "risk_distance": risk, "protected": protected,
            "observation": observation,
            "tm_version_id": row.get("tm_version_id"),
            "latest_recommendation": {"action": action, "reason_codes": row.get("latest_reason_codes") or [],
                                      "decided_at": _iso(_ts(row.get("latest_decision_at")))} if action else None,
            "advisory_only": True,
        }

    def project(self) -> dict[str, Any]:
        now = self.clock()
        linked = self.repository.broker_linked_managed_trades()
        history = self.repository.managed_trade_linkage_counts()
        broker = self._read_broker()
        connected = broker["status"] == "CONNECTED"

        positions_by_id: dict[str, dict[str, Any]] = {}
        if connected:
            for position in broker["positions"]:
                if isinstance(position, dict) and position.get("ticket") is not None:
                    positions_by_id[str(position["ticket"])] = position

        active, closed, unresolved, conflicts = [], [], [], []
        claimed: set[str] = set()
        for row in linked:
            position_id, source = position_identity(row, hedging=connected and broker["hedging"])
            if position_id is None:
                continue  # order ticket only, and the account is not proven HEDGING
            identity = {"position_id": position_id, "position_id_source": source}
            if not connected:
                unresolved.append({**self._trade(row, None, None, now, **identity), "broker_status": "UNRESOLVED",
                                   "reconciliation_reason": "BROKER_READ_UNAVAILABLE"})
                continue
            if str(row["account_id"]) != broker["account_id"]:
                # Recorded on a different account than the one the bridge serves: cannot be
                # confirmed open or closed from this read.
                unresolved.append({**self._trade(row, None, broker["environment"], now, **identity),
                                   "broker_status": "UNRESOLVED",
                                   "reconciliation_reason": "ACCOUNT_NOT_OBSERVED"})
                continue
            position = positions_by_id.get(position_id)
            if position is None:
                closed.append({**self._trade(row, None, broker["environment"], now, **identity),
                               "broker_status": "CLOSED", "reconciliation_reason": "BROKER_POSITION_ABSENT"})
                continue
            if _POSITION_TYPE_DIRECTION.get(position.get("type")) != row["direction"]:
                conflicts.append({"managed_trade_id": row["managed_trade_id"], "position_id": position_id,
                                  "reason": "DIRECTION_MISMATCH"})
                continue
            claimed.add(position_id)
            active.append({**self._trade(row, position, broker["environment"], now, **identity),
                           "broker_status": "OPEN", "reconciliation_reason": "BROKER_POSITION_PRESENT"})

        unlinked_positions = [
            {"position_id": pid, "symbol": p.get("symbol"), "direction": _POSITION_TYPE_DIRECTION.get(p.get("type")),
             "volume": _num(p.get("volume")), "price_open": _num(p.get("price_open")), "sl": _num(p.get("sl")),
             "tp": _num(p.get("tp")), "profit": _num(p.get("profit"))}
            for pid, p in positions_by_id.items() if pid not in claimed]

        if not connected:
            system_state = DISCONNECTED
            summary = None
        else:
            stale = [t for t in active if t["observation"]["status"] != "FRESH"]
            env_conflicts = [t for t in active if t["environment_conflict"]]
            system_state = DEGRADED if (stale or unlinked_positions or conflicts or unresolved
                                        or env_conflicts) else LIVE
            recommended = [t for t in active if (t["latest_recommendation"] or {}).get("action") not in (None, "HOLD")]
            summary = {"active_trades": len(active),
                       "protected_trades": sum(1 for t in active if t["protected"]),
                       "actions_pending": len(recommended),
                       "holding_or_no_action": len(active) - len(recommended),
                       "stale_observations": len(stale)}

        degraded_reasons = []
        if not connected:
            degraded_reasons.append("BROKER_READ_UNAVAILABLE")
        else:
            if summary and summary["stale_observations"]:
                degraded_reasons.append("ACTIVE_OBSERVATION_STALE_OR_UNAVAILABLE")
            if unlinked_positions:
                degraded_reasons.append("BROKER_POSITION_WITHOUT_MANAGED_TRADE")
            if conflicts:
                degraded_reasons.append("BROKER_IDENTITY_CONFLICT")
            if unresolved:
                degraded_reasons.append("LINKED_TRADE_UNRESOLVED")
            if any(t["environment_conflict"] for t in active):
                degraded_reasons.append("ENVIRONMENT_CONFLICT")

        total = sum(int(v) for v in history.values())
        linked_trades = int(history.get("BROKER_LINKED", 0))
        if connected and broker["hedging"]:
            linked_trades += int(history.get(ORDER_TICKET_ONLY, 0))
            history = {k: v for k, v in history.items() if k != ORDER_TICKET_ONLY}
        return {
            "projection": "trade-manager-live.v1",
            "observed_at": _iso(now),
            "system_state": system_state,
            "degraded_reasons": degraded_reasons,
            "position_authority": "MT5_BRIDGE_READ_ONLY",
            "advisory_only": True,
            "broker_writes": 0,
            "broker": {k: broker[k] for k in ("status", "observed_at", "reason", "account_id", "environment")}
                      | {"open_positions": len(broker["positions"]) if connected else None,
                         "pending_orders": len(broker["pending_orders"]) if connected else None},
            "summary": summary,
            "active": active,
            "closed": closed,
            "unresolved": unresolved,
            "identity_conflicts": conflicts,
            "unlinked_broker_positions": unlinked_positions,
            "historical_unreconciled": {"count": total - linked_trades,
                                        "by_linkage": {k: int(v) for k, v in history.items() if k != "BROKER_LINKED"}},
            "managed_trade_count": total,
        }
