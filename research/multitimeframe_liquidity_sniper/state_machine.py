"""Causal M15 liquidity/reclaim/displacement/structure state machine."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .engine import TF_SECONDS, _body, _range, _ts, completed_candles, structure_direction


def _atr(rows: list[Mapping[str, Any]], index: int, period: int = 14) -> float:
    sample = rows[max(0, index - period):index]
    values = [_range(x) for x in sample if _range(x) > 0]
    return sum(values) / len(values) if values else _range(rows[index])


def _close_valid(candle: Mapping[str, Any], direction: str, level: float) -> bool:
    return float(candle["close"]) > level if direction == "LONG" else float(candle["close"]) < level


def _adverse_invalidation(candle: Mapping[str, Any], direction: str, sweep_price: float) -> bool:
    return float(candle["close"]) < sweep_price if direction == "LONG" else float(candle["close"]) > sweep_price


def _displacement(candle: Mapping[str, Any], direction: str, atr: float, threshold: float) -> bool:
    body = _body(candle); rng = _range(candle)
    if rng <= 0 or body / max(atr, 1e-12) < threshold:
        return False
    loc = (float(candle["close"]) - float(candle["low"])) / rng
    return (float(candle["close"]) > float(candle["open"]) and loc >= 0.60) if direction == "LONG" else (float(candle["close"]) < float(candle["open"]) and loc <= 0.40)


def _structure_type(rows: list[Mapping[str, Any]], direction: str) -> str:
    prior = structure_direction(rows, lookback=min(len(rows), 5))
    if direction == "LONG":
        return "CHOCH" if prior == "BEARISH_LH_LL" else "BOS"
    return "CHOCH" if prior == "BULLISH_HH_HL" else "BOS"


def detect_m15_setups(candles: list[Mapping[str, Any]], params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return completed M15 setups with explicit sequential lifecycle fields."""
    rows = sorted(candles, key=lambda x: _ts(x["time"]))
    n = int(params.get("liquidity_lookback", 5)); reclaim_max = int(params.get("reclaim_delay", 2))
    disp_max = int(params.get("displacement_window", 5)); bos_max = int(params.get("bos_delay", 2))
    lookback = int(params.get("structure_lookback", 5)); threshold = float(params.get("displacement_body_atr", .50))
    depth_fraction = float(params.get("sweep_depth_atr", .10)); out: list[dict[str, Any]] = []
    for i in range(n, len(rows)):
        prior = rows[i-n:i]; sweep = rows[i]; prior_high = max(float(x["high"]) for x in prior); prior_low = min(float(x["low"]) for x in prior)
        atr = _atr(rows, i)
        candidates = []
        if float(sweep["low"]) < prior_low - depth_fraction * atr:
            candidates.append(("LONG", prior_low, float(sweep["low"])))
        if float(sweep["high"]) > prior_high + depth_fraction * atr:
            candidates.append(("SHORT", prior_high, float(sweep["high"])))
        for direction, reference, sweep_price in candidates:
            trace = [{"state": "WAITING_FOR_LIQUIDITY_EVENT", "timestamp": _ts(sweep["time"])}]
            reclaim_i = next((j for j in range(i, min(len(rows), i + reclaim_max + 1))
                              if _close_valid(rows[j], direction, reference)), None)
            if reclaim_i is None:
                out.append({"setup_id": f"M15-{_ts(sweep['time'])}-{direction}", "direction": direction,
                            "status": "EXPIRED", "terminal_reason": "RECLAIM_TIMEOUT", "trace": trace + [{"state": "EXPIRED"}],
                            "liquidity_reference": reference, "sweep_timestamp": _ts(sweep["time"]), "sweep_price": sweep_price,
                            "reclaim_timestamp": None, "reclaim_delay": None})
                continue
            trace.append({"state": "WAITING_FOR_RECLAIM", "timestamp": _ts(rows[reclaim_i]["time"]), "delay": reclaim_i-i})
            disp_i = next((j for j in range(reclaim_i, min(len(rows), reclaim_i + disp_max + 1))
                           if not _adverse_invalidation(rows[j], direction, sweep_price) and
                           _displacement(rows[j], direction, _atr(rows, j), threshold)), None)
            if disp_i is None:
                out.append({"setup_id": f"M15-{_ts(sweep['time'])}-{direction}", "direction": direction,
                            "status": "INVALIDATED" if any(_adverse_invalidation(rows[j], direction, sweep_price) for j in range(reclaim_i, min(len(rows), reclaim_i + disp_max + 1))) else "EXPIRED",
                            "terminal_reason": "DISPLACEMENT_NOT_CONFIRMED", "trace": trace + [{"state": "WAITING_FOR_DISPLACEMENT"}, {"state": "INVALIDATED"}],
                            "liquidity_reference": reference, "sweep_timestamp": _ts(sweep["time"]), "sweep_price": sweep_price,
                            "reclaim_timestamp": _ts(rows[reclaim_i]["time"]), "reclaim_delay": reclaim_i-i})
                continue
            disp = rows[disp_i]; trace.append({"state": "WAITING_FOR_DISPLACEMENT", "timestamp": _ts(disp["time"])})
            structure_rows = rows[max(0, disp_i-lookback):disp_i]
            if len(structure_rows) < lookback:
                continue
            structure_level = max(float(x["high"]) for x in structure_rows) if direction == "LONG" else min(float(x["low"]) for x in structure_rows)
            structure_type = _structure_type(structure_rows, direction)
            bos_i = next((j for j in range(disp_i, min(len(rows), disp_i + bos_max + 1))
                          if _close_valid(rows[j], direction, structure_level)), None)
            if bos_i is None:
                out.append({"setup_id": f"M15-{_ts(sweep['time'])}-{direction}", "direction": direction,
                            "status": "EXPIRED", "terminal_reason": "STRUCTURE_CONFIRMATION_TIMEOUT",
                            "trace": trace + [{"state": "WAITING_FOR_STRUCTURE_CONFIRMATION"}, {"state": "EXPIRED"}],
                            "liquidity_reference": reference, "sweep_timestamp": _ts(sweep["time"]), "sweep_price": sweep_price,
                            "reclaim_timestamp": _ts(rows[reclaim_i]["time"]), "reclaim_delay": reclaim_i-i,
                            "displacement_timestamp": _ts(disp["time"]), "displacement_body": _body(disp),
                            "displacement_ATR": _body(disp)/max(_atr(rows, disp_i), 1e-12), "structure_level": structure_level,
                            "structure_type": structure_type})
                continue
            bos = rows[bos_i]; trace += [{"state": "WAITING_FOR_STRUCTURE_CONFIRMATION", "timestamp": _ts(bos["time"]), "delay": bos_i-disp_i}, {"state": "SETUP_ACTIVE"}]
            out.append({"setup_id": f"M15-{_ts(sweep['time'])}-{direction}", "direction": direction, "status": "SETUP_ACTIVE", "trace": trace,
                        "liquidity_reference": reference, "sweep_timestamp": _ts(sweep["time"]), "sweep_price": sweep_price,
                        "reclaim_timestamp": _ts(rows[reclaim_i]["time"]), "reclaim_delay": reclaim_i-i,
                        "displacement_timestamp": _ts(disp["time"]), "displacement_body": _body(disp),
                        "displacement_ATR": _body(disp)/max(_atr(rows, disp_i), 1e-12), "structure_level": structure_level,
                        "structure_type": structure_type, "bos_choch_timestamp": _ts(bos["time"]),
                        "confirmation_delay": bos_i-disp_i, "confirmation_index": bos_i, "displacement_index": disp_i,
                        "m15_displacement_high": float(disp["high"]), "m15_displacement_low": float(disp["low"]),
                        "sweep_index": i, "reclaim_index": reclaim_i})
    return out
