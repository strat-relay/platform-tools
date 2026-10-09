"""Read-only early post-entry M5 microstructure study for KOJO_STRUCTURE_RECLAIM_V3.

Observes the first 15 minutes (3 completed M5 bars) after each of the 38 discovery
trades, and the full M5 sequence through terminal exit.

Base commits:
    V3_ENTRY             = c82d290
    POST_ENTRY           = 1e321fe
    EXIT_COUNTERFACTUAL  = d9264a7
    FAILURE_ANATOMY      = 827a95f
    ENTRY_GEOMETRY_AUDIT = 3351fb3

Safety constraints:
    BROKER_WRITES=0
    VALIDATION_OUTCOMES_ACCESSED=false
    ENTRY_RULE_CHANGED=false
    STOP_RULE_CHANGED=false
    TARGET_RULE_CHANGED=false
    TRADE_MANAGER_POLICY_CREATED=false
    EXIT_RULE_OPTIMIZED=false
    PARAMETER_SEARCH=false
    PRODUCTION_CHANGED=false
    READY_FOR_VALIDATION=false
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from statistics import median, mean, stdev
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy_backtest.kojo_structure_reclaim_v3 import (
    _rejection_wick_bullish,
    _rejection_wick_bearish,
)

V3_ENTRY_COMMIT             = "c82d290"
POST_ENTRY_COMMIT           = "1e321fe"
EXIT_COUNTERFACTUAL_COMMIT  = "d9264a7"
FAILURE_ANATOMY_COMMIT      = "827a95f"
ENTRY_GEOMETRY_COMMIT       = "3351fb3"

M5_SECONDS  = 300
M15_SECONDS = 900
POINT       = 0.001

ENTRY_GEOMETRY_PATH = Path("artifacts/research/kojo_v3_entry_geometry_audit.json")
POST_ENTRY_PATH     = Path("artifacts/research/kojo_v3_post_entry_behavior.json")
FAILURE_ANAT_PATH   = Path("artifacts/research/kojo_v3_failure_anatomy.json")


# ──────────────────────────────────────────────────────────────────────────────
# bar-level causal primitives (reuse V3 originals; define engulf locally)
# ──────────────────────────────────────────────────────────────────────────────

def _body_close_bearish(bar: dict) -> bool:
    o, h, lo, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    body = abs(c - o)
    full_range = h - lo
    if full_range < 1e-9:
        return False
    return (body / full_range >= 0.25) and (c < o)


def _body_close_bullish(bar: dict) -> bool:
    o, h, lo, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    body = abs(c - o)
    full_range = h - lo
    if full_range < 1e-9:
        return False
    return (body / full_range >= 0.25) and (c > o)


def _engulf_bearish(prev: dict, curr: dict) -> bool:
    """Bearish engulf: current candle body engulfs previous candle body."""
    pc, po = float(prev["close"]), float(prev["open"])
    cc, co = float(curr["close"]), float(curr["open"])
    return (cc < co) and (co >= max(pc, po)) and (cc <= min(pc, po))


def _engulf_bullish(prev: dict, curr: dict) -> bool:
    """Bullish engulf: current candle body engulfs previous candle body."""
    pc, po = float(prev["close"]), float(prev["open"])
    cc, co = float(curr["close"]), float(curr["open"])
    return (cc > co) and (co <= min(pc, po)) and (cc >= max(pc, po))


# ──────────────────────────────────────────────────────────────────────────────
# R-conversion helpers
# ──────────────────────────────────────────────────────────────────────────────

def _r_from_entry(price: float, entry: float, risk: float, direction: str) -> float:
    if direction == "LONG":
        return (price - entry) / risk
    return (entry - price) / risk


def _mfe_bar(o: float, h: float, lo: float, entry: float, risk: float, direction: str) -> float:
    if direction == "LONG":
        return (h - entry) / risk
    return (entry - lo) / risk


def _mae_bar(o: float, h: float, lo: float, entry: float, risk: float, direction: str) -> float:
    if direction == "LONG":
        return (entry - lo) / risk
    return (h - entry) / risk


def _bar_direction(o: float, c: float, direction: str) -> str:
    if direction == "LONG":
        return "FAVORABLE" if c > o else ("ADVERSE" if c < o else "NEUTRAL")
    return "FAVORABLE" if c < o else ("ADVERSE" if c > o else "NEUTRAL")


def _close_direction(c: float, entry: float, direction: str) -> str:
    if direction == "LONG":
        return "ABOVE_ENTRY" if c > entry else ("BELOW_ENTRY" if c < entry else "AT_ENTRY")
    return "BELOW_ENTRY" if c < entry else ("ABOVE_ENTRY" if c > entry else "AT_ENTRY")


# ──────────────────────────────────────────────────────────────────────────────
# per-bar geometry
# ──────────────────────────────────────────────────────────────────────────────

def _bar_geometry(bar: dict) -> dict:
    o, h, lo, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
    full_range = h - lo
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    return {
        "open": round(o, 3),
        "high": round(h, 3),
        "low": round(lo, 3),
        "close": round(c, 3),
        "range": round(full_range, 3),
        "body_size": round(body, 3),
        "upper_wick": round(upper_wick, 3),
        "lower_wick": round(lower_wick, 3),
        "body_to_range_ratio": round(body / full_range, 4) if full_range > 1e-9 else 0.0,
    }


def _bar_relations(
    bar: dict,
    entry: float,
    key_level: float,
    retest_level: float,
    rejection_extreme: float,
    stop: float,
    direction: str,
) -> dict:
    lo, h, c = float(bar["low"]), float(bar["high"]), float(bar["close"])

    if direction == "SHORT":
        above_entry  = h > entry
        below_entry  = lo < entry
        above_level  = h > key_level
        below_level  = lo < key_level
        above_retest = h > retest_level
        below_retest = lo < retest_level
        close_above_entry  = c > entry
        close_above_level  = c > key_level
        close_above_retest = c > retest_level
    else:
        above_entry  = h > entry
        below_entry  = lo < entry
        above_level  = h > key_level
        below_level  = lo < key_level
        above_retest = h > retest_level
        below_retest = lo < retest_level
        close_above_entry  = c > entry
        close_above_level  = c > key_level
        close_above_retest = c > retest_level

    return {
        "price_touches_entry": above_entry and below_entry,
        "close_adverse_to_entry": close_above_entry if direction == "SHORT" else not close_above_entry,
        "close_adverse_to_level": close_above_level if direction == "SHORT" else not close_above_level,
        "close_adverse_to_retest": close_above_retest if direction == "SHORT" else not close_above_retest,
    }


# ──────────────────────────────────────────────────────────────────────────────
# per-trade analysis
# ──────────────────────────────────────────────────────────────────────────────

def _analyze_trade(
    eg_trade: dict,
    pe_trade: dict,
    fa_trade: dict,
    m5_by_ts: dict[int, dict],
    immediate_peak_sig_ids: set[str],
) -> dict:
    sig_id    = eg_trade["signal_id"]
    direction = eg_trade["direction"]
    entry     = float(eg_trade["entry_price"])
    stop      = float(eg_trade["initial_stop"])
    risk      = abs(entry - stop)
    level     = float(eg_trade["h1_key_level"])
    retest    = float(eg_trade["m15_retest_extreme"])
    rej_ext   = float(eg_trade.get("m15_rejection_high") or eg_trade.get("m15_rejection_low") or entry)
    entry_ts  = int(eg_trade["entry_decision_ts"])
    tp1       = float(eg_trade["tp1"])
    is_winner = eg_trade["static_status"] == "TARGET_HIT"
    is_immediate_peak = sig_id in immediate_peak_sig_ids
    terminal_minutes  = fa_trade["sl_minutes"]  # time from entry to terminal exit (SL or TP)
    terminal_ts = entry_ts + terminal_minutes * 60

    # ── Section A: first 3 M5 bars ───────────────────────────────────────────
    first_3_bars: list[dict] = []
    cumulative_mfe = 0.0
    cumulative_mae = 0.0
    for i in range(3):
        bar_ts = entry_ts + i * M5_SECONDS
        bar = m5_by_ts.get(bar_ts)
        if bar is None:
            break
        o, h, lo, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        bar_mfe = _mfe_bar(o, h, lo, entry, risk, direction)
        bar_mae = _mae_bar(o, h, lo, entry, risk, direction)
        cumulative_mfe = max(cumulative_mfe, bar_mfe)
        cumulative_mae = max(cumulative_mae, bar_mae)
        close_r = round(_r_from_entry(c, entry, risk, direction), 4)
        geom = _bar_geometry(bar)
        geom["bar_index"] = i
        geom["open_ts"] = bar_ts
        geom["mfe_r_to_date"] = round(cumulative_mfe, 4)
        geom["mae_r_to_date"] = round(cumulative_mae, 4)
        geom["close_r_from_entry"] = close_r
        geom["bar_direction"] = _bar_direction(o, c, direction)
        geom["close_relation_to_entry"] = _close_direction(c, entry, direction)
        geom["close_adverse_to_level"] = (c > level) if direction == "SHORT" else (c < level)
        geom["close_adverse_to_retest"] = (c > retest) if direction == "SHORT" else (c < retest)
        geom["bar_still_in_trade"] = (bar_ts < terminal_ts)
        geom["rej_wick_against_trade"] = (
            _rejection_wick_bullish(bar) if direction == "SHORT"
            else _rejection_wick_bearish(bar)
        )
        geom["rej_wick_in_trade_direction"] = (
            _rejection_wick_bearish(bar) if direction == "SHORT"
            else _rejection_wick_bullish(bar)
        )
        first_3_bars.append(geom)

    # ── Section B: immediate-peak extended ───────────────────────────────────
    section_b: dict[str, Any] = {"is_immediate_peak_loser": is_immediate_peak}
    if first_3_bars:
        section_b["first_m5_close_r"]  = first_3_bars[0]["close_r_from_entry"] if len(first_3_bars) > 0 else None
        section_b["second_m5_close_r"] = first_3_bars[1]["close_r_from_entry"] if len(first_3_bars) > 1 else None
        section_b["third_m5_close_r"]  = first_3_bars[2]["close_r_from_entry"] if len(first_3_bars) > 2 else None

    # ── Section C: MFE giveback ───────────────────────────────────────────────
    # Run full M5 sequence to terminal exit
    full_bars: list[dict] = []
    ts = entry_ts
    running_mfe = 0.0
    running_mae = 0.0
    while ts < terminal_ts:
        bar = m5_by_ts.get(ts)
        if bar is None:
            ts += M5_SECONDS
            continue
        o, h, lo, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        running_mfe = max(running_mfe, _mfe_bar(o, h, lo, entry, risk, direction))
        running_mae = max(running_mae, _mae_bar(o, h, lo, entry, risk, direction))
        close_r = _r_from_entry(c, entry, risk, direction)
        full_bars.append({
            "ts": ts,
            "bar_index": (ts - entry_ts) // M5_SECONDS,
            "open": o, "high": h, "low": lo, "close": c,
            "close_r": round(close_r, 4),
            "mfe_r_to_date": round(running_mfe, 4),
            "mae_r_to_date": round(running_mae, 4),
            "bar_direction": _bar_direction(o, c, direction),
        })
        ts += M5_SECONDS

    # Terminal bar: if terminal_ts is not on a M5 boundary, add it
    term_bar = m5_by_ts.get(terminal_ts)
    if term_bar and terminal_ts not in {b["ts"] for b in full_bars}:
        o, h, lo, c = float(term_bar["open"]), float(term_bar["high"]), float(term_bar["low"]), float(term_bar["close"])
        running_mfe = max(running_mfe, _mfe_bar(o, h, lo, entry, risk, direction))
        running_mae = max(running_mae, _mae_bar(o, h, lo, entry, risk, direction))
        full_bars.append({
            "ts": terminal_ts,
            "bar_index": (terminal_ts - entry_ts) // M5_SECONDS,
            "open": o, "high": h, "low": lo, "close": c,
            "close_r": round(_r_from_entry(c, entry, risk, direction), 4),
            "mfe_r_to_date": round(running_mfe, 4),
            "mae_r_to_date": round(running_mae, 4),
            "bar_direction": _bar_direction(o, c, direction),
        })

    final_mfe = running_mfe

    # Find initial MFE bar index (first bar that achieved the overall MFE)
    mfe_bar_idx = None
    peak_mfe = 0.0
    for b in full_bars:
        if b["mfe_r_to_date"] > peak_mfe:
            peak_mfe = b["mfe_r_to_date"]
            mfe_bar_idx = b["bar_index"]

    # Giveback and re-entry timing
    time_to_entry_revisit_min = None
    time_to_negative_r_min   = None
    for b in full_bars:
        if b["ts"] <= entry_ts:
            continue
        elapsed = (b["ts"] - entry_ts) // 60
        if b["close_r"] <= 0.0 and time_to_entry_revisit_min is None:
            time_to_entry_revisit_min = elapsed
        if b["close_r"] <= -0.01 and time_to_negative_r_min is None:
            time_to_negative_r_min = elapsed

    reached_025r = any(b["mfe_r_to_date"] >= 0.25 for b in full_bars)
    reached_050r = any(b["mfe_r_to_date"] >= 0.50 for b in full_bars)
    reached_100r = any(b["mfe_r_to_date"] >= 1.00 for b in full_bars)

    retrace_after_025_to_entry = False
    retrace_after_050_to_entry = False
    retrace_after_100_to_entry = False
    for i, b in enumerate(full_bars):
        if b["mfe_r_to_date"] >= 0.25 and not retrace_after_025_to_entry:
            for b2 in full_bars[i+1:]:
                if b2["close_r"] <= 0.0:
                    retrace_after_025_to_entry = True
                    break
        if b["mfe_r_to_date"] >= 0.50 and not retrace_after_050_to_entry:
            for b2 in full_bars[i+1:]:
                if b2["close_r"] <= 0.0:
                    retrace_after_050_to_entry = True
                    break
        if b["mfe_r_to_date"] >= 1.00 and not retrace_after_100_to_entry:
            for b2 in full_bars[i+1:]:
                if b2["close_r"] <= 0.0:
                    retrace_after_100_to_entry = True
                    break

    section_c = {
        "max_mfe_r": round(final_mfe, 4),
        "mfe_achieved_on_bar_index": mfe_bar_idx,
        "mfe_achieved_at_minutes": mfe_bar_idx * 5 if mfe_bar_idx is not None else None,
        "time_to_entry_revisit_min": time_to_entry_revisit_min,
        "time_to_negative_r_min": time_to_negative_r_min,
        "reached_0_25r": reached_025r,
        "reached_0_50r": reached_050r,
        "reached_1_00r": reached_100r,
        "retrace_to_entry_after_0_25r": retrace_after_025_to_entry,
        "retrace_to_entry_after_0_50r": retrace_after_050_to_entry,
        "retrace_to_entry_after_1_00r": retrace_after_100_to_entry,
    }

    # ── Section D: M5 persistence ─────────────────────────────────────────────
    consecutive_adverse_closes = 0
    consecutive_favorable_closes = 0
    max_consec_adverse = 0
    max_consec_favorable = 0
    consec_adverse = 0
    consec_favorable = 0

    first_adverse_ts    = None
    first_adverse_r     = None
    consec2_adverse_ts  = None
    consec2_adverse_r   = None
    consec3_adverse_ts  = None
    consec3_adverse_r   = None

    for b in full_bars:
        is_adverse   = b["bar_direction"] == "ADVERSE"
        is_favorable = b["bar_direction"] == "FAVORABLE"

        if is_adverse:
            consec_adverse += 1
            consec_favorable = 0
            if consec_adverse == 1 and first_adverse_ts is None:
                first_adverse_ts = b["ts"]
                first_adverse_r  = b["close_r"]
            if consec_adverse == 2 and consec2_adverse_ts is None:
                consec2_adverse_ts = b["ts"]
                consec2_adverse_r  = b["close_r"]
            if consec_adverse == 3 and consec3_adverse_ts is None:
                consec3_adverse_ts = b["ts"]
                consec3_adverse_r  = b["close_r"]
        else:
            consec_adverse = 0
            if is_favorable:
                consec_favorable += 1
            else:
                consec_favorable = 0

        max_consec_adverse   = max(max_consec_adverse, consec_adverse)
        max_consec_favorable = max(max_consec_favorable, consec_favorable)

    section_d = {
        "max_consecutive_adverse_closes": max_consec_adverse,
        "max_consecutive_favorable_closes": max_consec_favorable,
        "first_adverse_close_ts": first_adverse_ts,
        "first_adverse_close_r": round(first_adverse_r, 4) if first_adverse_r is not None else None,
        "first_adverse_close_minutes": (first_adverse_ts - entry_ts) // 60 if first_adverse_ts else None,
        "two_consecutive_adverse_closes": consec2_adverse_ts is not None,
        "two_consec_adverse_ts": consec2_adverse_ts,
        "two_consec_adverse_r": round(consec2_adverse_r, 4) if consec2_adverse_r is not None else None,
        "two_consec_adverse_minutes": (consec2_adverse_ts - entry_ts) // 60 if consec2_adverse_ts else None,
        "three_consecutive_adverse_closes": consec3_adverse_ts is not None,
        "three_consec_adverse_ts": consec3_adverse_ts,
        "three_consec_adverse_r": round(consec3_adverse_r, 4) if consec3_adverse_r is not None else None,
        "three_consec_adverse_minutes": (consec3_adverse_ts - entry_ts) // 60 if consec3_adverse_ts else None,
    }

    # ── Section E: key-level / retest-level behavior ──────────────────────────
    first_close_thru_entry_ts = None
    first_close_thru_retest_ts = None
    first_close_thru_level_ts = None

    for b in full_bars:
        c = b["close"]
        elapsed = (b["ts"] - entry_ts) // 60
        if direction == "SHORT":
            thru_entry  = c > entry
            thru_retest = c > retest
            thru_level  = c > level
        else:
            thru_entry  = c < entry
            thru_retest = c < retest
            thru_level  = c < level

        if thru_entry  and first_close_thru_entry_ts  is None:
            first_close_thru_entry_ts  = b["ts"]
        if thru_retest and first_close_thru_retest_ts is None:
            first_close_thru_retest_ts = b["ts"]
        if thru_level  and first_close_thru_level_ts  is None:
            first_close_thru_level_ts  = b["ts"]

    # persistent vs transient reclaim: does crossing persist for 2+ consecutive M5 bars?
    transient_m5_reclaim  = False
    persistent_m5_reclaim = False
    consec_adverse_close_count = 0
    for b in full_bars:
        c = b["close"]
        adverse_close = (c > entry) if direction == "SHORT" else (c < entry)
        if adverse_close:
            consec_adverse_close_count += 1
            if consec_adverse_close_count == 1:
                transient_m5_reclaim = True
            if consec_adverse_close_count >= 2:
                persistent_m5_reclaim = True
        else:
            consec_adverse_close_count = 0

    section_e = {
        "first_close_thru_entry_ts":   first_close_thru_entry_ts,
        "first_close_thru_entry_min":  (first_close_thru_entry_ts - entry_ts)  // 60 if first_close_thru_entry_ts  else None,
        "first_close_thru_retest_ts":  first_close_thru_retest_ts,
        "first_close_thru_retest_min": (first_close_thru_retest_ts - entry_ts) // 60 if first_close_thru_retest_ts else None,
        "first_close_thru_level_ts":   first_close_thru_level_ts,
        "first_close_thru_level_min":  (first_close_thru_level_ts - entry_ts)  // 60 if first_close_thru_level_ts  else None,
        "transient_m5_reclaim":  transient_m5_reclaim,
        "persistent_m5_reclaim": persistent_m5_reclaim,
    }

    # ── Section F: extension ──────────────────────────────────────────────────
    bar0_mfe = full_bars[0]["mfe_r_to_date"] if full_bars else 0.0
    bar1_mfe = full_bars[1]["mfe_r_to_date"] if len(full_bars) > 1 else bar0_mfe
    bar2_mfe = full_bars[2]["mfe_r_to_date"] if len(full_bars) > 2 else bar1_mfe

    first_extends  = bar0_mfe > 0.0
    second_extends = len(full_bars) > 1 and bar1_mfe > bar0_mfe
    third_extends  = len(full_bars) > 2 and bar2_mfe > bar1_mfe

    time_to_second_ext = None
    time_to_third_ext  = None
    if second_extends and len(full_bars) > 1:
        time_to_second_ext = 5  # bar 1 = 5 min
    if third_extends and len(full_bars) > 2:
        time_to_third_ext = 10  # bar 2 = 10 min

    no_further_ext_after_bar0 = first_extends and not second_extends

    section_f = {
        "first_m5_creates_favorable_extreme": first_extends,
        "second_m5_extends_favorable_extreme": second_extends,
        "third_m5_extends_favorable_extreme": third_extends,
        "time_to_second_extension_min": time_to_second_ext,
        "time_to_third_extension_min": time_to_third_ext,
        "no_further_extension_after_first_m5": no_further_ext_after_bar0,
    }

    # ── Section G: M5 reversal / opposite structure ───────────────────────────
    opposite_events: list[dict] = []
    for i, b in enumerate(full_bars):
        bar_dict = {
            "time": b["ts"], "open": b["open"], "high": b["high"],
            "low": b["low"], "close": b["close"],
        }
        rej_against = (
            _rejection_wick_bullish(bar_dict) if direction == "SHORT"
            else _rejection_wick_bearish(bar_dict)
        )
        strong_body_against = (
            _body_close_bullish(bar_dict) if direction == "SHORT"
            else _body_close_bearish(bar_dict)
        )
        engulf_against = False
        if i > 0:
            prev = full_bars[i - 1]
            prev_dict = {
                "time": prev["ts"], "open": prev["open"], "high": prev["high"],
                "low": prev["low"], "close": prev["close"],
            }
            engulf_against = (
                _engulf_bullish(prev_dict, bar_dict) if direction == "SHORT"
                else _engulf_bearish(prev_dict, bar_dict)
            )

        if rej_against or strong_body_against or engulf_against:
            elapsed = (b["ts"] - entry_ts) // 60
            opposite_events.append({
                "bar_index": b["bar_index"],
                "ts": b["ts"],
                "minutes_from_entry": elapsed,
                "close_r": b["close_r"],
                "rejection_wick_against_trade": rej_against,
                "strong_body_close_against_trade": strong_body_against,
                "engulf_against_trade": engulf_against,
            })

    earliest_opposite_event_min = opposite_events[0]["minutes_from_entry"] if opposite_events else None
    earliest_opposite_event_r   = opposite_events[0]["close_r"] if opposite_events else None

    section_g = {
        "opposite_events_count": len(opposite_events),
        "earliest_opposite_event_min": earliest_opposite_event_min,
        "earliest_opposite_event_r": earliest_opposite_event_r,
        "events": opposite_events[:10],  # cap to avoid bloating artifact
    }

    return {
        "signal_id": sig_id,
        "direction": direction,
        "static_status": eg_trade["static_status"],
        "is_winner": is_winner,
        "is_immediate_peak_loser": is_immediate_peak,
        "entry_ts": entry_ts,
        "entry_price": round(entry, 3),
        "initial_stop": round(stop, 3),
        "risk_points": round(risk, 3),
        "h1_key_level": round(level, 3),
        "m15_retest_extreme": round(retest, 3),
        "tp1_price": round(tp1, 3),
        "terminal_minutes": terminal_minutes,
        "total_m5_bars": len(full_bars),
        "section_a_first_3_bars": first_3_bars,
        "section_b_immediate_peak": section_b,
        "section_c_giveback": section_c,
        "section_d_persistence": section_d,
        "section_e_level_behavior": section_e,
        "section_f_extension": section_f,
        "section_g_opposite_structure": section_g,
    }


# ──────────────────────────────────────────────────────────────────────────────
# statistical helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 1) if d else 0.0


def _med(vals: list) -> float | None:
    return round(median(vals), 4) if vals else None


def _count_pct(subset: list[dict], key: str, val=True) -> tuple[int, float]:
    n = sum(1 for t in subset if t.get(key) == val)
    return n, _pct(n, len(subset))


def _get_nested(trade: dict, *path) -> Any:
    obj = trade
    for key in path:
        if obj is None:
            return None
        obj = obj.get(key)
    return obj


# ──────────────────────────────────────────────────────────────────────────────
# discrimination table
# ──────────────────────────────────────────────────────────────────────────────

def _discrimination_table(losers: list[dict], winners: list[dict]) -> list[dict]:
    nl, nw = len(losers), len(winners)
    rows = []

    def row(feature: str, lc: int, wc: int) -> dict:
        lp = _pct(lc, nl)
        wp = _pct(wc, nw)
        return {
            "feature": feature,
            "loser_count": lc, "loser_pct": lp,
            "winner_count": wc, "winner_pct": wp,
            "delta_pp": round(lp - wp, 1),
        }

    # Immediate peak
    lc = sum(1 for t in losers if t["is_immediate_peak_loser"])
    rows.append(row("IMMEDIATE_PEAK_LOSER", lc, 0))

    # First M5 adverse close
    lc = sum(1 for t in losers if _get_nested(t, "section_a_first_3_bars") and
             t["section_a_first_3_bars"][0]["bar_direction"] == "ADVERSE")
    wc = sum(1 for t in winners if _get_nested(t, "section_a_first_3_bars") and
             t["section_a_first_3_bars"][0]["bar_direction"] == "ADVERSE")
    rows.append(row("FIRST_M5_ADVERSE_CLOSE", lc, wc))

    # First M5 close adverse to entry
    lc = sum(1 for t in losers if _get_nested(t, "section_a_first_3_bars") and
             t["section_a_first_3_bars"][0]["close_r_from_entry"] < 0)
    wc = sum(1 for t in winners if _get_nested(t, "section_a_first_3_bars") and
             t["section_a_first_3_bars"][0]["close_r_from_entry"] < 0)
    rows.append(row("FIRST_M5_CLOSE_NEGATIVE_R", lc, wc))

    # Return to entry within 15 min (first 3 M5 bars)
    lc = sum(1 for t in losers
             if t["section_c_giveback"]["time_to_entry_revisit_min"] is not None
             and t["section_c_giveback"]["time_to_entry_revisit_min"] <= 15)
    wc = sum(1 for t in winners
             if t["section_c_giveback"]["time_to_entry_revisit_min"] is not None
             and t["section_c_giveback"]["time_to_entry_revisit_min"] <= 15)
    rows.append(row("RETURN_TO_ENTRY_WITHIN_15M", lc, wc))

    # Two consecutive adverse M5 closes
    lc = sum(1 for t in losers if t["section_d_persistence"]["two_consecutive_adverse_closes"])
    wc = sum(1 for t in winners if t["section_d_persistence"]["two_consecutive_adverse_closes"])
    rows.append(row("TWO_CONSECUTIVE_ADVERSE_M5_CLOSES", lc, wc))

    # Three consecutive adverse M5 closes
    lc = sum(1 for t in losers if t["section_d_persistence"]["three_consecutive_adverse_closes"])
    wc = sum(1 for t in winners if t["section_d_persistence"]["three_consecutive_adverse_closes"])
    rows.append(row("THREE_CONSECUTIVE_ADVERSE_M5_CLOSES", lc, wc))

    # Persistent M5 reclaim (2+ M5 closes adverse to entry)
    lc = sum(1 for t in losers if t["section_e_level_behavior"]["persistent_m5_reclaim"])
    wc = sum(1 for t in winners if t["section_e_level_behavior"]["persistent_m5_reclaim"])
    rows.append(row("PERSISTENT_M5_RECLAIM", lc, wc))

    # Transient M5 reclaim (at least 1 adverse close)
    lc = sum(1 for t in losers if t["section_e_level_behavior"]["transient_m5_reclaim"])
    wc = sum(1 for t in winners if t["section_e_level_behavior"]["transient_m5_reclaim"])
    rows.append(row("TRANSIENT_M5_RECLAIM", lc, wc))

    # M5 close through retest level
    lc = sum(1 for t in losers if t["section_e_level_behavior"]["first_close_thru_retest_ts"] is not None)
    wc = sum(1 for t in winners if t["section_e_level_behavior"]["first_close_thru_retest_ts"] is not None)
    rows.append(row("M5_CLOSE_THROUGH_RETEST_LEVEL", lc, wc))

    # M5 close through key level
    lc = sum(1 for t in losers if t["section_e_level_behavior"]["first_close_thru_level_ts"] is not None)
    wc = sum(1 for t in winners if t["section_e_level_behavior"]["first_close_thru_level_ts"] is not None)
    rows.append(row("M5_CLOSE_THROUGH_KEY_LEVEL", lc, wc))

    # No second favorable extension
    lc = sum(1 for t in losers if not t["section_f_extension"]["second_m5_extends_favorable_extreme"])
    wc = sum(1 for t in winners if not t["section_f_extension"]["second_m5_extends_favorable_extreme"])
    rows.append(row("NO_SECOND_FAVORABLE_EXTENSION", lc, wc))

    # No further extension after first M5
    lc = sum(1 for t in losers if t["section_f_extension"]["no_further_extension_after_first_m5"])
    wc = sum(1 for t in winners if t["section_f_extension"]["no_further_extension_after_first_m5"])
    rows.append(row("NO_FURTHER_EXTENSION_AFTER_FIRST_M5", lc, wc))

    # No third favorable extension
    lc = sum(1 for t in losers if not t["section_f_extension"]["third_m5_extends_favorable_extreme"])
    wc = sum(1 for t in winners if not t["section_f_extension"]["third_m5_extends_favorable_extreme"])
    rows.append(row("NO_THIRD_FAVORABLE_EXTENSION", lc, wc))

    # Any opposite M5 structure
    lc = sum(1 for t in losers if t["section_g_opposite_structure"]["opposite_events_count"] > 0)
    wc = sum(1 for t in winners if t["section_g_opposite_structure"]["opposite_events_count"] > 0)
    rows.append(row("OPPOSITE_M5_STRUCTURE", lc, wc))

    # Opposite M5 structure in first 15 min
    lc = sum(1 for t in losers
             if t["section_g_opposite_structure"]["earliest_opposite_event_min"] is not None
             and t["section_g_opposite_structure"]["earliest_opposite_event_min"] <= 15)
    wc = sum(1 for t in winners
             if t["section_g_opposite_structure"]["earliest_opposite_event_min"] is not None
             and t["section_g_opposite_structure"]["earliest_opposite_event_min"] <= 15)
    rows.append(row("OPPOSITE_M5_STRUCTURE_WITHIN_15M", lc, wc))

    return sorted(rows, key=lambda r: abs(r["delta_pp"]), reverse=True)


# ──────────────────────────────────────────────────────────────────────────────
# section H: winner control answers
# ──────────────────────────────────────────────────────────────────────────────

def _winner_control(winners: list[dict]) -> dict:
    nw = len(winners)

    def _yn(lc: int) -> str:
        pct = lc / nw * 100 if nw else 0
        if pct >= 60:
            return "YES"
        if pct <= 15:
            return "NO"
        return "MIXED"

    ret_to_entry = sum(1 for t in winners
                       if t["section_c_giveback"]["time_to_entry_revisit_min"] is not None
                       and t["section_c_giveback"]["time_to_entry_revisit_min"] <= 30)
    adverse_bar1 = sum(1 for t in winners
                       if t["section_a_first_3_bars"]
                       and t["section_a_first_3_bars"][0]["bar_direction"] == "ADVERSE")
    two_consec = sum(1 for t in winners
                     if t["section_d_persistence"]["two_consecutive_adverse_closes"])
    persistent = sum(1 for t in winners
                     if t["section_e_level_behavior"]["persistent_m5_reclaim"])
    continues_ext = sum(1 for t in winners
                        if t["section_f_extension"]["second_m5_extends_favorable_extreme"]
                        or t["section_f_extension"]["third_m5_extends_favorable_extreme"])
    survives_gback = sum(1 for t in winners
                         if t["section_c_giveback"]["time_to_entry_revisit_min"] is not None
                         and t["section_c_giveback"]["reached_1_00r"])

    return {
        "DO_WINNERS_FREQUENTLY_RETURN_TO_ENTRY_EARLY": _yn(ret_to_entry),
        "DO_WINNERS_HAVE_ADVERSE_FIRST_M5_CLOSES": _yn(adverse_bar1),
        "DO_WINNERS_SHOW_TWO_CONSECUTIVE_ADVERSE_M5_CLOSES": _yn(two_consec),
        "DO_WINNERS_SHOW_PERSISTENT_M5_RECLAIM": _yn(persistent),
        "DO_WINNERS_CONTINUE_MAKING_FAVORABLE_EXTREMES": _yn(continues_ext),
        "DO_WINNERS_SURVIVE_EARLY_GIVEBACK_AND_RECOVER": _yn(survives_gback),
        "detail": {
            "return_to_entry_within_30min_count": ret_to_entry,
            "adverse_first_bar_count": adverse_bar1,
            "two_consec_adverse_count": two_consec,
            "persistent_reclaim_count": persistent,
            "continues_extension_count": continues_ext,
            "survives_giveback_to_tp_count": survives_gback,
            "n_winners": nw,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# core conclusion (K)
# ──────────────────────────────────────────────────────────────────────────────

def _core_conclusion(disc_table: list[dict], losers: list[dict], winners: list[dict]) -> dict:
    max_delta = max((abs(r["delta_pp"]) for r in disc_table), default=0)

    if max_delta >= 40:
        discrimination = "STRONGLY"
    elif max_delta >= 20:
        discrimination = "MODERATELY"
    elif max_delta >= 10:
        discrimination = "WEAKLY"
    elif max_delta >= 5:
        discrimination = "NO"
    else:
        discrimination = "NO"

    # Immediate-peak sequence check
    ip_losers = [t for t in losers if t["is_immediate_peak_loser"]]
    ip_sequence_common = False
    if ip_losers:
        no_ext = sum(1 for t in ip_losers
                     if t["section_f_extension"]["no_further_extension_after_first_m5"])
        ip_sequence_common = no_ext / len(ip_losers) >= 0.7

    winner_sequence_common = False
    if winners:
        w_ext = sum(1 for t in winners
                    if t["section_f_extension"]["second_m5_extends_favorable_extreme"]
                    or t["section_f_extension"]["third_m5_extends_favorable_extreme"])
        winner_sequence_common = w_ext / len(winners) >= 0.5

    # Early management: is it justified to study further?
    highest_disc_feature = disc_table[0] if disc_table else {}
    early_mgmt_justified = max_delta >= 20

    return {
        "EARLY_M5_BEHAVIOR_DISCRIMINATING": discrimination,
        "IMMEDIATE_PEAK_LOSERS_HAVE_COMMON_M5_SEQUENCE": "YES" if ip_sequence_common else "MIXED",
        "WINNERS_COMMONLY_SHARE_SAME_SEQUENCE": "YES" if winner_sequence_common else "MIXED",
        "EARLY_MANAGEMENT_RESEARCH_JUSTIFIED": early_mgmt_justified,
        "highest_discriminating_feature": highest_disc_feature.get("feature"),
        "highest_delta_pp": highest_disc_feature.get("delta_pp"),
        "max_delta_pp_any_feature": round(max_delta, 1),
    }


# ──────────────────────────────────────────────────────────────────────────────
# section J: source-supported interpretation
# ──────────────────────────────────────────────────────────────────────────────

def _source_interpretation(disc_table: list[dict]) -> dict:
    feature_map = {
        "PERSISTENT_M5_RECLAIM": {
            "label": "Persistent M5 adverse closes — reclaim of broken structure on M5",
            "source_status": "SOURCE_COMPATIBLE_IMPLEMENTATION_HYPOTHESIS",
            "reasoning": (
                "Source explicitly names 'reclaim of broken structure' as a management reason. "
                "M5 persistent adverse closes are a lower-timeframe realization of the same concept. "
                "No source text explicitly says 'M5 close'; M15 reclaim is the source-level primitive."
            ),
        },
        "NO_FURTHER_EXTENSION_AFTER_FIRST_M5": {
            "label": "No extension after bar 0 — failure to continue on M5",
            "source_status": "SOURCE_COMPATIBLE_IMPLEMENTATION_HYPOTHESIS",
            "reasoning": (
                "Source names 'failure to continue' as a management trigger. "
                "Absence of a second M5 favorable extreme directly evidences failure to continue. "
                "No source text specifies the M5 timeframe for this check."
            ),
        },
        "OPPOSITE_M5_STRUCTURE": {
            "label": "Opposite M5 structure — opposite structure on M5",
            "source_status": "SOURCE_COMPATIBLE_IMPLEMENTATION_HYPOTHESIS",
            "reasoning": (
                "Source names 'opposite structure' as a management reason. "
                "Opposite rejection wick / body close / engulf on M5 expresses this concept. "
                "The primitives are reused from the V3 M15 evaluator; M5 application is an extension."
            ),
        },
        "TWO_CONSECUTIVE_ADVERSE_M5_CLOSES": {
            "label": "Two consecutive adverse M5 closes — H1/M15 alignment deterioration signal",
            "source_status": "SOURCE_COMPATIBLE_IMPLEMENTATION_HYPOTHESIS",
            "reasoning": (
                "Source names 'deterioration of H1/M15 alignment' as a management reason. "
                "Two consecutive adverse closes could serve as an earlier M5-level proxy. "
                "The concept is compatible but no source text specifies this exact pattern."
            ),
        },
    }

    return {
        "source_explicit_management_reasons": [
            "failure to continue",
            "reclaim of broken structure",
            "opposite structure",
            "deterioration of H1/M15 alignment",
        ],
        "m5_feature_interpretations": feature_map,
        "caveat": (
            "No source text explicitly describes M5-level management rules. "
            "All M5 interpretations are SOURCE_COMPATIBLE_IMPLEMENTATION_HYPOTHESIS only."
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="V3 early post-entry M5 microstructure study")
    parser.add_argument("--canonical", type=Path,
        default=Path("/private/tmp/claude-501/-Users-caleb-mt5-native-bridge"
                     "/4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"))
    parser.add_argument("--entry-geometry", type=Path, default=ENTRY_GEOMETRY_PATH)
    parser.add_argument("--post-entry",     type=Path, default=POST_ENTRY_PATH)
    parser.add_argument("--failure-anatomy",type=Path, default=FAILURE_ANAT_PATH)
    parser.add_argument("--artifact-root",  type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    # ── load sources ─────────────────────────────────────────────────────────
    canonical_bytes = args.canonical.read_bytes()
    canonical_sha   = hashlib.sha256(canonical_bytes).hexdigest()
    canonical       = json.loads(canonical_bytes)

    m5_by_ts: dict[int, dict] = {int(b["time"]): b for b in canonical["m5_bars"]}
    print(f"M5 bars loaded: {len(m5_by_ts)}", file=sys.stderr)

    eg  = json.loads(args.entry_geometry.read_bytes())
    pe  = json.loads(args.post_entry.read_bytes())
    fa  = json.loads(args.failure_anatomy.read_bytes())

    eg_by_sig  = {t["signal_id"]: t for t in eg["per_trade_audit"]}
    pe_by_sig  = {t["signal_id"]: t for t in pe["trades"]}
    fa_by_sig  = {t["signal_id"]: t for t in fa["per_trade_anatomy"]}

    # Immediate-peak loser signal IDs (from entry geometry section_h)
    immediate_peak_sig_ids: set[str] = {
        d["signal_id"] for d in eg["section_h_immediate_peak_cohort"]["immediate_peak_details"]
    }
    print(f"Immediate-peak losers: {len(immediate_peak_sig_ids)}", file=sys.stderr)

    # ── per-trade analysis ───────────────────────────────────────────────────
    trade_results: list[dict] = []
    all_sig_ids = list(eg_by_sig.keys())
    for sig_id in all_sig_ids:
        eg_t = eg_by_sig[sig_id]
        pe_t = pe_by_sig[sig_id]
        fa_t = fa_by_sig[sig_id]
        result = _analyze_trade(eg_t, pe_t, fa_t, m5_by_ts, immediate_peak_sig_ids)
        trade_results.append(result)

    print(f"Trades analyzed: {len(trade_results)}", file=sys.stderr)

    losers  = [t for t in trade_results if not t["is_winner"]]
    winners = [t for t in trade_results if t["is_winner"]]

    # ── discrimination table ─────────────────────────────────────────────────
    disc_table = _discrimination_table(losers, winners)

    # ── section H: winner control ────────────────────────────────────────────
    section_h = _winner_control(winners)

    # ── core conclusion ──────────────────────────────────────────────────────
    section_k = _core_conclusion(disc_table, losers, winners)

    # ── source interpretation ────────────────────────────────────────────────
    section_j = _source_interpretation(disc_table)

    # ── aggregate stats by cohort ─────────────────────────────────────────────
    def _agg_cohort(trades: list[dict]) -> dict:
        n = len(trades)
        if not n:
            return {"n": 0}
        first_m5_close_rs = [
            t["section_a_first_3_bars"][0]["close_r_from_entry"]
            for t in trades if t["section_a_first_3_bars"]
        ]
        second_m5_close_rs = [
            t["section_b_immediate_peak"]["second_m5_close_r"]
            for t in trades
            if t["section_b_immediate_peak"]["second_m5_close_r"] is not None
        ]
        max_consec_adverse = [t["section_d_persistence"]["max_consecutive_adverse_closes"] for t in trades]
        two_consec_count   = sum(1 for t in trades if t["section_d_persistence"]["two_consecutive_adverse_closes"])
        three_consec_count = sum(1 for t in trades if t["section_d_persistence"]["three_consecutive_adverse_closes"])
        persistent_reclaim = sum(1 for t in trades if t["section_e_level_behavior"]["persistent_m5_reclaim"])
        no_second_ext      = sum(1 for t in trades if not t["section_f_extension"]["second_m5_extends_favorable_extreme"])
        opposite_any       = sum(1 for t in trades if t["section_g_opposite_structure"]["opposite_events_count"] > 0)
        reached_025r       = sum(1 for t in trades if t["section_c_giveback"]["reached_0_25r"])
        reached_050r       = sum(1 for t in trades if t["section_c_giveback"]["reached_0_50r"])
        return {
            "n": n,
            "first_m5_close_r_median": _med(first_m5_close_rs),
            "second_m5_close_r_median": _med(second_m5_close_rs),
            "max_consec_adverse_median": _med(max_consec_adverse),
            "two_consec_adverse_count": two_consec_count,
            "two_consec_adverse_pct": _pct(two_consec_count, n),
            "three_consec_adverse_count": three_consec_count,
            "three_consec_adverse_pct": _pct(three_consec_count, n),
            "persistent_m5_reclaim_count": persistent_reclaim,
            "persistent_m5_reclaim_pct": _pct(persistent_reclaim, n),
            "no_second_ext_count": no_second_ext,
            "no_second_ext_pct": _pct(no_second_ext, n),
            "opposite_m5_structure_count": opposite_any,
            "opposite_m5_structure_pct": _pct(opposite_any, n),
            "reached_0_25r_count": reached_025r,
            "reached_0_25r_pct": _pct(reached_025r, n),
            "reached_0_50r_count": reached_050r,
            "reached_0_50r_pct": _pct(reached_050r, n),
        }

    ip_losers        = [t for t in losers if t["is_immediate_peak_loser"]]
    non_ip_losers    = [t for t in losers if not t["is_immediate_peak_loser"]]

    # ── build artifact ───────────────────────────────────────────────────────
    artifact = {
        "study_type": "EARLY_POST_ENTRY_M5_MICROSTRUCTURE_OBSERVATIONAL",
        "base_commits": {
            "v3_entry":             V3_ENTRY_COMMIT,
            "post_entry":           POST_ENTRY_COMMIT,
            "exit_counterfactual":  EXIT_COUNTERFACTUAL_COMMIT,
            "failure_anatomy":      FAILURE_ANATOMY_COMMIT,
            "entry_geometry_audit": ENTRY_GEOMETRY_COMMIT,
        },
        "canonical_source_sha256": canonical_sha,
        "discovery_start": pe["discovery_start"],
        "discovery_end":   pe["discovery_end"],
        "methodology": {
            "observation_window": "Entry to terminal exit (SL or TP) using completed M5 bars",
            "intrabar_ordering":  "NOT_INFERRED — only completed-bar OHLC used",
            "mfe_mae_basis":      "Cumulative across completed bars; no intrabar ordering",
            "terminal_exit_source": "failure_anatomy.sl_minutes (time from entry to SL or TP)",
            "m5_bar_source":      "canonical m5_bars (same frozen source as backtest)",
            "existing_primitives_reused": [
                "_rejection_wick_bullish (V3 evaluator)",
                "_rejection_wick_bearish (V3 evaluator)",
            ],
            "new_primitives": [
                "_body_close_bullish (local, body/range >= 25%)",
                "_body_close_bearish (local, body/range >= 25%)",
                "_engulf_bullish (local, body engulf)",
                "_engulf_bearish (local, body engulf)",
            ],
            "no_threshold_tuned": True,
            "no_exit_rule_created": True,
            "no_policy_created": True,
        },
        "total_trades": len(trade_results),
        "total_losers": len(losers),
        "total_winners": len(winners),
        "immediate_peak_loser_count": len(ip_losers),
        "non_immediate_peak_loser_count": len(non_ip_losers),
        "cohort_aggregates": {
            "all_losers":            _agg_cohort(losers),
            "immediate_peak_losers": _agg_cohort(ip_losers),
            "non_ip_losers":         _agg_cohort(non_ip_losers),
            "winners":               _agg_cohort(winners),
        },
        "section_h_winner_control": section_h,
        "section_i_discrimination_table": disc_table,
        "section_j_source_interpretation": section_j,
        "section_k_core_conclusion": section_k,
        "trades": trade_results,
        "no_entry_rule_changed": True,
        "no_stop_rule_changed": True,
        "no_target_rule_changed": True,
        "no_management_policy_created": True,
        "no_exit_rule_optimized": True,
        "no_parameter_search": True,
        "validation_outcomes_accessed": False,
        "production_changed": False,
        "broker_writes": 0,
        "ready_for_validation": False,
    }

    artifact_bytes = json.dumps(artifact, sort_keys=True).encode()
    artifact_fp    = hashlib.sha256(artifact_bytes).hexdigest()
    artifact["artifact_fingerprint"] = artifact_fp

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    out_path = args.artifact_root / "kojo_v3_early_post_entry_microstructure.json"
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"artifact: {out_path}", file=sys.stderr)

    # ── print summary report ──────────────────────────────────────────────────
    print(json.dumps({
        "STUDY": "EARLY_POST_ENTRY_M5_MICROSTRUCTURE",
        "BASE_COMMITS": artifact["base_commits"],
        "ARTIFACT_FINGERPRINT": artifact_fp,
        "TOTAL_TRADES": len(trade_results),
        "TOTAL_LOSERS": len(losers),
        "TOTAL_WINNERS": len(winners),
        "IMMEDIATE_PEAK_LOSERS": len(ip_losers),
        "NON_IP_LOSERS": len(non_ip_losers),
        "cohort_aggregates": artifact["cohort_aggregates"],
        "section_h_winner_control": section_h,
        "section_i_discrimination_table": disc_table,
        "section_k_core_conclusion": section_k,
        "TRADE_MANAGER_POLICY_CREATED": False,
        "EXIT_RULE_OPTIMIZED": False,
        "PARAMETER_SEARCH": False,
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "PRODUCTION_CHANGED": False,
        "BROKER_WRITES": 0,
        "READY_FOR_VALIDATION": False,
    }, indent=2))


if __name__ == "__main__":
    main()
