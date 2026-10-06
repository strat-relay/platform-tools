"""Normalized broker risk state (RiskSnapshot) for cached V2 execution risk evaluation.

The collector (collector.py) is the only producer: it reads the read-only MT5 bridge, normalizes
through the functions here, validates, and stores the result in Redis. Execution only consumes it.
Normalization never turns a missing critical value into zero: malformed broker rows raise
MalformedBrokerState, and the collector then keeps the last valid snapshot instead.

The cache is never more authoritative than the broker: every value here is a timestamped
observation of broker truth, re-read on a fixed cadence.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

SCHEMA_VERSION = "risk-snapshot.v1"
REFERENCE_SCHEMA_VERSION = "risk-reference.v1"
HEALTHY, DEGRADED, UNAVAILABLE = "healthy", "degraded", "unavailable"
COMPONENTS = ("fast", "history", "reference")


class MalformedBrokerState(ValueError):
    """A broker read cannot be normalized safely; the previous valid snapshot must be kept."""

    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code


def account_ref(account_id: str) -> str:
    """Stable non-reversible account reference for keys, logs and UI (never the raw account id)."""
    return hashlib.sha256(f"stratrelay-account:{account_id}".encode()).hexdigest()[:16]


def _finite(value: Any, stage: str, code: str, name: str, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MalformedBrokerState(stage, code, f"{name} is missing or not numeric") from None
    if not math.isfinite(number) or (positive and number <= 0):
        raise MalformedBrokerState(stage, code, f"{name} is not a valid {'positive ' if positive else ''}number")
    return number


def _rows(payload: Any, key: str, stage: str) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise MalformedBrokerState(stage, "MALFORMED_ROWS", f"broker {key} are malformed")
    return rows


@dataclass(frozen=True)
class Position:
    ticket: str
    provider_symbol: str
    canonical_instrument: str | None
    direction: str
    volume: float
    open_price: float
    stop_loss: float | None      # None: the broker reports no stop (MT5 sends 0.0)
    take_profit: float | None
    opened_at: int | None


@dataclass(frozen=True)
class PendingOrder:
    ticket: str
    provider_symbol: str
    canonical_instrument: str | None
    order_type: int | None
    volume: float


def normalize_positions(payload: Any, canonical_for: Any) -> list[Position]:
    out = []
    for row in _rows(payload, "positions", "POSITIONS"):
        ticket, symbol = row.get("ticket"), row.get("symbol")
        if ticket is None or not isinstance(symbol, str) or not symbol:
            raise MalformedBrokerState("POSITIONS", "POSITION_IDENTITY_MISSING", "position without ticket/symbol")
        kind = row.get("type")
        if kind not in (0, 1):
            raise MalformedBrokerState("POSITIONS", "POSITION_DIRECTION_INVALID", "position type is not BUY/SELL")
        sl = row.get("sl")
        tp = row.get("tp")
        out.append(Position(
            ticket=str(ticket), provider_symbol=symbol, canonical_instrument=canonical_for(symbol),
            direction="LONG" if kind == 0 else "SHORT",
            volume=_finite(row.get("volume"), "POSITIONS", "POSITION_VOLUME_INVALID", "position volume", positive=True),
            open_price=_finite(row.get("price_open"), "POSITIONS", "POSITION_PRICE_INVALID", "position open price",
                               positive=True),
            stop_loss=None if sl in (None, 0, 0.0) else _finite(sl, "POSITIONS", "POSITION_STOP_INVALID", "stop loss"),
            take_profit=None if tp in (None, 0, 0.0) else _finite(tp, "POSITIONS", "POSITION_TARGET_INVALID", "take profit"),
            opened_at=int(row["time"]) if isinstance(row.get("time"), (int, float)) else None))
    return out


def normalize_orders(payload: Any, canonical_for: Any) -> list[PendingOrder]:
    out = []
    for row in _rows(payload, "orders", "ORDERS"):
        ticket, symbol = row.get("ticket"), row.get("symbol")
        if ticket is None or not isinstance(symbol, str) or not symbol:
            raise MalformedBrokerState("ORDERS", "ORDER_IDENTITY_MISSING", "order without ticket/symbol")
        volume = row.get("volume_current", row.get("volume_initial", row.get("volume")))
        out.append(PendingOrder(ticket=str(ticket), provider_symbol=symbol, canonical_instrument=canonical_for(symbol),
                                order_type=row.get("type") if isinstance(row.get("type"), int) else None,
                                volume=_finite(volume, "ORDERS", "ORDER_VOLUME_INVALID", "order volume", positive=True)))
    return out


def daily_loss_from_history(payload: Any, day: date) -> tuple[float, float]:
    """Return net daily realized P&L and net daily loss for the UTC ``day``.

    Winning deals offset losing deals; a profitable day reports zero loss. Any row missing a
    timestamp or profit is malformed (never counted as zero).
    """
    if isinstance(payload, dict):
        payload = payload.get("deals") or payload.get("history") or payload.get("rows")
    if not isinstance(payload, list):
        raise MalformedBrokerState("HISTORY", "HISTORY_UNAVAILABLE", "broker history is unavailable")
    realized = 0.0
    for row in payload:
        if not isinstance(row, dict):
            raise MalformedBrokerState("HISTORY", "HISTORY_ROW_MALFORMED", "broker history row is malformed")
        stamp, pnl = row.get("time") or row.get("timestamp") or row.get("close_time"), row.get("profit")
        if stamp is None or pnl is None:
            raise MalformedBrokerState("HISTORY", "HISTORY_FIELDS_MISSING", "broker history lacks loss-accounting fields")
        try:
            when = (datetime.fromtimestamp(float(stamp), tz=timezone.utc) if isinstance(stamp, (int, float))
                    else datetime.fromisoformat(str(stamp).replace("Z", "+00:00")))
            result = float(pnl) + float(row.get("commission", 0) or 0) + float(row.get("swap", 0) or 0)
        except (TypeError, ValueError):
            raise MalformedBrokerState("HISTORY", "HISTORY_TIMESTAMP_MALFORMED", "broker history row is malformed") from None
        if when.date() == day:
            realized += result
    return realized, max(0.0, -realized)


def normalize_reference(symbol_info: Any, provider_symbol: str) -> dict[str, Any]:
    if not isinstance(symbol_info, dict):
        raise MalformedBrokerState("REFERENCE", "SYMBOL_INFO_MALFORMED", f"symbol metadata for {provider_symbol} is malformed")
    names = {"tick_size": "tick_size", "tick_value": "tick_value", "volume_min": "min_lot",
             "volume_max": "max_lot", "volume_step": "lot_step"}
    values = {k: _finite(symbol_info.get(src), "REFERENCE", "SYMBOL_SIZING_INCOMPLETE", f"{provider_symbol} {src}",
                         positive=True) for k, src in names.items()}
    values["version"] = hashlib.sha256(repr(sorted(values.items())).encode()).hexdigest()[:12]
    return values


@dataclass
class RiskSnapshot:
    account_ref: str
    provider: str = "MT5"
    schema_version: str = SCHEMA_VERSION
    generation: int = 0
    observed_at: float | None = None
    balance: float | None = None
    equity: float | None = None
    equity_observed_at: float | None = None
    positions_observed_at: float | None = None
    orders_observed_at: float | None = None
    history_observed_at: float | None = None
    open_positions: list[Position] = field(default_factory=list)
    pending_orders: list[PendingOrder] = field(default_factory=list)
    trading_day: str | None = None
    daily_realized_pnl: float | None = None
    daily_loss: float | None = None
    source_health: dict[str, str] = field(default_factory=lambda: {c: UNAVAILABLE for c in COMPONENTS})
    source_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def open_position_count(self) -> int:
        return len(self.open_positions)

    @property
    def pending_order_count(self) -> int:
        return len(self.pending_orders)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["open_position_count"] = self.open_position_count
        data["pending_order_count"] = self.pending_order_count
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskSnapshot":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise MalformedBrokerState("SNAPSHOT_READ", "SNAPSHOT_SCHEMA_MISMATCH",
                                       f"unsupported snapshot schema {data.get('schema_version')!r}")
        fields_ = {k: v for k, v in data.items() if k not in ("open_position_count", "pending_order_count")}
        fields_["open_positions"] = [Position(**p) for p in data.get("open_positions") or []]
        fields_["pending_orders"] = [PendingOrder(**o) for o in data.get("pending_orders") or []]
        snapshot = cls(**fields_)
        if data.get("open_position_count", snapshot.open_position_count) != snapshot.open_position_count:
            raise MalformedBrokerState("SNAPSHOT_READ", "SNAPSHOT_INCONSISTENT", "position count does not match rows")
        return snapshot


def position_open_risk(position: Position, reference: dict[str, Any] | None) -> float:
    """Loss to the broker stop in account currency; unbounded (inf) without a stop. Raises when the
    position's symbol metadata is unknown, because exposure then cannot be computed safely."""
    if reference is None:
        raise MalformedBrokerState("EXPOSURE", "POSITION_REFERENCE_MISSING",
                                   f"no symbol metadata for open position symbol {position.provider_symbol}")
    if position.stop_loss is None:
        return math.inf
    distance = (position.open_price - position.stop_loss if position.direction == "LONG"
                else position.stop_loss - position.open_price)
    return max(0.0, distance) / float(reference["tick_size"]) * float(reference["tick_value"]) * position.volume
