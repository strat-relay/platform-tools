"""Explicit M5 entry-family semantics for bounded research replay."""
from __future__ import annotations
from typing import Any, Mapping
from .engine import TF_SECONDS, _ts, pip_size

FAMILIES = ("MICRO_BOS_CHOCH", "SHALLOW_RETRACEMENT", "TOUCH_CLOSE_HOLD", "TOUCH_NEXT_CANDLE_HOLD",
            "CLOSE_RECLAIM", "M5_LIQUIDITY_SWEEP_RECLAIM", "PSYCHOLOGICAL_LEVEL_RECLAIM")

FAMILY_CONTRACTS = {
    "MICRO_BOS_CHOCH": {"required": ("direction", "M5 candles"), "reference": "M5 prior micro swing", "confirmation": "completed M5 close beyond swing", "expiration": "entry window", "stop_anchor": "configured M5 structure"},
    "SHALLOW_RETRACEMENT": {"required": ("direction", "M15 displacement high/low"), "reference": "M15 displacement percentage", "confirmation": "touch only", "expiration": "entry window", "stop_anchor": "configured M5 structure"},
    "TOUCH_CLOSE_HOLD": {"required": ("direction", "M15 displacement high/low"), "reference": "M15 displacement percentage", "confirmation": "touch candle closes on valid side", "expiration": "entry window", "stop_anchor": "configured M5 structure"},
    "TOUCH_NEXT_CANDLE_HOLD": {"required": ("direction", "M15 displacement high/low"), "reference": "M15 displacement percentage", "confirmation": "immediately following closed M5 candle holds", "expiration": "entry window", "stop_anchor": "configured M5 structure"},
    "CLOSE_RECLAIM": {"required": ("direction", "M15 displacement high/low"), "reference": "M15 displacement percentage", "confirmation": "closed candle reclaims after penetration", "expiration": "entry window", "stop_anchor": "configured M5 structure"},
    "M5_LIQUIDITY_SWEEP_RECLAIM": {"required": ("direction", "M5 candles"), "reference": "prior M5 swing", "confirmation": "same candle sweep and reclaim close", "expiration": "entry window", "stop_anchor": "configured M5 structure"},
    "PSYCHOLOGICAL_LEVEL_RECLAIM": {"required": ("direction", "psychological_level"), "reference": "symbol-aware psychological level", "confirmation": "touch and valid-side close", "expiration": "entry window", "stop_anchor": "configured M5 structure"},
}

class UnsupportedResearchFamily(ValueError): pass

def _touch(c, level, direction):
    return float(c["low"]) <= level if direction == "LONG" else float(c["high"]) >= level

def _holds(c, level, direction):
    return float(c["close"]) > level if direction == "LONG" else float(c["close"]) < level

def _level(setup, params):
    family = params["family"]
    if family == "PSYCHOLOGICAL_LEVEL_RECLAIM":
        if setup.get("psychological_level") is None: raise UnsupportedResearchFamily("MISSING_PSYCHOLOGICAL_LEVEL")
        return float(setup["psychological_level"])
    if family in {"SHALLOW_RETRACEMENT", "TOUCH_CLOSE_HOLD", "TOUCH_NEXT_CANDLE_HOLD", "CLOSE_RECLAIM"}:
        if "m15_displacement_high" not in setup or "m15_displacement_low" not in setup:
            raise UnsupportedResearchFamily("MISSING_ENTRY_REFERENCE")
        hi, lo = float(setup["m15_displacement_high"]), float(setup["m15_displacement_low"])
        depth = float(params.get("depth", .15))
        return hi - (hi-lo)*depth if setup["direction"] == "LONG" else lo + (hi-lo)*depth
    return None

def evaluate_m5_entry(symbol: str, m5: list[Mapping[str, Any]], setup: Mapping[str, Any], params: Mapping[str, Any]) -> dict[str, Any] | None:
    family = params["family"]
    if family not in FAMILIES: raise UnsupportedResearchFamily(family)
    direction = setup["direction"]
    confirmation_time = int(setup.get("bos_choch_timestamp", 0))
    confirmation_i = next((i for i, c in enumerate(m5) if _ts(c["time"]) > confirmation_time), int(setup.get("confirmation_index", 0))) - 1
    start = confirmation_i + 1; end = min(len(m5), start + int(params.get("expiration", 5)))
    if family in {"SHALLOW_RETRACEMENT", "TOUCH_CLOSE_HOLD", "TOUCH_NEXT_CANDLE_HOLD", "CLOSE_RECLAIM", "PSYCHOLOGICAL_LEVEL_RECLAIM"}:
        level = _level(setup, params)
        touch_i = None; fill_i = None; confirmation_i2 = None
        for i in range(start, end):
            c = m5[i]
            if not _touch(c, level, direction): continue
            touch_i = i
            if family == "SHALLOW_RETRACEMENT" or family == "PSYCHOLOGICAL_LEVEL_RECLAIM":
                fill_i = i; confirmation_i2 = i; break
            if family == "TOUCH_CLOSE_HOLD" or family == "CLOSE_RECLAIM":
                if _holds(c, level, direction): fill_i = i; confirmation_i2 = i; break
            if family == "TOUCH_NEXT_CANDLE_HOLD":
                if i + 1 < end and _ts(m5[i+1]["time"]) == _ts(c["time"]) + TF_SECONDS["M5"] and _holds(m5[i+1], level, direction):
                    fill_i = i + 1; confirmation_i2 = i + 1; break
                touch_i = None  # this attempt failed; later touch attempts are independent
        if fill_i is None: return None
        return {"family": family, "touch_timestamp": _ts(m5[touch_i]["time"]),
                "confirmation_timestamp": _ts(m5[confirmation_i2]["time"]),
                "confirmation_delay_candles": confirmation_i2-touch_i, "order_created_timestamp": _ts(m5[start]["time"]),
                "activation_timestamp": _ts(m5[touch_i]["time"]), "fill_timestamp": _ts(m5[fill_i]["time"]),
                "fill_price": level, "entry_reference": "PSYCHOLOGICAL_LEVEL" if family.startswith("PSYCHO") else "M15_DISPLACEMENT",
                "entry_level": level, "expiration": int(params.get("expiration", 5)), "stop_anchor": params.get("stop_anchor")}
    if family == "M5_LIQUIDITY_SWEEP_RECLAIM":
        for i in range(start, end):
            if i == 0: continue
            c, p = m5[i], m5[i-1]
            swept = float(c["low"]) < float(p["low"]) and _holds(c, float(p["low"]), direction) if direction == "LONG" else float(c["high"]) > float(p["high"]) and _holds(c, float(p["high"]), direction)
            if swept:
                level = float(p["low"] if direction == "LONG" else p["high"])
                return {"family": family, "touch_timestamp": _ts(c["time"]), "confirmation_timestamp": _ts(c["time"]),
                        "confirmation_delay_candles": 0, "order_created_timestamp": _ts(m5[start]["time"]),
                        "activation_timestamp": _ts(c["time"]), "fill_timestamp": _ts(c["time"]), "fill_price": level,
                        "entry_reference": "M5_LIQUIDITY", "entry_level": level, "expiration": int(params.get("expiration", 5)),
                        "stop_anchor": params.get("stop_anchor")}
        return None
    if family == "MICRO_BOS_CHOCH":
        for i in range(start, end):
            c, p = m5[i], m5[i-1]
            ok = float(c["close"]) > float(p["high"]) if direction == "LONG" else float(c["close"]) < float(p["low"])
            if ok:
                return {"family": family, "touch_timestamp": None, "confirmation_timestamp": _ts(c["time"]),
                        "confirmation_delay_candles": 0, "order_created_timestamp": _ts(c["time"]),
                        "activation_timestamp": _ts(c["time"]), "fill_timestamp": _ts(c["time"]),
                        "fill_price": float(c["close"]), "entry_reference": "M5_MICRO_STRUCTURE",
                        "entry_level": float(c["close"]), "expiration": int(params.get("expiration", 5)),
                        "stop_anchor": params.get("stop_anchor")}
        return None
    raise UnsupportedResearchFamily(family)
