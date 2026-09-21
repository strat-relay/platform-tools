"""Causal, broker-free market observation for Trade Manager Phase 1.5.

The caller supplies already-read quotes/rates and an observation timestamp.
This module never reads MT5 and never has an execution surface.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from .engine import TradeManager, ema_rejection, position_r
from .semantics import price_semantics

TIMEFRAME_SECONDS = {"M1": 60, "M5": 300}
OBSERVATION_EVENTS = (
    "EMA_APPROACH", "EMA_TOUCH", "EMA_CROSS", "EMA_REJECTION_CANDIDATE",
    "EMA_REJECTION_CONFIRMED", "NEW_MFE", "NEW_MAE", "STRUCTURE_TEST",
    "STRUCTURE_BREAK", "STRUCTURE_RECLAIM", "STOP_APPROACH", "STOP_CROSSED",
    "TARGET_APPROACH", "TARGET_CROSSED", "POSITION_STATE_DISCREPANCY",
)


def _epoch(value: str | int | float | datetime) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def iso(value: str | int | float | datetime) -> str:
    return datetime.fromtimestamp(_epoch(value), timezone.utc).isoformat()


def _bar(row: dict[str, Any], complete: bool, timeframe: str) -> dict[str, Any]:
    return {"time": int(row["time"]), "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "spread": row.get("spread"), "timeframe": timeframe,
            "complete": bool(complete), "source": "SUPPLIED_CAUSAL_RATES"}


def causal_candles(rates: list[dict[str, Any]], timeframe: str, as_of: str | int | float | datetime) -> dict[str, Any]:
    """Split rates at as_of; no candle closing after as_of can be complete."""
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    cutoff = _epoch(as_of)
    duration = TIMEFRAME_SECONDS[timeframe]
    rows = sorted((r for r in rates if float(r["time"]) <= cutoff), key=lambda r: int(r["time"]))
    completed, incomplete = [], None
    for row in rows:
        is_complete = float(row["time"]) + duration <= cutoff
        if is_complete:
            completed.append(_bar(row, True, timeframe))
        else:
            incomplete = _bar(row, False, timeframe)
    return {"timeframe": timeframe, "as_of": iso(as_of), "completed": completed,
            "incomplete": incomplete, "latest": incomplete or (completed[-1] if completed else None)}


def ema_value(candles: list[dict[str, Any]], period: int) -> float | None:
    if len(candles) < period:
        return None
    values = [float(x["close"]) for x in candles]
    alpha = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _confirmed_swings(candles: list[dict[str, Any]], side: str, left: int = 2, right: int = 2) -> list[dict[str, Any]]:
    result = []
    for i in range(left, len(candles) - right):
        window = candles[i - left:i + right + 1]
        value = float(candles[i][side])
        others = [float(x[side]) for j, x in enumerate(window) if j != left]
        if (side == "high" and value > max(others)) or (side == "low" and value < min(others)):
            result.append({"time": candles[i]["time"], "price": value, "kind": "SWING_HIGH" if side == "high" else "SWING_LOW"})
    return result


def structure_snapshot(candles: list[dict[str, Any]]) -> dict[str, Any]:
    highs = _confirmed_swings(candles, "high")
    lows = _confirmed_swings(candles, "low")
    latest_high = highs[-1] if highs else None
    latest_low = lows[-1] if lows else None
    high_label = None
    low_label = None
    if len(highs) >= 2:
        high_label = "HIGHER_HIGH" if highs[-1]["price"] > highs[-2]["price"] else "LOWER_HIGH"
    if len(lows) >= 2:
        low_label = "HIGHER_LOW" if lows[-1]["price"] > lows[-2]["price"] else "LOWER_LOW"
    return {"latest_confirmed_swing_high": latest_high, "latest_confirmed_swing_low": latest_low,
            "latest_high_label": high_label, "latest_low_label": low_label,
            "distance_to_high": None, "distance_to_low": None,
            "structure_break_state": "UNKNOWN", "causal": True,
            "lookback_left": 2, "lookback_right": 2}


def _risk_state(position: dict[str, Any], executable_price: float) -> dict[str, float]:
    entry = float(position["entry"]); stop = float(position["original_stop"])
    current_r = position_r(position["direction"], entry, stop, executable_price)
    favorable = max(0.0, current_r); adverse = max(0.0, -current_r)
    prior_mfe = float(position.get("mfe_R") or 0.0); prior_mae = float(position.get("mae_R") or 0.0)
    return {"current_R": current_r, "mfe_price": max(prior_mfe * abs(entry - stop), favorable * abs(entry - stop)),
            "mae_price": max(prior_mae * abs(entry - stop), adverse * abs(entry - stop)),
            "mfe_R": max(prior_mfe, favorable), "mae_R": max(prior_mae, adverse)}


def build_observation(position: dict[str, Any], quote: dict[str, Any], m1_rates: list[dict[str, Any]],
                      m5_rates: list[dict[str, Any]], as_of: str | int | float | datetime,
                      *, ema_period: int = 200) -> dict[str, Any]:
    bid, ask = float(quote["bid"]), float(quote["ask"])
    direction = str(position["direction"]).upper()
    semantics = price_semantics(direction, bid, ask)
    executable = semantics.close_price
    m1 = causal_candles(m1_rates, "M1", as_of); m5 = causal_candles(m5_rates, "M5", as_of)
    ema200 = ema_value(m5["completed"], ema_period)
    structure = structure_snapshot(m5["completed"])
    if structure.get("latest_confirmed_swing_high"):
        structure["distance_to_high"] = structure["latest_confirmed_swing_high"]["price"] - executable
    if structure.get("latest_confirmed_swing_low"):
        structure["distance_to_low"] = executable - structure["latest_confirmed_swing_low"]["price"]
    risk = _risk_state(position, executable)
    entry_time = _epoch(position.get("entry_time", as_of))
    return {"schema": "trade-manager-causal-observation-v1", "observation_id": None,
            "timestamp": iso(as_of), "economic_position_id": position["economic_position_id"],
            "setup_id": position.get("setup_id"), "strategy_id": position.get("strategy_id"),
            "account_context_id": position.get("account_context_id"), "symbol": position["symbol"],
            "direction": direction, "bid": bid, "ask": ask, "spread": semantics.spread, "mid": semantics.mark_price,
            "mark_price": semantics.mark_price,
            "price_semantics": semantics.as_dict(), "executable_price": executable,
            "entry_executable_price": semantics.entry_price, "close_executable_price": semantics.close_price,
            "entry": position["entry"],
            "current_stop": position.get("current_stop", position["original_stop"]),
            "original_stop": position["original_stop"], "target": position["original_target"],
            "size": position.get("size"), "current_R": risk["current_R"],
            "mfe_price": risk["mfe_price"], "mae_price": risk["mae_price"],
            "mfe_R": risk["mfe_R"], "mae_R": risk["mae_R"],
            "time_in_trade_seconds": max(0.0, _epoch(as_of) - entry_time),
            "market": {"M1": m1, "M5": m5, "M5_EMA": {"period": ema_period, "value": ema200,
                       "source": "COMPLETED_M5_ONLY", "available": ema200 is not None},
                       "structure": structure},
            "causal": True, "future_data_used": False}


def finalize_observation_id(observation: dict[str, Any]) -> str:
    raw = "|".join(str(observation.get(k, "")) for k in
                    ("economic_position_id", "timestamp", "bid", "ask", "m1_time", "m5_time"))
    observation["observation_id"] = sha256(raw.encode()).hexdigest()
    return observation["observation_id"]


def _event(observation: dict[str, Any], event_type: str, reason: str, **evidence: Any) -> dict[str, Any]:
    payload = {"schema": "trade-manager-causal-event-v1", "event_id": None,
               "timestamp": observation["timestamp"], "economic_position_id": observation["economic_position_id"],
               "setup_id": observation.get("setup_id"), "strategy_id": observation.get("strategy_id"),
               "symbol": observation["symbol"], "event": event_type, "reason_code": reason,
               "evidence": evidence, "causal": True, "advisory_only": True}
    raw = json_key(payload)
    payload["event_id"] = sha256(raw.encode()).hexdigest()
    return payload


def json_key(value: Any) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def detect_events(previous: dict[str, Any] | None, observation: dict[str, Any],
                  *, ema_tolerance: float = 0.0, structure_tolerance: float = 0.0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ema200 = observation["market"]["M5_EMA"]["value"]
    price = float(observation["executable_price"])
    if previous:
        if observation["mfe_R"] > previous.get("mfe_R", 0):
            events.append(_event(observation, "NEW_MFE", "NEW_MFE", mfe_R=observation["mfe_R"]))
        if observation["mae_R"] > previous.get("mae_R", 0):
            events.append(_event(observation, "NEW_MAE", "NEW_MAE", mae_R=observation["mae_R"]))
        old_ema = previous.get("market", {}).get("M5_EMA", {}).get("value")
        if ema200 is not None and abs(price - ema200) <= ema_tolerance:
            events.append(_event(observation, "EMA_TOUCH", "EMA_TOUCH", ema=ema200, price=price))
        elif ema200 is not None and ema_tolerance and abs(price - ema200) <= ema_tolerance * 2:
            events.append(_event(observation, "EMA_APPROACH", "EMA_APPROACH", ema=ema200, price=price))
        if old_ema is not None and ema200 is not None and (previous["executable_price"] - old_ema) * (price - ema200) < 0:
            events.append(_event(observation, "EMA_CROSS", "EMA_CROSS", prior_ema=old_ema, ema=ema200))
    m5_latest = observation["market"]["M5"].get("latest")
    if ema200 is not None and m5_latest is not None:
        if not m5_latest["complete"] and float(m5_latest["high"]) >= ema200 >= float(m5_latest["low"]):
            events.append(_event(observation, "EMA_REJECTION_CANDIDATE", "EMA_REJECTION_CANDIDATE", ema=ema200, candle=m5_latest))
        elif m5_latest["complete"] and ema_rejection(m5_latest, ema200, observation["direction"]):
            events.append(_event(observation, "EMA_REJECTION_CONFIRMED", "EMA_REJECTION_CONFIRMED", ema=ema200, candle=m5_latest))
    stop = float(observation["current_stop"]); target = float(observation["target"])
    direction = observation["direction"]
    stop_trigger = float(observation["bid"]) if direction == "LONG" else float(observation["ask"])
    target_trigger = float(observation["bid"]) if direction == "LONG" else float(observation["ask"])
    stop_distance = (stop_trigger - stop) if direction == "LONG" else (stop - stop_trigger)
    target_distance = (target - target_trigger) if direction == "LONG" else (target_trigger - target)
    if stop_distance >= 0 and stop_distance <= abs(float(observation["entry"]) - float(observation["original_stop"])) * .25:
        events.append(_event(observation, "STOP_APPROACH", "STOP_APPROACH", distance=stop_distance))
    if target_distance >= 0 and target_distance <= abs(float(observation["entry"]) - float(observation["original_stop"])) * .25:
        events.append(_event(observation, "TARGET_APPROACH", "TARGET_APPROACH", distance=target_distance))
    stop_crossed = (direction == "LONG" and stop_trigger <= stop) or (direction == "SHORT" and stop_trigger >= stop)
    target_crossed = (direction == "LONG" and target_trigger >= target) or (direction == "SHORT" and target_trigger <= target)
    if stop_crossed:
        events.append(_event(observation, "STOP_CROSSED", "STOP_CROSSED", trigger_price=stop_trigger, persisted_status=observation.get("status"), bid=observation["bid"], ask=observation["ask"]))
    if target_crossed:
        events.append(_event(observation, "TARGET_CROSSED", "TARGET_CROSSED", trigger_price=target_trigger, persisted_status=observation.get("status"), bid=observation["bid"], ask=observation["ask"]))
    if observation.get("status", "OPEN") == "OPEN" and (stop_crossed or target_crossed):
        events.append(_event(observation, "POSITION_STATE_DISCREPANCY", "POSITION_STATE_DISCREPANCY",
                             expected_state="STOPPED" if stop_crossed else "TARGET_HIT",
                             persisted_state="OPEN", trigger_price=stop_trigger if stop_crossed else target_trigger,
                             bid=observation["bid"], ask=observation["ask"]))
    structure = observation["market"].get("structure", {})
    if structure_tolerance > 0:
        for name, level in (("high", structure.get("latest_confirmed_swing_high")),
                            ("low", structure.get("latest_confirmed_swing_low"))):
            if level and abs(price - float(level["price"])) <= structure_tolerance:
                events.append(_event(observation, "STRUCTURE_TEST", "STRUCTURE_TEST", level=level, price=price))
            if previous and level:
                old_price = float(previous["executable_price"]); level_price = float(level["price"])
                if (old_price - level_price) * (price - level_price) < 0:
                    events.append(_event(observation, "STRUCTURE_BREAK", "STRUCTURE_BREAK", level=level, price=price))
                    events.append(_event(observation, "STRUCTURE_RECLAIM", "STRUCTURE_RECLAIM", level=level, price=price))
    return events


@dataclass
class CausalObserver:
    manager: TradeManager = field(default_factory=TradeManager)
    observations: dict[str, dict[str, Any]] = field(default_factory=dict)
    emitted_events: set[str] = field(default_factory=set)

    def observe(self, position: dict[str, Any], quote: dict[str, Any], m1_rates: list[dict[str, Any]],
                m5_rates: list[dict[str, Any]], as_of: str | int | float | datetime,
                *, ema_tolerance: float = 0.0, structure_tolerance: float = 0.0) -> dict[str, Any]:
        observation = build_observation(position, quote, m1_rates, m5_rates, as_of)
        observation["status"] = position.get("status", "OPEN")
        observation["market"]["M1"]["latest"] = observation["market"]["M1"].get("latest")
        observation["m1_time"] = (observation["market"]["M1"]["latest"] or {}).get("time")
        observation["m5_time"] = (observation["market"]["M5"]["latest"] or {}).get("time")
        finalize_observation_id(observation)
        pid = position["economic_position_id"]
        previous = self.observations.get(pid)
        duplicate = bool(previous and previous.get("observation_id") == observation["observation_id"])
        events = [] if duplicate else detect_events(previous, observation, ema_tolerance=ema_tolerance,
                                                    structure_tolerance=structure_tolerance)
        events = [x for x in events if x["event_id"] not in self.emitted_events]
        for event in events:
            self.emitted_events.add(event["event_id"])
        self.observations[pid] = observation
        decision_market = {**observation, "timestamp": observation["timestamp"],
                           "current_price": observation["executable_price"],
                           "ema_200": observation["market"]["M5_EMA"]["value"],
                           "ema_tolerance": ema_tolerance}
        decision = self.manager.evaluate(position, decision_market, timestamp=observation["timestamp"])
        return {"observation": observation, "events": events, "decision": decision,
                "duplicate_observation": duplicate}
