"""Pure, broker-free trade-management evaluation primitives."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

ACTIONS = ("HOLD", "PROTECT_STOP", "MOVE_BREAKEVEN", "TRAIL_STOP",
           "REDUCE_POSITION", "CLOSE_POSITION", "ADD_POSITION", "REENTRY_ELIGIBLE")

REASONS = {
    "EMA_APPROACH", "EMA_TOUCH", "EMA_REJECTION", "STRUCTURE_BREAK", "STRUCTURE_HOLD",
    "CONTINUATION_CONFIRMED", "PROFIT_THRESHOLD_REACHED", "BREAKEVEN_ELIGIBLE",
    "TRAIL_ELIGIBLE", "MOMENTUM_WEAKENING", "TARGET_APPROACH", "RISK_INCREASED",
    "ADD_ELIGIBLE", "REENTRY_ELIGIBLE", "NO_MANAGEMENT_CHANGE", "DATA_UNAVAILABLE",
    "POSITION_CLOSED", "STALE_MARKET_DATA", "INVALID_POSITION_STATE",
}


@dataclass(frozen=True)
class ManagementPolicy:
    policy_id: str = "CONTEXT_V1_MANAGEMENT_EXPERIMENT_V1"
    version: str = "1"
    mode: str = "ADVISORY_SHADOW"
    breakeven_enabled: bool = False
    trailing_enabled: bool = False
    reductions_enabled: bool = False
    closes_enabled: bool = False
    additions_enabled: bool = False
    reentry_enabled: bool = False
    breakeven_r: float | None = None
    trail_distance: float | None = None
    ema_tolerance: float | None = None
    max_market_age_ms: int | None = None


def position_r(direction: str, entry: float, stop: float, price: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("non-positive risk distance")
    direction = direction.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    return ((entry - price) if direction == "SHORT" else (price - entry)) / risk


def excursion(direction: str, entry: float, stop: float, price: float,
              previous_mfe_r: float = 0.0, previous_mae_r: float = 0.0) -> dict[str, float]:
    """Update MFE/MAE using only the supplied observation and prior state."""
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        raise ValueError("non-positive risk distance")
    favorable = (float(entry) - float(price)) if direction.upper() == "SHORT" else (float(price) - float(entry))
    adverse = -favorable
    mfe_r = max(float(previous_mfe_r), max(0.0, favorable / risk))
    mae_r = max(float(previous_mae_r), max(0.0, adverse / risk))
    return {"mfe": max(0.0, favorable, previous_mfe_r * risk),
            "mae": max(0.0, adverse, previous_mae_r * risk),
            "mfe_R": mfe_r, "mae_R": mae_r}


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def ema_interaction(price: float, ema_value: float | None, tolerance: float | None) -> str | None:
    if ema_value is None or tolerance is None:
        return None
    distance = abs(float(price) - float(ema_value))
    return "EMA_TOUCH" if distance <= float(tolerance) else ("EMA_APPROACH" if distance <= float(tolerance) * 2 else None)


def ema_rejection(candle: dict[str, float], ema_value: float, direction: str) -> bool:
    """Causal single-candle rejection test; caller supplies a closed candle only."""
    high, low, close = float(candle["high"]), float(candle["low"]), float(candle["close"])
    if direction.upper() == "SHORT":
        return high >= float(ema_value) and close < float(ema_value)
    if direction.upper() == "LONG":
        return low <= float(ema_value) and close > float(ema_value)
    raise ValueError("direction must be LONG or SHORT")


def structural_stop(direction: str, structure_level: float, buffer: float = 0.0) -> float:
    if direction.upper() == "SHORT":
        return float(structure_level) + abs(float(buffer))
    if direction.upper() == "LONG":
        return float(structure_level) - abs(float(buffer))
    raise ValueError("direction must be LONG or SHORT")


def idempotency_key(position: dict[str, Any], action: str, timestamp: str) -> str:
    raw = "|".join(str(position.get(k, "")) for k in
                    ("strategy_id", "setup_id", "economic_position_id")) + f"|{action}|{timestamp}"
    return sha256(raw.encode()).hexdigest()


class TradeManager:
    """Pure evaluator. It has no broker/provider dependency and cannot write orders."""

    def __init__(self, policy: ManagementPolicy | None = None):
        self.policy = policy or ManagementPolicy()
        self._emitted: set[str] = set()

    def evaluate(self, position: dict[str, Any], market: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
        required = ("strategy_id", "setup_id", "economic_position_id", "symbol", "direction",
                    "entry", "original_stop", "original_target", "size")
        if any(k not in position for k in required):
            return self._record(position, market, timestamp, "HOLD", ["INVALID_POSITION_STATE"],
                                 "Position state is incomplete; no management action proposed.")
        if position.get("status") in {"CLOSED", "STOPPED", "TARGET_HIT"}:
            return self._record(position, market, timestamp, "HOLD", ["POSITION_CLOSED"],
                                 "Position is already terminal.")
        price = market.get("current_price")
        if price is None or market.get("timestamp") is None:
            return self._record(position, market, timestamp, "HOLD", ["DATA_UNAVAILABLE"],
                                 "No current market observation was available.")
        if market.get("stale") or (self.policy.max_market_age_ms is not None and
                                    market.get("age_ms") is not None and
                                    float(market["age_ms"]) > self.policy.max_market_age_ms):
            return self._record(position, market, timestamp, "HOLD", ["STALE_MARKET_DATA"],
                                "Market observation exceeded the advisory freshness limit.")
        current_r = position_r(position["direction"], float(position["entry"]),
                               float(position["original_stop"]), float(price))
        prior_mfe = float(position.get("mfe_R") or 0.0)
        prior_mae = float(position.get("mae_R") or 0.0)
        exc = excursion(position["direction"], position["entry"], position["original_stop"], price,
                        prior_mfe, prior_mae)
        reasons = ["NO_MANAGEMENT_CHANGE"]
        explanation = "No executable management threshold is enabled by this advisory policy."
        action = "HOLD"
        interaction = ema_interaction(price, market.get("ema_200"),
                                      market.get("ema_tolerance", self.policy.ema_tolerance))
        if interaction:
            reasons = [interaction]
            explanation = "Context EMA interaction observed; no EMA exit is enabled in this policy."
        if self.policy.breakeven_enabled and self.policy.breakeven_r is not None and current_r >= self.policy.breakeven_r:
            action, reasons, explanation = "MOVE_BREAKEVEN", ["BREAKEVEN_ELIGIBLE"], "Configured experimental breakeven threshold reached."
        elif self.policy.trailing_enabled and self.policy.trail_distance is not None and current_r > 0:
            action, reasons, explanation = "TRAIL_STOP", ["TRAIL_ELIGIBLE"], "Configured experimental trailing threshold reached."
        return self._record(position, market, timestamp, action, reasons, explanation,
                            current_r=current_r, excursion_state=exc)

    def _record(self, position, market, timestamp, action, reasons, explanation, current_r=None, excursion_state=None):
        key = idempotency_key(position, action, timestamp)
        duplicate = key in self._emitted
        self._emitted.add(key)
        return {
            "timestamp": timestamp, "strategy_id": position.get("strategy_id"),
            "setup_id": position.get("setup_id"), "economic_position_id": position.get("economic_position_id"),
            "account_context_id": position.get("account_context_id"), "symbol": position.get("symbol"),
            "direction": position.get("direction"), "entry": position.get("entry"),
            "original_stop": position.get("original_stop"), "original_target": position.get("original_target"),
            "current_price": market.get("current_price"), "current_stop": position.get("current_stop", position.get("original_stop")),
            "current_target": position.get("current_target", position.get("original_target")), "position_size": position.get("size"),
            "unrealized_pnl": market.get("unrealized_pnl"), "current_R": current_r,
            "MFE": (excursion_state or {}).get("mfe", position.get("mfe")),
            "MAE": (excursion_state or {}).get("mae", position.get("mae")),
            "MFE_R": (excursion_state or {}).get("mfe_R", position.get("mfe_R")),
            "MAE_R": (excursion_state or {}).get("mae_R", position.get("mae_R")),
            "management_policy": f"{self.policy.policy_id}:{self.policy.version}", "action": action,
            "proposed_price": None, "proposed_size": None, "reason_codes": reasons,
            "explanation": explanation, "market_evidence": market,
            "authorization_mode": self.policy.mode, "advisory_only": True,
            "idempotency_key": key, "duplicate_suppressed": duplicate,
        }


def management_intent(decision: dict[str, Any], *, expires_at: str | None = None) -> dict[str, Any]:
    """Create a broker-free future execution contract from an advisory decision."""
    action_map = {"PROTECT_STOP": "MOVE_STOP", "MOVE_BREAKEVEN": "MOVE_BREAKEVEN",
                  "TRAIL_STOP": "MOVE_STOP", "REDUCE_POSITION": "REDUCE",
                  "CLOSE_POSITION": "CLOSE", "ADD_POSITION": "ADD",
                  "REENTRY_ELIGIBLE": "REENTER"}
    action = action_map.get(decision.get("action"), "HOLD")
    return {"intent_id": sha256((decision["idempotency_key"] + "|intent").encode()).hexdigest(),
            "economic_position_id": decision.get("economic_position_id"),
            "strategy_id": decision.get("strategy_id"), "policy_id": decision.get("management_policy"),
            "action": action, "current_state": decision,
            "requested_state": {"proposed_price": decision.get("proposed_price"), "proposed_size": decision.get("proposed_size")},
            "reason_codes": decision.get("reason_codes", []), "evidence": decision.get("market_evidence", {}),
            "created_at": decision.get("timestamp"), "expires_at": expires_at,
            "idempotency_key": decision.get("idempotency_key"), "authorization_mode": "ADVISORY_SHADOW",
            "portfolio_authorization_required": action in {"ADD", "REENTER"}}
