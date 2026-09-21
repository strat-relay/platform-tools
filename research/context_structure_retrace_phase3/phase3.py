"""CONTEXT_STRUCTURE_RETRACE_V1 Phase 3 historical candidate research.

This module describes hypothetical setup lifecycles and outcomes.  It does
not submit orders, create runner state, or alter any existing strategy.
Features/qualification are built causally; future paths are attached only as
separate outcome labels.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import statistics
from bisect import bisect_left
from concurrent.futures import ProcessPoolExecutor
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from context_structure_retrace.attention import attention_layer
from context_structure_retrace.config import ResearchTimeframes
from context_structure_retrace.data import CausalReplay, bar_end, iso
from context_structure_retrace.indicators import atr, ema_context
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace.replay import feature_snapshot

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path("/tmp/context-structure-retrace")
DATA_FILES = {"XAUUSDm": "xau.json", "BTCUSDm": "btc_basic.json", "USDJPYm": "USDJPYm.json", "EURUSDm": "EURUSDm.json"}
START = int(datetime(2026, 3, 15, tzinfo=timezone.utc).timestamp())


@dataclass(frozen=True)
class Phase3Config:
    timeframes: ResearchTimeframes = ResearchTimeframes(execution="M15", lower=("M5",), higher=("H1", "H4"))
    retracement_depths: tuple[float, ...] = (0.20, 0.50)
    max_retrace_lower_candles: int = 12
    max_hold_minutes: tuple[int, ...] = (30, 60, 90, 120, 180)
    departure_atr: float = 2.0
    target_r: float = 1.25
    spread_multiplier: float = 1.0


def phase2_hash() -> str:
    names = ("attention.py", "config.py", "data.py", "indicators.py", "normalization.py", "patterns.py", "replay.py", "schema.py", "sr.py", "structures.py")
    h = hashlib.sha256()
    for name in names:
        path = Path(__file__).parents[2] / "context_structure_retrace" / name
        h.update(name.encode()); h.update(path.read_bytes())
    return h.hexdigest()


def _direction(bar: dict[str, Any]) -> str:
    o, c = float(bar["open"]), float(bar["close"])
    return "UP" if c > o else "DOWN" if c < o else "DOJI"


def _spread(bar: dict[str, Any], contract: dict[str, Any]) -> float:
    return float(bar.get("spread", 0)) * float(contract.get("point", contract.get("tick_size", 0.00001)))


def _pattern_events(prefix: list[dict[str, Any]], tf: str, symbol: str, as_of: int) -> list[dict[str, Any]]:
    """Detect only from the completed prefix ending at the event bar."""
    if len(prefix) < 3:
        return []
    return detect_patterns(prefix, tf, as_of, symbol, completed_override=prefix)


def _causal_window(replay: CausalReplay, as_of: int, times_by_tf: dict[str, list[int]] | None = None) -> CausalReplay:
    """Bound the Phase 2 replay input without changing its causal rules."""
    # Phase 3 records the Phase 2 algorithms but uses an explicit bounded
    # causal research window so the historical ledger remains tractable.
    limits = {"M5": 120, "M15": 60, "H1": 40, "H4": 24, "M1": 600}
    selected = {}
    for tf, bars in replay.bars_by_timeframe.items():
        times = (times_by_tf or {}).get(tf) or [int(x["time"]) for x in bars]
        end = bisect_left(times, int(as_of))
        selected[tf] = bars[max(0, end - limits.get(tf, 250)):end]
    return CausalReplay(selected)


def _closest_context(snapshot: dict[str, Any], direction: str) -> dict[str, Any]:
    tf = snapshot["provenance"]["structure_timeframe"]
    structure = snapshot["timeframes"].get(tf, {})
    attention = attention_layer(snapshot, limit=3)
    boundaries = attention["boundaries"]
    ema = structure.get("ema_context", {})
    values = ema.get("ema_values_completed", {})
    price = float((structure.get("completed_candle") or {}).get("close", 0))
    above = sum(1 for v in values.values() if v is not None and price > float(v))
    below = sum(1 for v in values.values() if v is not None and price < float(v))
    htf = {}
    for name in ("H1", "H4"):
        item = snapshot["timeframes"].get(name, {})
        htf[name] = {"completed_direction": item.get("completed_direction"), "forming_direction": item.get("forming_direction"), "ema_ordering": item.get("ema_context", {}).get("ordering")}
    opposing_room = boundaries.get("distance_to_resistance_atr") if direction == "LONG" else boundaries.get("distance_to_support_atr")
    supporting_room = boundaries.get("distance_to_support_atr") if direction == "LONG" else boundaries.get("distance_to_resistance_atr")
    contradictions = [name for name, item in htf.items() if item.get("completed_direction") and item.get("completed_direction") != ("UP" if direction == "LONG" else "DOWN")]
    flags = []
    if opposing_room is not None and opposing_room < 1.0: flags.append("NEAR_OPPOSING_STRUCTURE_FLAG")
    if contradictions: flags.append("HTF_CONTRADICTION_FLAG")
    if (direction == "LONG" and below >= 3) or (direction == "SHORT" and above >= 3): flags.append("EMA_CONTEXT_CONFLICT_FLAG")
    return {
        "direction": direction, "structure_timeframe": tf, "nearest_support": boundaries.get("below"), "nearest_resistance": boundaries.get("above"),
        "distance_to_support_atr": boundaries.get("distance_to_support_atr"), "distance_to_resistance_atr": boundaries.get("distance_to_resistance_atr"),
        "room_opposing_atr": opposing_room, "room_supporting_atr": supporting_room, "ema_above_count": above, "ema_below_count": below,
        "ema_ordering": ema.get("ordering"), "ema_slopes": ema.get("slopes"), "ema_separation": ema.get("pair_separation"),
        "htf": htf, "qualification_flags": flags, "trendlines": attention.get("trendlines", {}).get("top", []), "channels": attention.get("channels", {}).get("top", []),
        "attention_summary": {"raw_sr": attention["sr"]["raw_count"], "raw_trendlines": attention["trendlines"]["raw_count"], "raw_channels": attention["channels"]["raw_count"]},
    }


def _stop_hypotheses(event_bar, direction, snapshot, atr_value, spread):
    lo, hi = float(event_bar["low"]), float(event_bar["high"])
    structure = snapshot["timeframes"][snapshot["provenance"]["structure_timeframe"]]
    boundary = snapshot["provenance"]["structure_timeframe"]
    zones = structure.get("sr_context", {}).get("zones", [])
    support = [z for z in zones if z.get("support_resistance_role") == "SUPPORT" and float(z["midpoint"]) <= float(event_bar["close"])]
    resistance = [z for z in zones if z.get("support_resistance_role") == "RESISTANCE" and float(z["midpoint"]) >= float(event_bar["close"])]
    if direction == "LONG":
        structural = min([lo] + [float(z["zone_low"]) for z in support], default=lo)
        refs = {"ORIGINATING_SETUP_EXTREME": lo, "PATTERN_EXTREME": lo, "LOCAL_STRUCTURAL_SWING": structural, "SUPPORTING_BOUNDARY": structural}
    else:
        structural = max([hi] + [float(z["zone_high"]) for z in resistance], default=hi)
        refs = {"ORIGINATING_SETUP_EXTREME": hi, "PATTERN_EXTREME": hi, "LOCAL_STRUCTURAL_SWING": structural, "SUPPORTING_BOUNDARY": structural}
    buffer = max(float(atr_value or 0) * 0.10, spread * 1.25)
    result = {}
    for name, extreme in refs.items():
        stop = extreme - buffer if direction == "LONG" else extreme + buffer
        entry = float(event_bar["close"]) + spread / 2 if direction == "LONG" else float(event_bar["close"]) - spread / 2
        risk = entry - stop if direction == "LONG" else stop - entry
        result[name] = {"structural_reference": extreme, "stop": stop, "stop_distance": risk, "stop_distance_atr": risk / atr_value if atr_value else None, "spread_adjusted_distance": risk + spread, "entry_reference": entry}
    return result


def _target_hypotheses(event_bar, direction, snapshot, entry):
    lo, hi = float(event_bar["low"]), float(event_bar["high"]); rng = hi - lo
    structure = snapshot["timeframes"][snapshot["provenance"]["structure_timeframe"]]
    zones = structure.get("sr_context", {}).get("zones", [])
    if direction == "LONG":
        opposing = sorted(float(z["zone_low"]) for z in zones if z.get("support_resistance_role") == "RESISTANCE" and float(z["midpoint"]) > entry)
        extension = hi + 0.50 * rng; next_structure = opposing[0] if opposing else None
        capped = min(extension, next_structure) if next_structure is not None else extension
    else:
        opposing = sorted((float(z["zone_high"]) for z in zones if z.get("support_resistance_role") == "SUPPORT" and float(z["midpoint"]) < entry), reverse=True)
        extension = lo - 0.50 * rng; next_structure = opposing[0] if opposing else None
        capped = max(extension, next_structure) if next_structure is not None else extension
    return {"CANDLE_EXTENSION_50": extension, "NEXT_OPPOSING_STRUCTURE": next_structure, "STRUCTURE_CAPPED_EXTENSION": capped}


def _assign_opportunity_ids(attempt_candidates, event_id, symbol, direction, entry, lower, atr_value, departure_atr):
    """Keep hover attempts together; split only after an explicit departure."""
    prior_index = None; opportunity_number = 1; current_id = hashlib.sha256(f"{symbol}|{event_id}|{opportunity_number}".encode()).hexdigest()[:20]
    for candidate in sorted(attempt_candidates, key=lambda x: x["lower_index"]):
        if prior_index is not None and any((float(x["high"]) - entry if direction == "LONG" else entry - float(x["low"])) >= (atr_value or 0.0) * departure_atr for x in lower[prior_index + 1:candidate["lower_index"]]):
            opportunity_number += 1
            current_id = hashlib.sha256(f"{symbol}|{event_id}|{opportunity_number}".encode()).hexdigest()[:20]
        candidate["entry_opportunity_id"] = current_id
        candidate["entry_opportunity_number"] = opportunity_number
        candidate["potential_scale_in"] = opportunity_number > 1
        prior_index = candidate["lower_index"]
    return attempt_candidates


def _entry_signals(lower, setup_bar, direction, depths, start_index, max_candles, contract, symbol="SYMBOL"):
    """Create observational entry hypotheses only after the setup bar."""
    entry = float(setup_bar["close"]); rng = max(float(setup_bar["high"]) - float(setup_bar["low"]), 1e-12)
    interactions = {}

    def register(index, signal, level, spread):
        item = interactions.setdefault(index, {"lower_index": index, "signal_types": [], "variants": [], "timestamp": bar_end(lower[index], "M5")})
        if signal not in item["signal_types"]:
            item["signal_types"].append(signal)
        item["variants"].append({"signal_type": signal, "entry_reference": level, "entry_price": level + spread / 2 if direction == "LONG" else level - spread / 2, "spread": spread})

    end = min(len(lower), start_index + max_candles)
    for j in range(start_index, end):
        bar = lower[j]
        low, high, close = float(bar["low"]), float(bar["high"]), float(bar["close"])
        if direction == "LONG" and low <= float(setup_bar["low"]):
            break
        if direction == "SHORT" and high >= float(setup_bar["high"]):
            break
        candidates = []
        for depth in depths:
            level = entry - rng * depth if direction == "LONG" else entry + rng * depth
            touched = low <= level <= high
            held = close >= level if direction == "LONG" else close <= level
            if touched and held: candidates.append((f"DEPTH_ONLY_{depth:.2f}", level))
        body = abs(float(bar["close"]) - float(bar["open"])); upper = high - max(float(bar["open"]), float(bar["close"])); lower_wick = min(float(bar["open"]), float(bar["close"])) - low
        if direction == "LONG" and lower_wick >= max(body * 2, 1e-12) and (close - low) / max(high-low, 1e-12) >= .65: candidates.append(("REJECTION_WICK", close))
        if direction == "SHORT" and upper >= max(body * 2, 1e-12) and (close-low) / max(high-low, 1e-12) <= .35: candidates.append(("REJECTION_WICK", close))
        # Pattern detection only needs the trailing ATR window and the last
        # three completed bars; passing the full history here would make the
        # causal replay quadratic without changing the result.
        lower_tail = lower[max(0, j - 14):j + 1]
        lower_events = detect_patterns(lower_tail, "M5", bar_end(bar, "M5"), symbol, completed_override=lower_tail)
        wanted = "BULLISH_ENGULFING" if direction == "LONG" else "BEARISH_ENGULFING"
        stars = "MORNING_STAR" if direction == "LONG" else "EVENING_STAR"
        if any(e["pattern"] == wanted for e in lower_events): candidates.append(("LOWER_TF_ENGULFING", close))
        if any(e["pattern"] == stars for e in lower_events): candidates.append(("MORNING_EVENING_STAR", close))
        if candidates:
            spread = _spread(bar, contract)
            for signal, level in candidates:
                register(j, signal, level, spread)
    out = []
    for item in interactions.values():
        representative = item["variants"][0]
        out.append({**item, **representative, "candles_waited": item["lower_index"] - start_index})
    return out


def _outcome(lower, candidate, stop, target, direction, max_hold=36, m1=None):
    fill = int(candidate["lower_index"]); entry = float(candidate["entry_price"]); risk = abs(entry-stop); exit_i = None; reason = "TIME_EXIT"
    if risk <= 0: return {"outcome": "INVALID_GEOMETRY", "r": None}
    resolution = "M1_SEQUENCE" if m1 else "M5_CONSERVATIVE_STOP_PRIORITY"
    for j in range(fill, min(len(lower), fill + max_hold)):
        bar = lower[j]; hit_stop = float(bar["low"]) <= stop if direction == "LONG" else float(bar["high"]) >= stop; hit_target = float(bar["high"]) >= target if direction == "LONG" else float(bar["low"]) <= target
        if hit_stop and hit_target and m1:
            intrabars = m1.get(int(bar["time"]), []) if isinstance(m1, dict) else [x for x in m1 if int(bar["time"]) <= int(x["time"]) < int(bar["time"]) + 300]
            for ib in intrabars:
                ib_stop = float(ib["low"]) <= stop if direction == "LONG" else float(ib["high"]) >= stop
                ib_target = float(ib["high"]) >= target if direction == "LONG" else float(ib["low"]) <= target
                if ib_stop or ib_target:
                    hit_stop, hit_target = ib_stop, ib_target
                    break
        if hit_stop: exit_i, reason = j, "STOPPED"; break
        if hit_target: exit_i, reason = j, "TARGET_HIT"; break
    if exit_i is None: exit_i = min(len(lower)-1, fill + max_hold - 1)
    exit_bar = lower[exit_i]; close = float(exit_bar["close"]); target_r = abs(target - entry) / risk
    r = ((close-entry)/risk if direction == "LONG" else (entry-close)/risk) if reason == "TIME_EXIT" else (target_r if reason == "TARGET_HIT" else -1.0)
    path = lower[fill:exit_i+1]; mfe = max((float(x["high"])-entry if direction == "LONG" else entry-float(x["low"])) for x in path); mae = max((entry-float(x["low"]) if direction == "LONG" else float(x["high"])-entry) for x in path)
    return {"outcome": reason, "r": r, "exit_index": exit_i, "exit_timestamp": int(exit_bar["time"]), "duration_minutes": (int(exit_bar["time"])-int(lower[fill]["time"])) / 60, "mfe": max(0.0,mfe), "mae": max(0.0,mae), "mfe_r": max(0.0,mfe)/risk, "mae_r": max(0.0,mae)/risk, "intrabar_resolution": resolution}


def replay_symbol(symbol: str, path: Path | None = None, config: Phase3Config = Phase3Config()) -> dict[str, Any]:
    data = json.loads((path or (DATA_DIR / DATA_FILES[symbol])).read_text())
    bars = {tf: data[tf] for tf in config.timeframes.all if tf in data}
    replay = CausalReplay(bars)
    execution = replay.bars_by_timeframe[config.timeframes.execution]; lower = replay.bars_by_timeframe[config.timeframes.lower[0]]
    times_by_tf = {tf: [int(x["time"]) for x in series] for tf, series in replay.bars_by_timeframe.items()}
    lower_times = times_by_tf[config.timeframes.lower[0]]
    contract = data.get("contract", {}); m1 = defaultdict(list)
    for item in data.get("M1", []): m1[(int(item["time"]) // 300) * 300].append(item)
    m1_arg = dict(m1) or None
    rows = []; sequence = 0
    snapshot_cache = {}
    for i, bar in enumerate(execution):
        as_of = bar_end(bar, config.timeframes.execution)
        if as_of < START or i < 3: continue
        execution_tail = execution[max(0, i - 14):i + 1]
        events = _pattern_events(execution_tail, config.timeframes.execution, symbol, as_of)
        if not events: continue
        snapshot = snapshot_cache.get(as_of)
        if snapshot is None:
            snapshot = feature_snapshot(_causal_window(replay, as_of, times_by_tf), symbol, as_of, contract=contract, timeframes=config.timeframes, window_limits={"M5": 120, "M15": 60, "H1": 40, "H4": 24}, structure_max_points=4)
            snapshot_cache[as_of] = snapshot
        structure = snapshot["timeframes"][config.timeframes.execution]; atr_value = structure["ema_context"].get("atr") or 0.0
        lower_start = bisect_left(lower_times, int(as_of))
        for event in events:
            direction = event["direction"]; ctx = _closest_context(snapshot, direction); sequence += 1
            lower_candidates = _entry_signals(lower, bar, direction, config.retracement_depths, lower_start, config.max_retrace_lower_candles, contract, symbol)
            invalidation_index = next((lower_start + 1 + k for k, x in enumerate(lower[lower_start+1:lower_start+1+config.max_retrace_lower_candles]) if (float(x["low"]) <= float(bar["low"]) if direction == "LONG" else float(x["high"]) >= float(bar["high"]))), None)
            invalidated = invalidation_index is not None
            qualification = "QUALIFIED_FOR_RETRACE_MONITORING" if not invalidated else "SETUP_INVALIDATED_BEFORE_ENTRY"
            rejection = "SETUP_INVALIDATED_BEFORE_ENTRY" if invalidated else None
            if not invalidated and not lower_candidates: rejection = "NO_RETRACE"
            stops = _stop_hypotheses(bar, direction, snapshot, atr_value, _spread(bar, contract)); entry_candidates = lower_candidates if not invalidated else []
            targets = _target_hypotheses(bar, direction, snapshot, float(bar["close"]))
            entry_candidates = _assign_opportunity_ids(entry_candidates, event["event_id"], symbol, direction, float(bar["close"]), lower, atr_value, config.departure_atr)
            opportunity_id = entry_candidates[0]["entry_opportunity_id"] if entry_candidates else hashlib.sha256(f"{symbol}|{event['event_id']}|1".encode()).hexdigest()[:20]
            attempts = []
            for candidate in entry_candidates:
                stop_results = {}
                for stop_name, stop in stops.items():
                    target_results = {}
                    for target_name, target in targets.items():
                        if target is None: target_results[target_name] = {"target": None, "outcome": "NO_TARGET"}; continue
                        target_results[target_name] = {"target": target, "rr": abs(target-candidate["entry_price"]) / max(abs(candidate["entry_price"]-stop["stop"]), 1e-12), "outcome": _outcome(lower, candidate, stop["stop"], target, direction, m1=m1_arg)}
                    stop_results[stop_name] = {"geometry": stop, "targets": target_results}
                attempts.append({"entry_attempt_id": hashlib.sha256(f"{candidate['entry_opportunity_id']}|{candidate['lower_index']}|{candidate['signal_type']}".encode()).hexdigest()[:20], "entry_opportunity_id": candidate["entry_opportunity_id"], "candidate": candidate, "stop_target_outcomes": stop_results, "two_position_model": {"status": "MODEL_ONLY", "position_a": "FIRST_TARGET_HYPOTHESIS", "position_b": "BREAKEVEN_AFTER_POSITION_A", "runner_exit_policy": "NOT_SELECTED"}, "runner_outcome": "MODEL_ONLY_NOT_SELECTED"})
            row = {"setup_event": event, "setup_id": event["event_id"], "market_event_id": hashlib.sha256(f"{symbol}|{event['timestamp']}".encode()).hexdigest()[:20], "context_snapshot": snapshot, "context_components": ctx, "qualification": qualification, "qualification_flags": ctx["qualification_flags"], "rejection_reason": rejection, "retrace_state": "WAITING_FOR_RETRACEMENT" if qualification.startswith("QUALIFIED") else "NOT_STARTED", "entry_signals": sorted({signal for x in entry_candidates for signal in x["signal_types"]}), "entry_opportunity_id": opportunity_id if attempts else None, "entry_opportunity_ids": sorted({a["entry_opportunity_id"] for a in attempts}), "entry_attempts": attempts, "stop_hypotheses": stops, "target_hypotheses": targets, "spread": _spread(bar, contract), "pattern_context": event["measurements"], "invalidation": {"candidate_extreme": float(bar["low"] if direction == "LONG" else bar["high"]), "timestamp": int(lower[invalidation_index]["time"]) if invalidation_index is not None else None, "candles_after_setup": invalidation_index - lower_start if invalidation_index is not None else None}, "ledger_provenance": {"phase2_representation_hash": phase2_hash(), "as_of_timestamp": as_of, "features_causal": True, "outcome_labels_separate": True, "development_only": True, "validation_status": "EXPOSED_HISTORICAL_PERIOD"}}
            rows.append(row)
    return {"symbol": symbol, "config": {"execution_timeframe": config.timeframes.execution, "lower": config.timeframes.lower, "higher": config.timeframes.higher, "retracement_depths": config.retracement_depths, "max_retrace_lower_candles": config.max_retrace_lower_candles, "departure_atr": config.departure_atr, "targets": ["CANDLE_EXTENSION_50", "NEXT_OPPOSING_STRUCTURE", "STRUCTURE_CAPPED_EXTENSION"], "causal_context_window": {"M5": 120, "M15": 60, "H1": 40, "H4": 24, "M1": 600}, "phase2_structure_max_points": 4}, "phase2_representation_hash": phase2_hash(), "rows": rows}


def summarize(run: dict[str, Any]) -> dict[str, Any]:
    rows = run["rows"]; attempts = [a for r in rows for a in r["entry_attempts"]]; outcomes = [a["candidate"] for a in attempts]
    def combo_metrics(selected, stop_name="ORIGINATING_SETUP_EXTREME", target_name="CANDLE_EXTENSION_50"):
        result = []
        for attempt in selected:
            outcome = attempt["stop_target_outcomes"].get(stop_name, {}).get("targets", {}).get(target_name, {}).get("outcome", {})
            if isinstance(outcome, dict) and outcome.get("r") is not None: result.append(outcome)
        rs = [float(x["r"]) for x in result]; wins = [x for x in rs if x > 0]; losses = [x for x in rs if x < 0]
        return {"n": len(rs), "wins": len(wins), "losses": len(losses), "win_rate_pct": 100 * len(wins) / len(rs) if rs else None, "profit_factor": sum(wins) / abs(sum(losses)) if losses else None, "expectancy_r": sum(rs) / len(rs) if rs else None, "cumulative_r": sum(rs), "avg_mae_r": statistics.mean(x["mae_r"] for x in result) if result else None, "avg_mfe_r": statistics.mean(x["mfe_r"] for x in result) if result else None, "median_duration_minutes": statistics.median(x["duration_minutes"] for x in result) if result else None, "time_to_target_count": sum(x["outcome"] == "TARGET_HIT" for x in result)}
    by_pattern = {}; by_signal = {}
    for pattern in sorted({r["setup_event"]["pattern"] for r in rows}):
        by_pattern[pattern] = combo_metrics([a for r in rows if r["setup_event"]["pattern"] == pattern for a in r["entry_attempts"]])
    for signal in sorted({s for r in rows for s in r["entry_signals"]}):
        by_signal[signal] = combo_metrics([a for r in rows if signal in r["entry_signals"] for a in r["entry_attempts"]])
    combos = {}
    for stop_name in ("ORIGINATING_SETUP_EXTREME", "PATTERN_EXTREME", "LOCAL_STRUCTURAL_SWING", "SUPPORTING_BOUNDARY"):
        for target_name in ("CANDLE_EXTENSION_50", "NEXT_OPPOSING_STRUCTURE", "STRUCTURE_CAPPED_EXTENSION"):
            combos[f"{stop_name}|{target_name}"] = combo_metrics(attempts, stop_name, target_name)
    base = [a["stop_target_outcomes"]["ORIGINATING_SETUP_EXTREME"]["targets"]["CANDLE_EXTENSION_50"]["outcome"] for a in attempts]
    durations = [x["duration_minutes"] for x in base if x.get("duration_minutes") is not None]
    def percentile(values, q):
        values = sorted(values)
        return values[min(len(values)-1, int(len(values) * q))] if values else None
    invalidation_candles = [r["invalidation"]["candles_after_setup"] for r in rows if r["invalidation"].get("candles_after_setup") is not None]
    mfe = [x["mfe_r"] for x in base if x.get("mfe_r") is not None]; mae = [x["mae_r"] for x in base if x.get("mae_r") is not None]
    retrace_wait = [a["candidate"]["candles_waited"] for a in attempts]
    opportunity_ids = {a["entry_opportunity_id"] for a in attempts}
    return {"setup_events": len(rows), "qualified": sum(r["qualification"] == "QUALIFIED_FOR_RETRACE_MONITORING" for r in rows), "qualification_rejected": sum(r["qualification"] != "QUALIFIED_FOR_RETRACE_MONITORING" for r in rows), "no_retrace": sum(r["rejection_reason"] == "NO_RETRACE" for r in rows), "rejected": sum(r["qualification"] != "QUALIFIED_FOR_RETRACE_MONITORING" for r in rows), "invalidated": sum(r["rejection_reason"] == "SETUP_INVALIDATED_BEFORE_ENTRY" for r in rows), "entered": len(attempts), "unique_entry_opportunities": len(opportunity_ids), "reentry_opportunities": sum(a.get("candidate", {}).get("potential_scale_in", False) for a in attempts), "potential_scale_ins": sum(a.get("candidate", {}).get("potential_scale_in", False) for a in attempts), "entry_signal_types": dict(Counter(signal for r in rows for signal in r["entry_signals"])), "patterns": dict(Counter(r["setup_event"]["pattern"] for r in rows)), "attempt_outcomes": dict(Counter(x.get("outcome") for x in base)), "illustrative_baseline": combo_metrics(attempts), "outcomes_by_pattern": by_pattern, "outcomes_by_entry_signal": by_signal, "all_stop_target_combinations": combos, "duration_distribution_minutes": {"min": min(durations) if durations else None, "median": statistics.median(durations) if durations else None, "p90": percentile(durations, .9), "max": max(durations) if durations else None}, "mfe_r_distribution": {"p10": percentile(mfe, .1), "p50": percentile(mfe, .5), "p90": percentile(mfe, .9)}, "mae_r_distribution": {"p10": percentile(mae, .1), "p50": percentile(mae, .5), "p90": percentile(mae, .9)}, "retracement_wait_candles_distribution": {"p10": percentile(retrace_wait, .1), "p50": percentile(retrace_wait, .5), "p90": percentile(retrace_wait, .9)}, "time_to_invalidation_candles_distribution": {"p10": percentile(invalidation_candles, .1), "p50": percentile(invalidation_candles, .5), "p90": percentile(invalidation_candles, .9)}, "qualification_policy": "context produces flags; no context variable is a mandatory optimized rejection filter"}


def write_outputs(runs: list[dict[str, Any]]) -> None:
    result = {"schema": "context_structure_retrace_phase3", "phase": 3, "research_only": True, "trade_selection_enabled": False, "paper_runner_started": False, "phase2_representation_hash": phase2_hash(), "instruments": {r["symbol"]: {"summary": summarize(r), "config": r["config"]} for r in runs}}
    (ROOT / "context_structure_retrace_phase3_results.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    with gzip.open(ROOT / "context_structure_retrace_phase3_snapshots.jsonl.gz", "wt", encoding="utf-8", compresslevel=1) as snapshots, gzip.open(ROOT / "context_structure_retrace_phase3_ledger.jsonl.gz", "wt", encoding="utf-8", compresslevel=1) as ledger:
        seen_snapshots = set()
        for run in runs:
            for row in run["rows"]:
                snapshot_ref = f"{run['symbol']}|{row['setup_event']['timestamp']}"
                if snapshot_ref not in seen_snapshots:
                    snapshots.write(json.dumps({"snapshot_id": snapshot_ref, "symbol": run["symbol"], "timestamp": row["setup_event"]["timestamp"], "context_snapshot": row["context_snapshot"]}, separators=(",", ":"), default=str) + "\n")
                    seen_snapshots.add(snapshot_ref)
                ledger_row = {"symbol": run["symbol"], **row}
                ledger_row.pop("context_snapshot", None)
                ledger_row["context_snapshot_ref"] = snapshot_ref
                ledger.write(json.dumps(ledger_row, separators=(",", ":"), default=str) + "\n")
    fields = ["symbol", "setup_id", "market_event_id", "pattern", "direction", "event_timestamp", "qualification", "rejection_reason", "entry_signal_types", "entry_opportunity_id", "entry_attempt_count", "spread"]
    with (ROOT / "context_structure_retrace_phase3_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for run in runs:
            for r in run["rows"]:
                w.writerow({"symbol": run["symbol"], "setup_id": r["setup_id"], "market_event_id": r["market_event_id"], "pattern": r["setup_event"]["pattern"], "direction": r["setup_event"]["direction"], "event_timestamp": r["setup_event"]["timestamp"], "qualification": r["qualification"], "rejection_reason": r["rejection_reason"], "entry_signal_types": ",".join(r["entry_signals"]), "entry_opportunity_id": r["entry_opportunity_id"], "entry_attempt_count": len(r["entry_attempts"]), "spread": r["spread"]})
    lines = ["# CONTEXT_STRUCTURE_RETRACE_V1 — Phase 3", "", "Historical trade-candidate hypothesis research only. No paper runner, orders, or existing strategy changes.", "", f"Phase 2 representation hash: `{phase2_hash()}`", "", "## Results", "", "| Instrument | Setup events | Qualified | Qualification rejected | Invalidated | No retrace | Entered |", "|---|---:|---:|---:|---:|---:|---:|"]
    for run in runs:
        s = summarize(run); lines.append(f"| {run['symbol']} | {s['setup_events']} | {s['qualified']} | {s['qualification_rejected']} | {s['invalidated']} | {s['no_retrace']} | {s['entered']} |")
    lines += ["", "## Lifecycle", "", "`SETUP_EVENT -> CONTEXT_SNAPSHOT -> QUALIFICATION -> WAITING_FOR_RETRACEMENT -> ENTRY_ATTEMPT -> STOP/TARGET HYPOTHESES -> OUTCOME LABELS`", "", "Qualification is intentionally non-optimizing. Context produces measurable flags; it is not silently converted into symbol-specific filters. Stop and target combinations remain separate hypotheses.", "", "## Representative lifecycles"]
    for run in runs:
        seen = set(); lines.append(f"\n### {run['symbol']}")
        for row in run["rows"]:
            pattern = row["setup_event"]["pattern"]
            if pattern in seen: continue
            seen.add(pattern)
            lines.append(f"- `{pattern}` at `{row['setup_event']['timestamp']}`: `{row['qualification']}`; flags={','.join(row['qualification_flags']) or 'NONE'}; rejection={row['rejection_reason'] or 'NONE'}; retrace signals={','.join(row['entry_signals']) or 'NONE'}; attempts={len(row['entry_attempts'])}; invalidation={row['invalidation'].get('timestamp') or 'NONE'}.")
    lines += ["", "## Remaining work", "", "Phase 4 should review representative lifecycles, validate ambiguity handling with M1 where available, and freeze any explicitly approved trade hypothesis before using future data as a true holdout."]
    (ROOT / "context_structure_retrace_phase3_summary.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    # Symbols are independent research inputs; parallelism changes no
    # strategy logic and keeps the four identical replays practical.
    with ProcessPoolExecutor(max_workers=min(4, len(DATA_FILES))) as pool:
        runs = list(pool.map(replay_symbol, DATA_FILES))
    write_outputs(runs)
    print(json.dumps({r["symbol"]: summarize(r) for r in runs}, indent=2))


if __name__ == "__main__":
    main()
