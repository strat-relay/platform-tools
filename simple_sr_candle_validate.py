from __future__ import annotations

import bisect, csv, hashlib, json, os, statistics, subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from paper_engine import _candle, atr

ROOT = Path(__file__).resolve().parent
WINE = "/Applications/MetaTrader 5.app/Contents/SharedSupport/wine/bin/wine"
PREFIX = "/Users/caleb/Library/Application Support/net.metaquotes.wine.metatrader5"
WINPY = r"C:\Python39\python.exe"
TARGETS = (1.0, 1.25, 1.5, 2.0)
MODE = os.environ.get("SIMPLE_SR_PATTERN_MODE", "ALL")
SIGNAL_TIMEFRAME = "M15" if MODE in ("M15_ENGULFING_ONLY", "M15_ENGULFING_CONFIRMATION", "M15_ENGULFING_CONFIRMATION_AVOID_SR", "M15_ENGULFING_AVOID_SR", "M15_ENGULFING_SR_BOTH") else "M5"
BAR_MINUTES = 15 if SIGNAL_TIMEFRAME == "M15" else 5
HOLD_BARS = 120 // BAR_MINUTES
OUTPUT_PREFIX = "simple_sr_candle_m15_engulfing_sr_both" if MODE == "M15_ENGULFING_SR_BOTH" else "simple_sr_candle_m15_engulfing_avoid_sr" if MODE == "M15_ENGULFING_AVOID_SR" else "simple_sr_candle_m15_engulfing_confirmed_avoid_sr" if MODE == "M15_ENGULFING_CONFIRMATION_AVOID_SR" else "simple_sr_candle_m15_engulfing_confirmed" if MODE == "M15_ENGULFING_CONFIRMATION" else "simple_sr_candle_m15_engulfing" if MODE == "M15_ENGULFING_ONLY" else "simple_sr_candle_engulfing" if MODE == "ENGULFING_ONLY" else "simple_sr_candle_stars" if MODE == "STARS_ONLY" else "simple_sr_candle"
CONFIG = {
    "symbol": "XAUUSDm", "execution": "M5", "context": "M15", "lookback_swing": 2, "level_lookback_m15_bars": 480,
    "cluster_tolerance_atr": 0.25, "support_tolerance_atr": 0.25,
    "morning_small_body_atr": 0.50, "morning_small_vs_first_body": 0.50,
    "strong_body_atr": 0.75, "close_location_min": 0.60,
    "atr_buffer_fraction": 0.10, "spread_buffer_multiple": 1.25,
    "max_hold_minutes": 120, "paper_long_only": True,
}


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def pct(n, d):
    return 100.0 * n / d if d else 0.0


def session(ts):
    h = datetime.fromtimestamp(int(ts), timezone.utc).hour
    if h < 8: return "ASIA"
    if h < 13: return "LONDON"
    if h < 17: return "LONDON_NEW_YORK_OVERLAP"
    if h < 22: return "NEW_YORK"
    return "OTHER"


def fetch():
    env = dict(os.environ, WINEPREFIX=PREFIX)
    p = subprocess.run([WINE, WINPY, "Z:" + str(ROOT / "historical_fetch_symbol.py")], env=env, text=True, capture_output=True, timeout=180, check=True)
    return json.loads(p.stdout.splitlines()[-1])


def swing_points(bars, lookback):
    out = []
    for i in range(lookback, len(bars) - lookback):
        hi = _candle(bars[i], "high"); lo = _candle(bars[i], "low")
        if hi == max(_candle(x, "high") for x in bars[i - lookback:i + lookback + 1]): out.append(("RESISTANCE", hi, int(bars[i]["time"])))
        if lo == min(_candle(x, "low") for x in bars[i - lookback:i + lookback + 1]): out.append(("SUPPORT", lo, int(bars[i]["time"])))
    return out


def levels_before(m15, signal_time, confirmed_points=None):
    prior = m15 if (not m15 or int(m15[-1]["time"]) < signal_time) else [x for x in m15 if int(x["time"]) < signal_time]
    if len(prior) < 10: return []
    m15_atr = atr(prior)[-1] or 0.0
    tolerance = max(m15_atr * CONFIG["cluster_tolerance_atr"], 1e-9)
    window_start = int(prior[-min(len(prior), CONFIG["level_lookback_m15_bars"])] ["time"]) if prior else signal_time
    points = [p for p in (confirmed_points if confirmed_points is not None else swing_points(prior, CONFIG["lookback_swing"])) if window_start <= p[2] + CONFIG["lookback_swing"] * 15 * 60 < signal_time]
    levels = []
    for kind in ("SUPPORT", "RESISTANCE"):
        for _, price, ts in sorted((p for p in points if p[0] == kind), key=lambda x: x[1]):
            match = next((z for z in levels if z["type"] == kind and abs(price - z["center_price"]) <= tolerance), None)
            if match:
                match["prices"].append(price); match["timestamps"].append(ts); match["center_price"] = sum(match["prices"]) / len(match["prices"])
                match["zone_low"] = min(match["prices"]); match["zone_high"] = max(match["prices"]); match["last_touch_timestamp"] = max(match["timestamps"]); match["touches"] = len(match["prices"])
            else:
                levels.append({"level_id": f"{kind}-{ts}-{price:.5f}", "type": kind, "center_price": price, "zone_low": price, "zone_high": price, "creation_timestamp": iso(ts + 15 * 60), "last_touch_timestamp": iso(ts + 15 * 60), "touches": 1, "prices": [price], "timestamps": [ts], "strength_score": 1.0})
    for level in levels:
        level["age_minutes"] = max(0, (signal_time - max(level["timestamps"])) / 60)
        level.pop("prices", None); level.pop("timestamps", None)
    return levels


def candle_pattern(m5, i):
    if i < 2: return []
    c0, c1, c2 = m5[i - 2], m5[i - 1], m5[i]
    o0, h0, l0, cl0 = map(lambda k: _candle(c0, k), ("open", "high", "low", "close"))
    o1, h1, l1, cl1 = map(lambda k: _candle(c1, k), ("open", "high", "low", "close"))
    o2, h2, l2, cl2 = map(lambda k: _candle(c2, k), ("open", "high", "low", "close"))
    a = atr(m5[max(0, i - 40):i + 1])[-1] or 0.0
    b0, b1, b2 = abs(cl0 - o0), abs(cl1 - o1), abs(cl2 - o2)
    out = []
    engulf = cl1 < o1 and cl2 > o2 and o2 <= cl1 and cl2 >= o1
    if engulf: out.append(("BULLISH_ENGULFING", l1, l2, {"previous_body": b1, "current_body": b2, "current_wick_high": h2, "current_wick_low": l2}))
    bearish_engulf = cl1 > o1 and cl2 < o2 and o2 >= cl1 and cl2 <= o1
    if bearish_engulf: out.append(("BEARISH_ENGULFING", h1, h2, {"previous_body": b1, "current_body": b2, "current_wick_high": h2, "current_wick_low": l2}))
    morning = cl0 < o0 and b1 <= min(a * CONFIG["morning_small_body_atr"], b0 * CONFIG["morning_small_vs_first_body"]) and cl2 > o2 and b2 >= a * CONFIG["strong_body_atr"] and ((cl2 - l2) / (h2 - l2) if h2 > l2 else 0) >= CONFIG["close_location_min"] and cl2 >= (o0 + cl0) / 2
    if morning: out.append(("MORNING_STAR", min(l0, l1, l2), l2, {"first_body": b0, "small_body": b1, "third_body": b2, "third_close_location": (cl2 - l2) / (h2 - l2) if h2 > l2 else 0}))
    evening = cl0 > o0 and b1 <= min(a * CONFIG["morning_small_body_atr"], b0 * CONFIG["morning_small_vs_first_body"]) and cl2 < o2 and b2 >= a * CONFIG["strong_body_atr"] and ((h2 - cl2) / (h2 - l2) if h2 > l2 else 0) >= CONFIG["close_location_min"] and cl2 <= (o0 + cl0) / 2
    if evening: out.append(("EVENING_STAR_INFO", max(h0, h1, h2), h2, {"first_body": b0, "small_body": b1, "third_body": b2, "third_close_location": (h2 - cl2) / (h2 - l2) if h2 > l2 else 0}))
    return out


def support_match(levels, signal_bar, signal_time, atr_m5):
    tolerance = max(atr_m5 * CONFIG["support_tolerance_atr"], 1e-9)
    low = _candle(signal_bar, "low"); high = _candle(signal_bar, "high")
    candidates = [z for z in levels if z["type"] == "SUPPORT" and low <= z["zone_high"] + tolerance and high >= z["zone_low"] - tolerance]
    if not candidates: return None
    return min(candidates, key=lambda z: abs(low - z["center_price"]))


def sr_match(levels, signal_bar, atr_m5):
    """Return any established S/R zone touched or approached by the pattern bar."""
    tolerance = max(atr_m5 * CONFIG["support_tolerance_atr"], 1e-9)
    low = _candle(signal_bar, "low"); high = _candle(signal_bar, "high")
    candidates = [z for z in levels if low <= z["zone_high"] + tolerance and high >= z["zone_low"] - tolerance]
    return min(candidates, key=lambda z: abs(((low + high) / 2) - z["center_price"])) if candidates else None


def nearest_resistance(levels, entry):
    above = [z for z in levels if z["type"] == "RESISTANCE" and z["center_price"] > entry]
    return min(above, key=lambda z: z["center_price"]) if above else None


def nearest_support(levels, entry):
    below = [z for z in levels if z["type"] == "SUPPORT" and z["center_price"] < entry]
    return max(below, key=lambda z: z["center_price"]) if below else None


def settle(row, bars, fill_i, target):
    entry = float(row["entry"]); stop = float(row["stop_loss"]); risk = abs(entry - stop); direction = row.get("direction", "LONG"); sign = 1 if direction == "LONG" else -1; tp = entry + sign * risk * target
    mfe = mae = 0.0; exit_price = None; reason = "INVALID_AFTER_SIGNAL"; duration = 0
    for step in range(HOLD_BARS + 1):
        j = fill_i + step
        if j >= len(bars): break
        bar = bars[j]; high = _candle(bar, "high"); low = _candle(bar, "low"); duration = step * BAR_MINUTES
        if direction == "LONG": mfe = max(mfe, high - entry); mae = max(mae, entry - low)
        else: mfe = max(mfe, entry - low); mae = max(mae, high - entry)
        hit_sl = low <= stop if direction == "LONG" else high >= stop
        hit_tp = high >= tp if direction == "LONG" else low <= tp
        if step >= HOLD_BARS: exit_price, reason = _candle(bar, "close"), "TIME_EXIT"; break
        if hit_sl or hit_tp: exit_price, reason = (stop, "STOPPED") if hit_sl else (tp, "TARGET_HIT"); break
    if exit_price is None: return {"outcome": reason, "r": None, "duration_minutes": duration, "mae_r": mae / risk, "mfe_r": mfe / risk, "mae_price": mae, "mfe_price": mfe, "exit_price": None, "exit_reason": reason}
    return {"outcome": "LOSS" if reason == "STOPPED" else "WIN" if reason == "TARGET_HIT" else "TIME_EXIT", "r": sign * (exit_price - entry) / risk, "duration_minutes": duration, "mae_r": mae / risk, "mfe_r": mfe / risk, "mae_price": mae, "mfe_price": mfe, "exit_price": exit_price, "exit_reason": reason}


def metrics(rows, target_key="r"):
    resolved = [x for x in rows if x.get(target_key) is not None]; rs = [float(x[target_key]) for x in resolved]; wins = [r for r in rs if r > 0]; losses = [r for r in rs if r < 0]; eq = peak = dd = 0.0; streak = longest = 0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq); streak = streak + 1 if r < 0 else 0; longest = max(longest, streak)
    return {"signals": len(rows), "trades": len(resolved), "wins": len(wins), "losses": len(losses), "time_exits": sum(x.get("outcome") == "TIME_EXIT" for x in resolved), "win_rate_pct": pct(len(wins), len(wins) + len(losses)), "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "expectancy_r": sum(rs) / len(rs) if rs else 0.0, "cumulative_r": sum(rs), "max_drawdown_r": dd, "longest_losing_streak": longest, "median_duration_minutes": statistics.median([x["duration_minutes"] for x in resolved]) if resolved else 0.0, "median_mae_r": statistics.median([x["mae_r"] for x in resolved]) if resolved else 0.0, "median_mfe_r": statistics.median([x["mfe_r"] for x in resolved]) if resolved else 0.0}


def main():
    data = fetch(); all_m5 = data["M5"]; m15 = data["M15"]; signal_source = m15 if SIGNAL_TIMEFRAME == "M15" else all_m5; end = max(int(x["time"]) for x in signal_source[:-1]); start = end - 184 * 86400; signal_bars = [x for x in signal_source if start <= int(x["time"]) < end]; m15_times = [int(x["time"]) for x in m15]; contract = data["contract"]; confirmed_points = swing_points(m15, CONFIG["lookback_swing"]); level_cache = {}; setups = []; patterns_anywhere = []; evening_info = []
    confirmation_offset = 2 if MODE in ("M15_ENGULFING_CONFIRMATION", "M15_ENGULFING_CONFIRMATION_AVOID_SR") else 1
    for i in range(40, len(signal_bars) - HOLD_BARS - confirmation_offset):
        signal_time = int(signal_bars[i]["time"]); patterns = candle_pattern(signal_bars, i)
        if MODE == "ENGULFING_ONLY": patterns = [p for p in patterns if p[0] == "BULLISH_ENGULFING"]
        elif MODE == "STARS_ONLY": patterns = [p for p in patterns if p[0] in ("MORNING_STAR", "EVENING_STAR_INFO")]
        elif MODE in ("M15_ENGULFING_ONLY", "M15_ENGULFING_CONFIRMATION", "M15_ENGULFING_CONFIRMATION_AVOID_SR", "M15_ENGULFING_AVOID_SR"): patterns = [p for p in patterns if p[0] == "BULLISH_ENGULFING"]
        elif MODE == "M15_ENGULFING_SR_BOTH": patterns = [p for p in patterns if p[0] in ("BULLISH_ENGULFING", "BEARISH_ENGULFING")]
        if not patterns: continue
        confirmation_bar = signal_bars[i + 1]
        confirmation_passed = True
        if MODE in ("M15_ENGULFING_CONFIRMATION", "M15_ENGULFING_CONFIRMATION_AVOID_SR"):
            confirmation_passed = _candle(confirmation_bar, "close") > _candle(signal_bars[i], "high") and _candle(confirmation_bar, "close") > _candle(confirmation_bar, "open")
            if not confirmation_passed: continue
        m15_index = bisect.bisect_left(m15_times, signal_time)
        if SIGNAL_TIMEFRAME == "M15": m15_index = max(0, m15_index - 1)
        if m15_index not in level_cache: level_cache[m15_index] = levels_before(m15[max(0, m15_index - CONFIG["level_lookback_m15_bars"]):m15_index], signal_time, confirmed_points)
        levels = level_cache[m15_index]; atr_m5 = atr(signal_bars[max(0, i - 40):i + 1])[-1] or 0.0; spread_price = float(signal_bars[i].get("spread", 0)) * float(contract["point"])
        for pattern, pattern_low, _, details in patterns:
            if pattern == "EVENING_STAR_INFO":
                evening_info.append({"timestamp": iso(signal_time + 300), "pattern_type": pattern, "levels": levels, "details": details}); continue
            entry_index = i + confirmation_offset; entry_bar = signal_bars[entry_index]; support = support_match(levels, signal_bars[i], signal_time, atr_m5); signal_sr = sr_match(levels, signal_bars[i], atr_m5); is_long = pattern == "BULLISH_ENGULFING"
            if MODE == "M15_ENGULFING_SR_BOTH" and (not signal_sr or signal_sr["type"] != ("SUPPORT" if is_long else "RESISTANCE")): continue
            if MODE in ("M15_ENGULFING_CONFIRMATION_AVOID_SR", "M15_ENGULFING_AVOID_SR") and signal_sr: continue
            entry = _candle(entry_bar, "open") + (spread_price / 2 if is_long else -spread_price / 2); resistance = nearest_resistance(levels, entry); below_support = nearest_support(levels, entry); structural_level = signal_sr if MODE == "M15_ENGULFING_SR_BOTH" else support; buffer = max(atr_m5 * CONFIG["atr_buffer_fraction"], spread_price * CONFIG["spread_buffer_multiple"], float(contract["tick_size"])); stop = (min(pattern_low, structural_level["zone_low"]) - buffer if is_long and structural_level else pattern_low - buffer if is_long else max(pattern_low, structural_level["zone_high"]) + buffer if structural_level else pattern_low + buffer); risk = (entry - stop) if is_long else (stop - entry)
            if risk <= 0: continue
            row = {"setup_id": f"SSCV1-{signal_time}-{pattern}", "timestamp": iso(signal_time + BAR_MINUTES * 60), "timestamp_epoch": signal_time + BAR_MINUTES * 60, "month": datetime.fromtimestamp(signal_time, timezone.utc).strftime("%Y-%m"), "session": session(signal_time + BAR_MINUTES * 60), "pattern_type": pattern, "direction": "LONG" if is_long else "SHORT", "support_passed": bool(support), "signal_sr_type": signal_sr["type"] if signal_sr else None, "signal_sr_level": signal_sr["center_price"] if signal_sr else None, "final_status": "VALID_SR_REVERSAL", "confirmation_required": MODE in ("M15_ENGULFING_CONFIRMATION", "M15_ENGULFING_CONFIRMATION_AVOID_SR"), "confirmation_passed": confirmation_passed, "confirmation_level": _candle(signal_bars[i], "high") if MODE in ("M15_ENGULFING_CONFIRMATION", "M15_ENGULFING_CONFIRMATION_AVOID_SR") else None, "confirmation_close": _candle(confirmation_bar, "close") if MODE in ("M15_ENGULFING_CONFIRMATION", "M15_ENGULFING_CONFIRMATION_AVOID_SR") else None, "entry": entry, "stop_loss": stop, "stop_distance": risk, "stop_source": "PATTERN_EXTREME_SR_ZONE", "support_level_id": structural_level["level_id"] if structural_level and structural_level["type"] == "SUPPORT" else None, "support_center": structural_level["center_price"] if structural_level and structural_level["type"] == "SUPPORT" else None, "support_zone_low": structural_level["zone_low"] if structural_level and structural_level["type"] == "SUPPORT" else None, "support_zone_high": structural_level["zone_high"] if structural_level and structural_level["type"] == "SUPPORT" else None, "support_prior_touches": structural_level["touches"] if structural_level and structural_level["type"] == "SUPPORT" else 0, "support_distance": abs(_candle(signal_bars[i], "low") - structural_level["center_price"]) if is_long and structural_level else None, "nearest_resistance_level_id": resistance["level_id"] if resistance else None, "nearest_resistance": resistance["center_price"] if resistance else None, "distance_to_resistance": resistance["center_price"] - entry if resistance else None, "available_r_to_resistance": (resistance["center_price"] - entry) / risk if is_long and resistance else None, "spread": spread_price, "atr": atr_m5, "pattern_details": json.dumps(details, sort_keys=True), "tick_size": contract["tick_size"], "tick_value": contract["tick_value"], "contract_size": contract["contract_size"], "broker_min_lot": contract["min_lot"], "broker_lot_step": contract["lot_step"], "risk_at_001": (risk / contract["tick_size"]) * contract["tick_value"] * contract["min_lot"], "fill_timestamp": iso(int(entry_bar["time"])), "fill_index": entry_index, "signal_index": i, "signal_to_fill_candles": confirmation_offset}
            for target in TARGETS:
                result = settle(row, signal_bars, entry_index, target); key = str(target).replace(".", "_"); row[f"outcome_{key}R"] = result["outcome"]; row[f"r_{key}R"] = result["r"]; row[f"duration_{key}R_minutes"] = result["duration_minutes"]; row[f"mae_{key}R"] = result["mae_r"]; row[f"mfe_{key}R"] = result["mfe_r"]; row[f"exit_reason_{key}R"] = result["exit_reason"]; row[f"next_resistance_reached_{key}R"] = bool(resistance and any(_candle(b, "high") >= resistance["center_price"] for b in signal_bars[entry_index:entry_index + HOLD_BARS]))
            row.update({"outcome": row["outcome_1_25R"], "r": row["r_1_25R"], "duration_minutes": row["duration_1_25R_minutes"], "mae_r": row["mae_1_25R"], "mfe_r": row["mfe_1_25R"], "exit_reason": row["exit_reason_1_25R"]}); setups.append(row); patterns_anywhere.append(row)
    months = sorted(set(x["month"] for x in setups)); split_months = months[:max(1, int(len(months) * 2 / 3))]; validation_months = months[len(split_months):]; target_rows = {str(t): [{k: v for k, v in row.items() if not k.startswith(("outcome_", "r_", "duration_", "mae_", "mfe_", "exit_reason_", "next_resistance_reached_"))} | {"outcome": row[f"outcome_{str(t).replace('.', '_')}R"], "r": row[f"r_{str(t).replace('.', '_')}R"], "duration_minutes": row[f"duration_{str(t).replace('.', '_')}R_minutes"], "mae_r": row[f"mae_{str(t).replace('.', '_')}R"], "mfe_r": row[f"mfe_{str(t).replace('.', '_')}R"]} for row in setups] for t in TARGETS}
    support_sets = {"BULLISH_ENGULFING_ANYWHERE": [x for x in setups if x["pattern_type"] == "BULLISH_ENGULFING"], "BULLISH_ENGULFING_NEAR_SUPPORT": [x for x in setups if x["pattern_type"] == "BULLISH_ENGULFING" and x["support_passed"]], "MORNING_STAR_ANYWHERE": [x for x in setups if x["pattern_type"] == "MORNING_STAR"], "MORNING_STAR_NEAR_SUPPORT": [x for x in setups if x["pattern_type"] == "MORNING_STAR" and x["support_passed"]], "ALL_BULLISH_ANYWHERE": setups, "ALL_BULLISH_NEAR_SUPPORT": [x for x in setups if x["support_passed"]]}
    room = {bucket: [x for x in setups if (x["available_r_to_resistance"] is not None and ((bucket == "<1R" and x["available_r_to_resistance"] < 1) or (bucket == "1-1.5R" and 1 <= x["available_r_to_resistance"] < 1.5) or (bucket == "1.5-2R" and 1.5 <= x["available_r_to_resistance"] < 2) or (bucket == ">2R" and x["available_r_to_resistance"] >= 2)))] for bucket in ("<1R", "1-1.5R", "1.5-2R", ">2R")}
    analysis_base = setups if MODE in ("M15_ENGULFING_CONFIRMATION_AVOID_SR", "M15_ENGULFING_AVOID_SR", "M15_ENGULFING_SR_BOTH") else [x for x in setups if x["support_passed"]]
    levels_summary = []
    for row in setups:
        if row["support_level_id"]: levels_summary.append({"level_id": row["support_level_id"], "type": "SUPPORT", "center_price": row["support_center"], "zone_high": row["support_zone_high"], "zone_low": row["support_zone_low"], "creation_timestamp": None, "touches": row["support_prior_touches"]})
    unique_levels = {x["level_id"]: x for x in levels_summary}
    balances = {}
    for bal in (100, 150, 250, 500, 750, 1000): balances[str(bal)] = {str(r): {"count": sum(x["risk_at_001"] <= bal * r / 100 for x in analysis_base), "pct": pct(sum(x["risk_at_001"] <= bal * r / 100 for x in analysis_base), len(analysis_base))} for r in (0.5, 1, 2)}
    valid = analysis_base; risks = sorted(x["risk_at_001"] for x in valid)
    lds = json.load(open(ROOT / "liquidity_displacement_results.json"))["target_results"]["1.25"]
    lds_rows = list(csv.DictReader(open(ROOT / "liquidity_displacement_trades.csv")))
    lds["median_stop_distance"] = statistics.median(float(x["stop_distance"]) for x in lds_rows)
    lds["median_min_lot_risk_dollars"] = statistics.median(float(x["min_lot_risk_dollars"]) for x in lds_rows)
    support_target_125 = [x for x in target_rows["1.25"] if (MODE in ("M15_ENGULFING_CONFIRMATION_AVOID_SR", "M15_ENGULFING_AVOID_SR", "M15_ENGULFING_SR_BOTH") or x["support_passed"])]
    support_validation = [x for x in support_target_125 if x["month"] in validation_months]
    strategy_name = "SIMPLE_SR_CANDLE_V1_M15_ENGULFING_SR_BOTH" if MODE == "M15_ENGULFING_SR_BOTH" else "SIMPLE_SR_CANDLE_V1_M15_ENGULFING_AVOID_SR" if MODE == "M15_ENGULFING_AVOID_SR" else "SIMPLE_SR_CANDLE_V1_M15_ENGULFING_CONFIRMED_AVOID_SR" if MODE == "M15_ENGULFING_CONFIRMATION_AVOID_SR" else "SIMPLE_SR_CANDLE_V1_M15_ENGULFING_CONFIRMED" if MODE == "M15_ENGULFING_CONFIRMATION" else "SIMPLE_SR_CANDLE_V1_M15_ENGULFING_ONLY" if MODE == "M15_ENGULFING_ONLY" else "SIMPLE_SR_CANDLE_V1_ENGULFING_ONLY" if MODE == "ENGULFING_ONLY" else "SIMPLE_SR_CANDLE_V1_STARS_ONLY" if MODE == "STARS_ONLY" else "SIMPLE_SR_CANDLE_V1"
    out = {"strategy": strategy_name, "pattern_mode": MODE, "source_sha256": hashlib.sha256((ROOT / "simple_sr_candle_validate.py").read_bytes()).hexdigest(), "paper_only": True, "rules": CONFIG, "period": {"start": iso(start), "split_months": split_months, "validation_months": validation_months, "end": iso(end)}, "level_count": len(unique_levels), "evening_star_info_count": len(evening_info), "pattern_counts": {k: len(v) for k, v in support_sets.items()}, "target_results": {str(t): metrics(target_rows[str(t)]) for t in TARGETS}, "support_comparison_1.25R": {k: metrics(v) for k, v in support_sets.items()}, "resistance_room_1.25R": {k: metrics(v) for k, v in room.items()}, "monthly_1.25R": {m: metrics([x for x in support_target_125 if x["month"] == m]) for m in months}, "discovery_validation_1.25R": {"discovery": metrics([x for x in support_target_125 if x["month"] in split_months]), "validation": metrics(support_validation)}, "small_account_feasibility": {"qualifying_support_setups": len(valid), "risk_at_001": {"minimum": min(risks) if risks else 0, "p25": statistics.quantiles(risks, n=4, method="inclusive")[0] if len(risks) > 1 else (risks[0] if risks else 0), "median": statistics.median(risks) if risks else 0, "p75": statistics.quantiles(risks, n=4, method="inclusive")[2] if len(risks) > 1 else (risks[0] if risks else 0)}, "curve": balances}, "comparison": {strategy_name: metrics(support_target_125), "LIQUIDITY_DISPLACEMENT_SCALP_V1": lds}, "artifacts": {"levels": list(unique_levels.values()), "evening_stars": evening_info}, "decision": "B" if metrics(support_validation)["expectancy_r"] > 0 else "D"}
    with open(ROOT / f"{OUTPUT_PREFIX}_results.json", "w", encoding="utf-8") as f: json.dump(out, f, indent=2)
    fields = sorted({k for row in setups for k in row});
    with open(ROOT / f"{OUTPUT_PREFIX}_trades.csv", "w", newline="", encoding="utf-8") as f: w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(setups)
    summary = ["# SIMPLE_SR_CANDLE_V1 historical validation", "", f"READ-ONLY / PAPER-ONLY. LONG only. M15 horizontal levels, {SIGNAL_TIMEFRAME} completed-candle patterns, no M1, no live orders.", "", "## Frozen rules", json.dumps(CONFIG, indent=2), "", "## Target results", json.dumps(out["target_results"], indent=2), "", "## Support comparison", json.dumps(out["support_comparison_1.25R"], indent=2), "", "## Resistance room", json.dumps(out["resistance_room_1.25R"], indent=2), "", "## Monthly stability", json.dumps(out["monthly_1.25R"], indent=2), "", "## Chronological validation", json.dumps(out["discovery_validation_1.25R"], indent=2), "", "## Small-account feasibility", json.dumps(out["small_account_feasibility"], indent=2), "", "## Comparison", json.dumps(out["comparison"], indent=2), "", f"## Classification\n{out['decision']} — rules were frozen before the untouched validation period."]
    (ROOT / f"{OUTPUT_PREFIX}_summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(json.dumps({"decision": out["decision"], "patterns": out["pattern_counts"], "target_results": out["target_results"], "validation": out["discovery_validation_1.25R"]["validation"]}, indent=2))


if __name__ == "__main__": main()
