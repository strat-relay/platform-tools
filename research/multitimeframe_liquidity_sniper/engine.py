"""Pure, deterministic research primitives for the sniper-entry hypothesis.

Input candles are mappings containing ``time, open, high, low, close`` and may
also contain ``atr`` and ``spread``.  Candle ``time`` is the candle open time;
only candles whose close time is at or before the decision timestamp are used.
No function in this module contacts MT5 or writes runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable, Mapping

from .config import (CONTROL_GROUPS, DEFAULT_PAIRS, ENTRY_WINDOWS_M5,
                     M15_PARAMETER_GRID, M5_TRIGGER_FAMILIES, MAX_HOLD_MINUTES,
                     STOP_BUFFERS_ATR, STOP_MODELS, TARGET_R)

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}


def _ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def _candle_close(candle: Mapping[str, Any], timeframe: str) -> int:
    return _ts(candle["time"]) + TF_SECONDS[timeframe]


def completed_candles(candles: Iterable[Mapping[str, Any]], timeframe: str,
                     decision_timestamp: Any) -> list[Mapping[str, Any]]:
    """Return only candles fully closed by the decision timestamp."""
    boundary = _ts(decision_timestamp)
    return sorted((c for c in candles if _candle_close(c, timeframe) <= boundary), key=lambda c: _ts(c["time"]))


def align_completed_timeframes(data: Mapping[str, Iterable[Mapping[str, Any]]],
                               decision_timestamp: Any) -> dict[str, Mapping[str, Any] | None]:
    """Align H4/H1/M15/M5 to the latest independently completed candle."""
    return {tf: (rows[-1] if (rows := completed_candles(data.get(tf, ()), tf, decision_timestamp)) else None)
            for tf in ("H4", "H1", "M15", "M5")}


def pip_size(symbol: str) -> float:
    """Return normalized FX pip size, including JPY crosses."""
    return 0.01 if "JPY" in symbol.upper() else 0.0001


def _atr(candle: Mapping[str, Any], fallback: float = 0.0) -> float:
    value = candle.get("atr", fallback)
    return float(value or 0.0)


def _body(c: Mapping[str, Any]) -> float:
    return abs(float(c["close"]) - float(c["open"]))


def _range(c: Mapping[str, Any]) -> float:
    return float(c["high"]) - float(c["low"])


def structure_direction(candles: list[Mapping[str, Any]], lookback: int = 5) -> str:
    """Descriptive HH/HL, LH/LL, or transition/range classification."""
    rows = candles[-max(lookback, 3):]
    if len(rows) < 3:
        return "TRANSITION_RANGE"
    highs = [float(x["high"]) for x in rows]
    lows = [float(x["low"]) for x in rows]
    if highs[-1] > highs[0] and lows[-1] > lows[0]:
        return "BULLISH_HH_HL"
    if highs[-1] < highs[0] and lows[-1] < lows[0]:
        return "BEARISH_LH_LL"
    return "TRANSITION_RANGE"


def context_snapshot(h4: list[Mapping[str, Any]], h1: list[Mapping[str, Any]],
                     decision_timestamp: Any) -> dict[str, Any]:
    aligned = align_completed_timeframes({"H4": h4, "H1": h1}, decision_timestamp)
    h4c = completed_candles(h4, "H4", decision_timestamp)
    h1c = completed_candles(h1, "H1", decision_timestamp)
    return {
        "decision_timestamp": _ts(decision_timestamp),
        "h4_source_candle_timestamp": _ts(aligned["H4"]["time"]) if aligned["H4"] else None,
        "h1_source_candle_timestamp": _ts(aligned["H1"]["time"]) if aligned["H1"] else None,
        "h4_structure": structure_direction(h4c),
        "h1_structure": structure_direction(h1c),
        "h4_location": "RANGE_EXTREME_OR_SWING_CONTEXT" if h4c else "UNKNOWN",
        "h1_location": "RANGE_EXTREME_OR_SWING_CONTEXT" if h1c else "UNKNOWN",
        "ema20_ema50_diagnostic": "RECORDED_ONLY",
    }


def _m15_setup(m15: list[Mapping[str, Any]], decision_timestamp: Any, params: Mapping[str, Any]) -> dict[str, Any] | None:
    rows = completed_candles(m15, "M15", decision_timestamp)
    n = int(params["liquidity_lookback"])
    if len(rows) < n + 2:
        return None
    sweep, reclaim = rows[-2], rows[-1]
    prior = rows[-(n + 2):-2]
    prior_high = max(float(x["high"]) for x in prior)
    prior_low = min(float(x["low"]) for x in prior)
    atr = _atr(reclaim, _range(reclaim))
    depth = float(params["sweep_depth_atr"]) * atr
    direction = None
    if float(sweep["low"]) < prior_low - depth and float(reclaim["close"]) > prior_low:
        direction = "LONG"
    elif float(sweep["high"]) > prior_high + depth and float(reclaim["close"]) < prior_high:
        direction = "SHORT"
    if not direction or _body(reclaim) / max(atr, 1e-12) < float(params["displacement_body_atr"]):
        return None
    return {
        "setup_id": f"M15-{_ts(sweep['time'])}-{direction}",
        "direction": direction,
        "m15_source_candle_timestamp": _ts(reclaim["time"]),
        "sweep_timestamp": _ts(sweep["time"]),
        "atr": atr,
        "liquidity_lookback": n,
        "sweep_depth_atr": float(params["sweep_depth_atr"]),
        "reclaim_delay": 0,
        "displacement_body_atr": _body(reclaim) / max(atr, 1e-12),
        "bos_delay": int(params["bos_delay"]),
        "structure_lookback": int(params["structure_lookback"]),
        "context_family": "UNCLASSIFIED_UNTIL_HTF_CONTEXT",
    }


def _trigger(m5: list[Mapping[str, Any]], setup: Mapping[str, Any], decision_timestamp: Any,
             family: str = "MICRO_STRUCTURE_BREAK") -> dict[str, Any] | None:
    rows = completed_candles(m5, "M5", decision_timestamp)
    if not rows:
        return None
    c = rows[-1]
    direction = setup["direction"]
    previous = rows[-2] if len(rows) > 1 else None
    bullish = float(c["close"]) > float(c["open"])
    if (direction == "LONG") != bullish:
        return None
    if family == "MICRO_STRUCTURE_BREAK":
        if previous is None or ((direction == "LONG" and float(c["close"]) <= float(previous["high"])) or
                                (direction == "SHORT" and float(c["close"]) >= float(previous["low"]))):
            return None
    elif family == "M5_LIQUIDITY_SWEEP_RECLAIM":
        if previous is None or ((direction == "LONG" and not (float(c["low"]) < float(previous["low"]) and float(c["close"]) > float(previous["low"]))) or
                                (direction == "SHORT" and not (float(c["high"]) > float(previous["high"]) and float(c["close"]) < float(previous["high"])) )):
            return None
    elif family == "PSYCHOLOGICAL_LEVEL":
        level = setup.get("psychological_level")
        if level is None or not (float(c["low"]) <= float(level) <= float(c["high"])):
            return None
    elif family in {"SHALLOW_RETRACEMENT", "HOLD_RECLAIM"}:
        level = setup.get("execution_level")
        if level is None or not (float(c["low"]) <= float(level) <= float(c["high"])):
            return None
        if family == "HOLD_RECLAIM" and ((direction == "LONG" and float(c["close"]) <= float(level)) or
                                          (direction == "SHORT" and float(c["close"]) >= float(level))):
            return None
    else:
        raise ValueError(f"unsupported trigger family: {family}")
    entry = float(c["close"])
    impulse = max(_range(c), 1e-12)
    return {
        "trigger_id": f"M5-{_ts(c['time'])}-{direction}",
        "m5_trigger_timestamp": _ts(c["time"]),
        "trigger_family": family,
        "direction": direction,
        "entry": entry,
        "m5_impulse_range": impulse,
        "m5_source_candle_timestamp": _ts(c["time"]),
    }


def next_candle_hold(touch: Mapping[str, Any], confirmation: Mapping[str, Any],
                     direction: str) -> bool:
    """Strictly require confirmation on the immediately following M5 candle."""
    expected = _ts(touch["time"]) + TF_SECONDS["M5"]
    if _ts(confirmation["time"]) != expected:
        return False
    level = float(touch.get("level", touch.get("close")))
    close = float(confirmation["close"])
    return close > level if direction == "LONG" else close < level


def state_machine_trace(symbol: str, setup: Mapping[str, Any] | None,
                        trigger: Mapping[str, Any] | None, *, filled: bool = False,
                        expired: bool = False, invalidated: bool = False) -> list[dict[str, Any]]:
    """Return an explicit lifecycle trace; no trigger means no trade path."""
    rows = [{"state": "WAITING_FOR_HTF_CONTEXT", "symbol": symbol}]
    if not setup:
        rows.append({"state": "NO_TRADE", "reason": "MISSING_M15_SETUP"})
        return rows
    rows.append({"state": "WAITING_FOR_M15_SETUP", "setup_id": setup.get("setup_id")})
    rows.append({"state": "WAITING_FOR_M5_TRIGGER", "setup_id": setup.get("setup_id")})
    if not trigger:
        rows.append({"state": "NO_TRADE", "reason": "M5_TRIGGER_NOT_CONFIRMED"})
        return rows
    rows.append({"state": "ENTRY_PENDING", "trigger_id": trigger.get("trigger_id")})
    terminal = "FILLED" if filled else "INVALIDATED" if invalidated else "EXPIRED" if expired else "ENTRY_PENDING"
    rows.append({"state": terminal, "trigger_id": trigger.get("trigger_id")})
    return rows


def ledger_row(symbol: str, candidate: Mapping[str, Any], *, entry_timestamp: Any = None,
               fill: Mapping[str, Any] | None = None, stop: float | None = None,
               target: float | None = None, net_r: float | None = None,
               population: str = "HTF_CONTINUATION", control_level: str = "D") -> dict[str, Any]:
    """Build the durable replay shape without writing it."""
    setup = candidate.get("setup") or {}
    trigger = candidate.get("trigger") or {}
    return {
        "decision_timestamp": candidate.get("decision_timestamp"), "setup_id": setup.get("setup_id"), "symbol": symbol, "direction": setup.get("direction"),
        "strategy_family": "MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH",
        "h4_source_timestamp": candidate.get("h4_source_candle_timestamp"),
        "h4_context": candidate.get("h4_context"), "h1_source_timestamp": candidate.get("h1_source_candle_timestamp"),
        "h1_context": candidate.get("h1_context"), "m15_setup_timestamp": setup.get("m15_source_candle_timestamp"),
        "m15_sweep": setup.get("sweep_timestamp"), "m15_reclaim": setup.get("reclaim_delay"),
        "m15_displacement": setup.get("displacement_body_atr"), "m15_bos": setup.get("bos_delay"),
        "m5_trigger_timestamp": trigger.get("m5_trigger_timestamp"), "m5_trigger_family": trigger.get("trigger_family"),
        "entry_timestamp": entry_timestamp, "fill_timestamp": fill.get("fill_timestamp") if fill else None,
        "fill_price": fill.get("price") if fill else None, "spread_at_fill": fill.get("spread") if fill else None,
        "stop": stop, "target": target, "gross_R": None, "cost_R": None, "net_R": net_r,
        "exit_reason": None, "control_level": control_level,
        "continuation_or_reversal": population,
        "configuration_hash": None, "source_commit": None,
    }


def evaluate_controls(trigger: Mapping[str, Any], setup: Mapping[str, Any] | None,
                      h1: Mapping[str, Any] | None, h4: Mapping[str, Any] | None) -> dict[str, bool]:
    """Evaluate the identical M5 trigger under controls A, B, C, and D."""
    return {
        "CONTROL_A_M5_ONLY": True,
        "CONTROL_B_M15": setup is not None,
        "CONTROL_C_M15_H1": setup is not None and h1 is not None,
        "CONTROL_D_H4_H1_M15": setup is not None and h1 is not None and h4 is not None,
    }


def inspect_candidate(symbol: str, data: Mapping[str, Iterable[Mapping[str, Any]]],
                      decision_timestamp: Any, m15_params: Mapping[str, Any],
                      trigger_family: str = "MICRO_STRUCTURE_BREAK") -> dict[str, Any]:
    """Build one auditable candidate without using any future candle."""
    aligned = align_completed_timeframes(data, decision_timestamp)
    m15 = _m15_setup(list(data.get("M15", ())), decision_timestamp, m15_params)
    trigger = _trigger(list(data.get("M5", ())), m15, decision_timestamp, trigger_family) if m15 else None
    h1 = aligned["H1"]
    h4 = aligned["H4"]
    controls = evaluate_controls(trigger or {}, m15, h1, h4) if trigger else {x: False for x in CONTROL_GROUPS}
    return {
        "symbol": symbol, "decision_timestamp": _ts(decision_timestamp),
        "h4_source_candle_timestamp": _ts(h4["time"]) if h4 else None,
        "h1_source_candle_timestamp": _ts(h1["time"]) if h1 else None,
        "m15_source_candle_timestamp": m15.get("m15_source_candle_timestamp") if m15 else None,
        "m5_trigger_timestamp": trigger.get("m5_trigger_timestamp") if trigger else None,
        "setup": m15, "trigger": trigger, "controls": controls,
        "status": "CANDIDATE" if trigger else "NO_VALID_M15_SETUP_OR_M5_TRIGGER",
    }


def spread_cost_diagnostic(symbol: str, fill: Mapping[str, Any], stop_price: float,
                           entry_price: float) -> dict[str, Any]:
    """Require spread and fill timestamps to be the same; never fall back."""
    if fill.get("spread_timestamp") != fill.get("fill_timestamp"):
        return {"status": "FILL_COST_UNAVAILABLE", "spread_cost_R": None}
    spread_price = float(fill["ask"]) - float(fill["bid"])
    stop_distance = abs(float(entry_price) - float(stop_price))
    cost_r = spread_price / stop_distance if stop_distance else None
    return {"status": "OK", "spread_timestamp": fill["spread_timestamp"],
            "fill_timestamp": fill["fill_timestamp"], "spread_pips": spread_price / pip_size(symbol),
            "stop_distance_pips": stop_distance / pip_size(symbol), "spread_cost_R": cost_r,
            "bucket": cost_bucket(cost_r)}


def cost_bucket(value: float | None) -> str:
    if value is None:
        return "FILL_COST_UNAVAILABLE"
    for upper, label in ((.05, "<=0.05R"), (.10, "0.05-0.10R"), (.15, "0.10-0.15R"),
                         (.20, "0.15-0.20R"), (.30, "0.20-0.30R")):
        if value <= upper:
            return label
    return ">0.30R"


def chronological_splits(rows: list[Mapping[str, Any]], discovery=.5, selection=.25) -> dict[str, list[Mapping[str, Any]]]:
    ordered = sorted(rows, key=lambda x: _ts(x["decision_timestamp"]))
    n = len(ordered); d = int(n * discovery); s = d + int(n * selection)
    return {"discovery": ordered[:d], "selection": ordered[d:s], "final_untouched_test": ordered[s:]}


def summarize_controls(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate the same-trigger control comparison; outcomes are supplied by caller."""
    result = {}
    for group in CONTROL_GROUPS:
        sample = [r for r in rows if r.get("controls", {}).get(group)]
        rs = [float(r["net_R"]) for r in sample if r.get("net_R") is not None]
        result[group] = {"n": len(sample), "fills": len(rs),
                         "expectancy_R": sum(rs) / len(rs) if rs else None,
                         "profit_factor": (sum(x for x in rs if x > 0) / abs(sum(x for x in rs if x < 0))) if any(x < 0 for x in rs) else None}
    return result
