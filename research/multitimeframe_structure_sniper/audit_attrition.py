"""Representation and gate-attrition audit; descriptive only."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .engine import (build_structure_map, classify_h1_context, classify_h4_context,
                      compatibility, compression_geometry, confirmed_swings, ema_context,
                      psychological_levels, support_resistance_levels)
from .validate_neutral import CONFIG, DATA_PATH, OUT, bars, iso, raw_trigger, replay_symbol, sha


def date_rows(data, date_text):
    return [x for x in data["M5"] if iso(int(x["time"])).startswith(date_text)]


def attrition(symbol, data, date_text):
    m5 = bars(data["M5"], "M5", symbol); m15 = bars(data["M15"], "M15", symbol)
    h1 = bars(data["H1"], "H1", symbol); h4 = bars(data["H4"], "H4", symbol)
    rows, reasons = [], Counter()
    for i, c in enumerate(m5[1:-1], 1):
        if not iso(int(c.timestamp.timestamp())).startswith(date_text):
            continue
        raw = raw_trigger(m5, i)
        if not raw:
            continue
        as_of = c.close_timestamp; expected = "BULLISH" if raw["direction"] == "LONG" else "BEARISH"
        h1m = build_structure_map(h1[-400:], as_of, 2, 2); h4m = build_structure_map(h4[-120:], as_of, 2, 2)
        m15e = [b for b in m15 if b.closed_by(as_of)]
        from .engine import m15_confirmations, compression_geometry, classify_h1_scenario
        confirmations = m15_confirmations(m15e[-80:], as_of, displacement_atr=CONFIG["m15_displacement_atr"])
        m15_confirmation = next((x for x in reversed(confirmations) if x["direction"] == expected), None)
        comp = compression_geometry(h1[-300:], as_of, CONFIG["compression_tolerance_atr"])
        scenario = classify_h1_scenario(h4m, h1m, comp)
        h1_context = classify_h1_context(h1m, comp, scenario=scenario, trade_direction=raw["direction"], m15_confirmation=m15_confirmation)
        h4_context = classify_h4_context(h4m, raw["direction"])
        m15_pass = m15_confirmation is not None; h1_pass = h1m.direction == expected; h4_pass = h4m.direction == expected
        rejection_reason = None if h4_pass and h1_pass and m15_pass else ("M15_NO_SETUP_OR_DIRECTION_MISMATCH" if not m15_pass else "H1_DIRECTION_MISMATCH" if not h1_pass else "H4_DIRECTION_MISMATCH")
        if not m15_pass: reasons["M15_NO_SETUP_OR_DIRECTION_MISMATCH"] += 1
        elif not h1_pass: reasons["H1_DIRECTION_MISMATCH"] += 1
        elif not h4_pass: reasons["H4_DIRECTION_MISMATCH"] += 1
        rows.append({"timestamp": iso(int(c.timestamp.timestamp())), "direction": raw["direction"], "rejection_reason": rejection_reason, "m15_pass": m15_pass,
                     "h1_pass": h1_pass, "h4_pass": h4_pass, "h1_state": h1m.state, "h1_direction": h1m.direction,
                     "h4_state": h4m.state, "h4_direction": h4m.direction, "scenario": scenario.value,
                     "scenario_direction": h1_context.scenario_direction,
                     "h1_context_compatibility": h1_context.compatibility.value,
                     "h4_context_compatibility": h4_context.compatibility.value,
                     "m15_confirmation": m15_confirmation, "compression": comp})
    return {"raw_m5_count": len(rows), "m15_pass_count": sum(x["m15_pass"] for x in rows),
            "h1_pass_count": sum(x["m15_pass"] and x["h1_pass"] for x in rows),
            "h4_pass_count": sum(x["m15_pass"] and x["h1_pass"] and x["h4_pass"] for x in rows),
            "rejection_reasons": dict(reasons), "rows": rows}


def feature_trace(symbol, data, date_text):
    m5 = bars(data["M5"], "M5", symbol); h1 = bars(data["H1"], "H1", symbol); h4 = bars(data["H4"], "H4", symbol); m15 = bars(data["M15"], "M15", symbol)
    selected = [b for b in m5 if iso(int(b.timestamp.timestamp())).startswith(date_text)]
    as_of = selected[-1].close_timestamp if selected else h1[-1].close_timestamp
    def swings(series):
        return [{"timestamp": s.timestamp.isoformat(), "price": s.price, "kind": s.kind, "confirmation_timestamp": (s.timestamp + __import__('datetime').timedelta(minutes={"H1":60,"H4":240}[series[0].timeframe])).isoformat()} for s in confirmed_swings(series, as_of)]
    h1_map = build_structure_map(h1[-400:], as_of); h4_map = build_structure_map(h4[-120:], as_of)
    return {"symbol": symbol, "date": date_text, "as_of": as_of.isoformat(),
            "H4": {"swings": swings(h4[-120:]), "map": {"state": h4_map.state, "direction": h4_map.direction, "bos": h4_map.bos, "choch": h4_map.choch}, "ema": ema_context(h4[-120:], as_of), "levels": support_resistance_levels(h4[-120:], as_of)},
            "H1": {"swings": swings(h1[-400:]), "map": {"state": h1_map.state, "direction": h1_map.direction, "bos": h1_map.bos, "choch": h1_map.choch}, "compression": compression_geometry(h1[-400:], as_of), "levels": support_resistance_levels(h1[-400:], as_of), "ema": ema_context(h1[-400:], as_of), "psychological": psychological_levels(h1[-1].close, .01 if "JPY" in symbol.upper() else .0001)},
            "M15": {"confirmation_candidates": __import__('research.multitimeframe_structure_sniper.engine', fromlist=['m15_confirmations']).m15_confirmations(m15[-500:], as_of, displacement_atr=CONFIG["m15_displacement_atr"])},
            "M5": {"raw_triggers": [{"timestamp": b.timestamp.isoformat(), "direction": raw_trigger(m5, i)["direction"]} for i,b in enumerate(m5) if iso(int(b.timestamp.timestamp())).startswith(date_text) and raw_trigger(m5, i)], "families": ["MICRO_BOS"]}}


def main():
    data = json.loads(DATA_PATH.read_text()); symbol = "USDJPYm"
    att = attrition(symbol, data[symbol], "2026-09-10")
    trace = feature_trace(symbol, data[symbol], "2026-09-10")
    replay = replay_symbol(symbol, data[symbol])
    c = {r["timestamp"] + r["direction"]: r for r in replay["groups"]["C_H1_M15"]}
    d = {r["timestamp"] + r["direction"]: r for r in replay["groups"]["D_H4_H1_M15"]}
    c_fail_d = [r for k,r in c.items() if k not in d]; c_pass_d = [r for k,r in c.items() if k in d]
    def subset_metrics(rows):
        fills = [r for r in rows if r.get("fill")]; net = [r["net_r"] for r in fills if r.get("net_r") is not None]; gross = [r["gross_r"] for r in fills if r.get("gross_r") is not None]
        return {"count": len(rows), "fills": len(fills), "gross_expectancy_R": sum(gross)/len(gross) if gross else None, "net_expectancy_R": sum(net)/len(net) if net else None, "PF": sum(x for x in net if x>0)/abs(sum(x for x in net if x<0)) if any(x<0 for x in net) else None}
    scenario = {k: v for k,v in replay["scenario_metrics"].items()}
    m15 = Counter((r.get("m15_confirmation") or {}).get("type", "UNKNOWN") for r in replay["groups"]["B_M15"])
    compatibility_counts = {"h1": dict(Counter(r["h1_context_compatibility"] for r in att["rows"])), "h4": dict(Counter(r["h4_context_compatibility"] for r in att["rows"]))}
    out = {"config_hash": sha(CONFIG), "source_data": str(DATA_PATH), "USDJPY_2026_09_10_attrition": att, "USDJPY_feature_trace": trace,
           "semantic_separation": {"H4_CONTEXT": "macro context compatibility; not a duplicate BOS/CHOCH trigger",
                                    "H1_CONTEXT": "structure/trend/location/compression, independent of actionable scenario",
                                    "M15_SETUP": "confirmation layer attached to parent H1 context",
                                    "M5_EXECUTION": "entry trigger only", "compatibility_counts": compatibility_counts},
           "first_blocking_gate": "M15_NO_SETUP_OR_DIRECTION_MISMATCH" if att["m15_pass_count"] < att["raw_m5_count"] else "H1_DIRECTION_MISMATCH" if att["h1_pass_count"] < att["m15_pass_count"] else "H4_DIRECTION_MISMATCH",
           "H1_scenario_metrics": scenario, "H4_C_pass_D_fail": subset_metrics(c_fail_d), "H4_C_pass_D_pass": subset_metrics(c_pass_d),
           "M15_confirmation_counts": dict(m15), "M5_family_counts": {"MICRO_BOS": len(replay["ledger"])},
           "GBPUSD_external_data": {"blocked": True, "reason": "no screenshot axis metadata or matching historical period in repository", "required_timeframes": ["H4", "H1", "M15", "M5"], "required_range": "UNRESOLVED_UNTIL_SCREENSHOT_DATE_AXIS_IS_AVAILABLE"},
           "representation_decision": "MIXED_DATA_LIMITATION_AND_NEUTRAL_GATE_ATTRITION", "optimization": False}
    (OUT / "representation_attrition_audit.json").write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(json.dumps({"raw": att["raw_m5_count"], "m15": att["m15_pass_count"], "h1": att["h1_pass_count"], "h4": att["h4_pass_count"], "first_blocking_gate": out["first_blocking_gate"]}, indent=2))


if __name__ == "__main__": main()
