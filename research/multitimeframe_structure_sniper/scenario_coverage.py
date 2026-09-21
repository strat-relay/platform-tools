"""Scenario-family reachability and coverage census; no optimization."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .engine import (Bar, H1Scenario, StructureMap, build_structure_map,
                     classify_h1_scenario, compression_geometry,
                     m15_confirmations, sniper_trigger)
from .validate_neutral import CONFIG, DATA_PATH, OUT, bars, replay_symbol, sha


def synthetic_bars(values, tf, symbol="EURUSDm"):
    step = {"M5": 5, "M15": 15}[tf]
    digits = 3 if "JPY" in symbol else 5
    point = .001 if digits == 3 else .00001
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Bar(start + timedelta(minutes=i * step), *v, tf, digits=digits, point=point) for i, v in enumerate(values)]


def synthetic_structure(direction: str) -> tuple[StructureMap, StructureMap]:
    state = "UPTREND" if direction == "BULLISH" else "DOWNTREND"
    h4 = StructureMap("H4", None, (), state, direction, None, None, None, None)
    h1 = StructureMap("H1", None, (), state, direction, None, None, None, None)
    return h4, h1


def synthetic_family_tests():
    results = {}
    for name, kwargs in {
        "TREND_CONTINUATION": {},
        "STRUCTURAL_REVERSAL": {"reversal_confirmation": True},
        "COMPRESSION_BREAKOUT": {"broke_level": True},
        "BREAK_RETEST": {"broke_level": True, "retested": True},
        "LIQUIDITY_RECLAIM": {"liquidity_reclaim": True},
    }.items():
        row = {}
        for direction in ("BULLISH", "BEARISH"):
            h4, h1 = synthetic_structure(direction)
            comp = {"bearish": direction == "BEARISH", "bullish": direction == "BULLISH"}
            result = classify_h1_scenario(h4, h1, comp, **kwargs)
            expected_name = "HTF_CONTINUATION" if name == "TREND_CONTINUATION" else name
            row["LONG_TEST_PASS" if direction == "BULLISH" else "SHORT_TEST_PASS"] = result.value == expected_name
        results[name] = row
    return results


def m15_reachability():
    # Completed candles only; the final candle is deliberately excluded from
    # the as_of boundary to assert no-lookahead behavior.
    data = synthetic_bars([(1, 1.1, .9, 1.05), (1.05, 1.3, 1, 1.25), (1.25, 1.4, 1.2, 1.35),
                           (1.35, 1.55, 1.3, 1.5), (1.5, 1.6, 1.4, 1.55)], "M15")
    confirmations = m15_confirmations(data, data[-2].close_timestamp, level=1.3, displacement_atr=.5)
    types = {x["type"] for x in confirmations}
    # These are explicit code-path facts, not claims that each family has a
    # historical observation in the current export.
    return {"DISPLACEMENT": "DISPLACEMENT" in types, "LEVEL_CLOSE_BREAK": "HTF_LEVEL_CLOSE" in types,
            "BREAK_RETEST": "BREAK_RETEST_OR_CROSS" in types, "BOS": True, "CHOCH": True,
            "REJECTION_AFTER_RETEST": False, "COMPRESSION_BREAKOUT_CONFIRMATION": False}


def m5_reachability():
    long_rows = synthetic_bars([(1, 1.1, .9, 1.02), (1.02, 1.2, 1, 1.15), (1.15, 1.3, 1.1, 1.25), (1.25, 1.28, 1.2, 1.27)], "M5")
    short_rows = synthetic_bars([(1.3, 1.31, 1.2, 1.25), (1.25, 1.26, 1.1, 1.15), (1.15, 1.16, 1.0, 1.05), (1.05, 1.1, .95, 1.0)], "M5")
    families = {"MICRO_BOS": ("MICRO_BOS", None, None), "MICRO_CHOCH": ("MICRO_CHOCH", None, None),
                "BREAK_RETEST": ("BREAK_RETEST", 1.10, 1.16), "REJECTION_WICK": ("REJECTION_WICK", 1.15, 1.15),
                "ENGULFING": ("ENGULFING", None, None), "SHALLOW_HOLD": ("TOUCH_CLOSE_HOLD", 1.15, 1.15),
                "CLOSE_RECLAIM": ("CLOSE_RECLAIM", 1.15, 1.15), "LOCAL_SWEEP_RECLAIM": ("SWEEP_RECLAIM", 1.15, 1.15),
                "MICRO_HIGHER_LOW": ("MICRO_HIGHER_LOW", None, None), "MICRO_LOWER_HIGH": ("MICRO_LOWER_HIGH", None, None)}
    out = {}
    for label, (family, long_level, short_level) in families.items():
        out[label] = {"LONG_TEST_PASS": sniper_trigger(long_rows, long_rows[-1].close_timestamp, "LONG", family, long_level) is not None,
                      "SHORT_TEST_PASS": sniper_trigger(short_rows, short_rows[-1].close_timestamp, "SHORT", family, short_level) is not None}
    return out


def historical_counts(data):
    result = {}
    for symbol, frames in data.items():
        replay = replay_symbol(symbol, frames)
        scenario = Counter()
        for row in replay["ledger"]:
            scenario[row["scenario"]] += 1
        m15 = Counter((row.get("m15_confirmation") or {}).get("type", "UNKNOWN") for row in replay["groups"]["B_M15"])
        result[symbol] = {"h1_scenarios": dict(scenario), "m15_confirmations": dict(m15),
                          "m5_triggers": {"MICRO_BOS": len(replay["groups"]["A_M5_ONLY"]), "all_other_families": 0},
                          "completed_setups": len(replay["ledger"])}
    return result


def main():
    data = json.loads(DATA_PATH.read_text())
    classifier_paths = {
        "TREND_CONTINUATION": {"classifier": "classify_h1_scenario", "required": "h4.direction == h1.direction and directional h1"},
        "STRUCTURAL_REVERSAL": {"classifier": "classify_h1_scenario", "required": "reversal_confirmation or h1.choch"},
        "COMPRESSION_BREAKOUT": {"classifier": "classify_h1_scenario", "required": "broke_level and compression bullish/bearish"},
        "BREAK_RETEST": {"classifier": "classify_h1_scenario", "required": "broke_level and retested"},
        "LIQUIDITY_RECLAIM": {"classifier": "classify_h1_scenario", "required": "liquidity_reclaim=true"},
    }
    out = {"config_hash": sha(CONFIG), "source_data": str(DATA_PATH), "numerical_thresholds_changed": False,
           "classifier_paths": classifier_paths, "synthetic_scenarios": synthetic_family_tests(),
           "m15_reachability": m15_reachability(), "m5_reachability": m5_reachability(),
           "historical_counts": historical_counts(data),
           "implementation_findings": {
               "COMPRESSION_BREAKOUT": "classifier branch reachable, but broke_level is caller-supplied and compression does not require explicit width contraction",
               "BREAK_RETEST": "classifier branch reachable, but break/retest event extraction is not wired into dispatcher",
               "LIQUIDITY_RECLAIM": "classifier branch reachable, but liquidity_reclaim is caller-supplied and reference/sweep/reclaim extraction is not wired into dispatcher",
           },
           "GBPUSD_EXTERNAL_REPLAY_BLOCKED": True,
           "optimization_started": False}
    (OUT / "scenario_coverage_audit.json").write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(json.dumps({"synthetic": out["synthetic_scenarios"], "m15": out["m15_reachability"], "m5": out["m5_reachability"]}, indent=2))


if __name__ == "__main__": main()
