"""Entry timing and geometry audit for KOJO_STRUCTURE_RECLAIM_V3.

Read-only diagnostic. Sections A-J per spec.

Safety constraints:
    ENTRY_RULE_CHANGED=false
    STOP_RULE_CHANGED=false
    TARGET_RULE_CHANGED=false
    TRADE_MANAGER_POLICY_CREATED=false
    EXIT_RULE_OPTIMIZED=false
    PARAMETER_SEARCH=false
    VALIDATION_OUTCOMES_ACCESSED=false
    PRODUCTION_CHANGED=false
    BROKER_WRITES=0
    READY_FOR_VALIDATION=false
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from statistics import median, quantiles
from typing import Any

DISCOVERY_END = 1786319999
M5_SECONDS  = 300
M15_SECONDS = 900
H1_SECONDS  = 3600
POINT = 0.001  # XAUUSDm tick_size / point from contract metadata

DISCOVERY_PATH  = Path("artifacts/backtests/kojo-v3-discovery-run-1/kojo-v3-discovery-run-1/result.json")
POST_ENTRY_PATH = Path("artifacts/research/kojo_v3_post_entry_behavior.json")
CANONICAL_PATH  = Path(
    "/private/tmp/claude-501/-Users-caleb-mt5-native-bridge/"
    "4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"
)


# ──────────────────────────────────────────────────────────────────────────────
# stats helpers
# ──────────────────────────────────────────────────────────────────────────────

def _f(v: float, d: int = 4) -> float:
    return round(v, d)


def _med(vs: list) -> Any:
    fvs = [float(v) for v in vs if v is not None]
    return round(median(fvs), 4) if fvs else None


def _p25(vs: list) -> Any:
    fvs = [float(v) for v in vs if v is not None]
    if not fvs:
        return None
    if len(fvs) < 2:
        return round(fvs[0], 4)
    return round(quantiles(fvs, n=4)[0], 4)


def _p75(vs: list) -> Any:
    fvs = [float(v) for v in vs if v is not None]
    if not fvs:
        return None
    if len(fvs) < 2:
        return round(fvs[0], 4)
    return round(quantiles(fvs, n=4)[2], 4)


def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 1) if d else 0.0


def _yes_no_mixed(condition_fn, losers, winners, threshold_pp: float = 15.0) -> str:
    """Return YES/NO/MIXED/INSUFFICIENT_EVIDENCE from loser/winner comparison."""
    if len(losers) < 5 or len(winners) < 3:
        return "INSUFFICIENT_EVIDENCE"
    l_vals = [condition_fn(t) for t in losers if condition_fn(t) is not None]
    w_vals = [condition_fn(t) for t in winners if condition_fn(t) is not None]
    if not l_vals or not w_vals:
        return "INSUFFICIENT_EVIDENCE"
    l_med = float(median(l_vals))
    w_med = float(median(w_vals))
    delta = w_med - l_med
    if abs(delta) < 1e-9:
        return "NO"
    # Positive delta = winners have higher values for this metric
    # The caller should frame the question so YES = good for winners
    if abs(delta / max(abs(l_med), abs(w_med), 1e-9)) >= 0.10:
        return "YES" if delta > 0 else "NO"
    return "MIXED"


# ──────────────────────────────────────────────────────────────────────────────
# per-trade entry geometry
# ──────────────────────────────────────────────────────────────────────────────

def _analyze_entry(
    sig: dict,
    outcome: dict,
    m5_bars_by_ts: dict[int, dict],
    static_status: str,
    mfe_min: int,
) -> dict:
    prov       = sig["provenance"]
    m15_conf   = prov["m15_confirmation_evidence"]
    direction  = sig["direction"]
    is_long    = direction == "LONG"

    entry_price = float(sig["entry_price"])
    stop_price  = float(sig["stop_price"])
    tp1_price   = float(prov["tp1"])
    tp2_raw     = prov.get("tp2")
    tp2_price   = float(tp2_raw) if tp2_raw else None
    risk        = abs(entry_price - stop_price)

    # reference prices from provenance
    level_price      = float(prov["structural_level_price"])
    h1_conf_close    = float(prov["h1_confirmation_close"])
    h1_conf_close_ts = int(prov["h1_confirmation_close_ts"])
    retest_ts        = int(prov["retest_timestamp"])
    m15_rej_ts       = int(prov["m15_rejection_ts"])
    decision_ts      = int(prov["entry_decision_ts"])
    pullback_extreme = float(prov["pullback_extreme"])
    stop_buffer      = float(prov.get("stop_buffer_value", 0.0))
    stop_basis       = prov.get("stop_basis", "UNKNOWN")

    # M15 confirmation candle
    m15_o = float(m15_conf["open"])
    m15_h = float(m15_conf["high"])
    m15_l = float(m15_conf["low"])
    m15_c = float(m15_conf["close"])
    m15_range = m15_h - m15_l

    # ── Section A: reconstructed sequence ─────────────────────────────────────
    h1_to_retest_min          = (retest_ts - h1_conf_close_ts) // 60
    retest_to_confirmation_min = (decision_ts - retest_ts) // 60
    conf_close_to_entry_ts_min = 0  # entry IS at decision_ts (next M5 open)

    # ── Section B: entry distances ────────────────────────────────────────────
    # All computed as "distance from entry in favor of the trade direction"
    # Positive = trade has already moved this far in favor from the reference
    if is_long:
        # LONG: level is below (broken resistance = now support), entry above
        dist_from_level_price = entry_price - level_price
        # pullback_extreme = lowest retest point (closest to level from above)
        dist_from_pullback_price = entry_price - pullback_extreme
        # h1_conf_close for LONG should be above level (closed above broken resistance)
        dist_from_h1_conf_price = entry_price - h1_conf_close
    else:
        # SHORT: level is above (broken support = now resistance), entry below
        dist_from_level_price = level_price - entry_price
        # pullback_extreme = highest retest point (closest to level from below)
        dist_from_pullback_price = pullback_extreme - entry_price
        dist_from_h1_conf_price = h1_conf_close - entry_price

    dist_from_level_r    = _f(dist_from_level_price / risk)
    dist_from_pullback_r = _f(dist_from_pullback_price / risk)
    dist_from_h1_conf_r  = _f(dist_from_h1_conf_price / risk)

    # Descriptive extension classification (quantile-based; labels DIAGNOSTIC_ONLY)
    # We classify per-trade; aggregate quantile labels assigned after all trades computed

    # ── Section C: confirmation candle ────────────────────────────────────────
    body       = abs(m15_c - m15_o)
    upper_wick = m15_h - max(m15_o, m15_c)
    lower_wick = min(m15_o, m15_c) - m15_l
    body_to_range = _f(body / m15_range) if m15_range > 1e-9 else None

    # Extension by end of confirmation candle (how far conf close is from level)
    if is_long:
        fav_ext_from_level      = _f((m15_c - level_price) / risk)
        fav_ext_from_pullback   = _f((m15_c - pullback_extreme) / risk)
        entry_pos_in_range      = _f((entry_price - m15_l) / m15_range) if m15_range > 1e-9 else None
    else:
        fav_ext_from_level      = _f((level_price - m15_c) / risk)
        fav_ext_from_pullback   = _f((pullback_extreme - m15_c) / risk)
        entry_pos_in_range      = _f((m15_h - entry_price) / m15_range) if m15_range > 1e-9 else None
        # 1.0 = entered at the wick tip; 0 = entered at the candle low (for SHORT)

    # ── Section D: entry latency ──────────────────────────────────────────────
    # Actual entry bar = first M5 bar with open_ts == decision_ts
    entry_m5 = m5_bars_by_ts.get(decision_ts)
    actual_entry_price = float(entry_m5["open"]) if entry_m5 else entry_price
    entry_slippage     = abs(actual_entry_price - m15_c)  # vs M15 close
    entry_slippage_r   = _f(entry_slippage / risk)

    # ── Section E: first M5 anatomy ───────────────────────────────────────────
    first_m5_mfe_r = None
    first_m5_mae_r = None
    first_m5_close_r = None
    first_m5_spread  = None
    intrabar_order   = "INTRABAR_ORDER_UNKNOWN"

    if entry_m5:
        o5, h5, l5, c5 = (float(entry_m5[k]) for k in ("open", "high", "low", "close"))
        first_m5_spread = entry_m5.get("spread")
        if is_long:
            first_m5_mfe_r   = _f((h5 - entry_price) / risk)
            first_m5_mae_r   = _f((entry_price - l5) / risk)
            first_m5_close_r = _f((c5 - entry_price) / risk)
        else:
            first_m5_mfe_r   = _f((entry_price - l5) / risk)
            first_m5_mae_r   = _f((h5 - entry_price) / risk)
            first_m5_close_r = _f((entry_price - c5) / risk)

    # ── Section F: stop geometry ──────────────────────────────────────────────
    if is_long:
        stop_vs_pullback   = _f(pullback_extreme - stop_price)   # positive = beyond (stop below pullback low)
        stop_vs_level      = _f(level_price - stop_price)        # positive = stop is below level (beyond level)
        stop_beyond_pullback = stop_price < pullback_extreme
        stop_beyond_level    = stop_price < level_price
    else:
        stop_vs_pullback   = _f(stop_price - pullback_extreme)   # positive = beyond (stop above pullback high)
        stop_vs_level      = _f(stop_price - level_price)        # positive = stop above level
        stop_beyond_pullback = stop_price > pullback_extreme
        stop_beyond_level    = stop_price > level_price

    if stop_beyond_pullback:
        stop_relation = "BEYOND_RETEST_STRUCTURE"
    elif abs(stop_vs_pullback) < risk * 0.05:  # within 5% of risk
        stop_relation = "AT_RETEST_STRUCTURE"
    else:
        stop_relation = "INSIDE_RETEST_STRUCTURE"

    # ── Section G: spread ─────────────────────────────────────────────────────
    spread_raw   = first_m5_spread or 0
    spread_price = _f(spread_raw * POINT)
    spread_pct_stop = _f(spread_price / risk * 100) if risk > 1e-9 else None
    tp1_dist     = abs(tp1_price - entry_price)
    spread_pct_tp1 = _f(spread_price / tp1_dist * 100) if tp1_dist > 1e-9 else None

    return {
        "signal_id": sig["signal_id"],
        "direction": direction,
        "static_status": static_status,
        "mfe_on_first_bar": mfe_min == 0,
        # ── Section A ──────────────────────────────────────────────────────────
        "h1_confirmation_close_ts": h1_conf_close_ts,
        "h1_key_level": _f(level_price),
        "h1_confirmation_close_price": _f(h1_conf_close),
        "m15_retest_first_ts": retest_ts,
        "m15_retest_extreme": _f(pullback_extreme),
        "m15_rejection_ts": m15_rej_ts,
        "m15_rejection_type": prov.get("confirmation_type"),
        "m15_rejection_open": _f(m15_o),
        "m15_rejection_high": _f(m15_h),
        "m15_rejection_low": _f(m15_l),
        "m15_rejection_close": _f(m15_c),
        "entry_decision_ts": decision_ts,
        "entry_price": _f(entry_price),
        "initial_stop": _f(stop_price),
        "tp1": _f(tp1_price),
        # ── Section B ──────────────────────────────────────────────────────────
        "entry_dist_from_key_level_price": _f(dist_from_level_price),
        "entry_dist_from_key_level_r": dist_from_level_r,
        "entry_dist_from_retest_extreme_price": _f(dist_from_pullback_price),
        "entry_dist_from_retest_extreme_r": dist_from_pullback_r,
        "entry_dist_from_h1_conf_close_price": _f(dist_from_h1_conf_price),
        "entry_dist_from_h1_conf_close_r": dist_from_h1_conf_r,
        # ── Section C ──────────────────────────────────────────────────────────
        "candle_range": _f(m15_range),
        "body_size": _f(body),
        "upper_wick": _f(upper_wick),
        "lower_wick": _f(lower_wick),
        "body_to_range_ratio": body_to_range,
        "favorable_extension_from_level_r": fav_ext_from_level,
        "favorable_extension_from_pullback_r": fav_ext_from_pullback,
        "entry_position_in_confirmation_range": entry_pos_in_range,
        # ── Section D ──────────────────────────────────────────────────────────
        "h1_close_to_retest_minutes": h1_to_retest_min,
        "retest_to_confirmation_close_minutes": retest_to_confirmation_min,
        "confirmation_close_to_entry_minutes": conf_close_to_entry_ts_min,
        "m15_conf_close_price": _f(m15_c),
        "actual_entry_vs_m15_close_slippage": _f(entry_slippage),
        "entry_slippage_r": entry_slippage_r,
        # ── Section E ──────────────────────────────────────────────────────────
        "first_m5_mfe_r": first_m5_mfe_r,
        "first_m5_mae_r": first_m5_mae_r,
        "first_m5_close_r": first_m5_close_r,
        "first_m5_spread_raw": spread_raw,
        "intrabar_order": intrabar_order,
        # ── Section F ──────────────────────────────────────────────────────────
        "stop_vs_pullback_extreme_price": stop_vs_pullback,
        "stop_vs_key_level_price": stop_vs_level,
        "stop_beyond_pullback_extreme": stop_beyond_pullback,
        "stop_beyond_key_level": stop_beyond_level,
        "stop_relation": stop_relation,
        "stop_buffer_value": stop_buffer,
        "stop_basis": stop_basis,
        # ── Section G ──────────────────────────────────────────────────────────
        "spread_price": spread_price,
        "spread_pct_of_stop_distance": spread_pct_stop,
        "spread_pct_of_tp1_distance": spread_pct_tp1,
        "risk_points": _f(risk),
        "tp1_distance_points": _f(tp1_dist),
    }


# ──────────────────────────────────────────────────────────────────────────────
# aggregate helpers
# ──────────────────────────────────────────────────────────────────────────────

def _agg_field(recs: list[dict], field: str) -> dict:
    vals = [r[field] for r in recs if r.get(field) is not None]
    return {
        "median": _med(vals),
        "p25": _p25(vals),
        "p75": _p75(vals),
        "n": len(vals),
    }


def _compare(loser_recs: list[dict], winner_recs: list[dict], field: str) -> dict:
    return {
        "losers": _agg_field(loser_recs, field),
        "winners": _agg_field(winner_recs, field),
    }


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discovery",     type=Path, default=DISCOVERY_PATH)
    parser.add_argument("--post-entry",    type=Path, default=POST_ENTRY_PATH)
    parser.add_argument("--canonical",     type=Path, default=CANONICAL_PATH)
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    raw_can   = args.canonical.read_bytes()
    source_fp = hashlib.sha256(raw_can).hexdigest()
    canonical = json.loads(raw_can)
    disc      = json.loads(args.discovery.read_text())
    pe        = json.loads(args.post_entry.read_text())

    # index M5 bars by open_ts (for first-bar lookup)
    m5_by_ts = {
        int(b["time"]): b
        for b in canonical["m5_bars"]
        if int(b["time"]) <= DISCOVERY_END
    }

    outcomes_by_sid = {o["signal_id"]: o for o in disc["outcomes"]}
    pe_by_sid       = {t["signal_id"]: t for t in pe["trades"]}

    # ── per-trade analysis ─────────────────────────────────────────────────────
    records: list[dict] = []
    for sig in disc["signals"]:
        sid        = sig["signal_id"]
        outcome    = outcomes_by_sid[sid]
        pe_trade   = pe_by_sid[sid]
        mfe_min    = pe_trade.get("time_to_mfe_minutes") or 0
        status     = outcome["status"]
        rec = _analyze_entry(sig, outcome, m5_by_ts, status, mfe_min)
        # attach SL timing for stopped trades
        if status == "STOPPED":
            exit_ts = int(outcome["exit_timestamp"])
            decision_ts = int(sig["provenance"]["entry_decision_ts"])
            rec["sl_minutes_from_entry"] = (exit_ts - decision_ts) // 60
        else:
            rec["sl_minutes_from_entry"] = None
        records.append(rec)

    losers      = [r for r in records if r["static_status"] == "STOPPED"]
    winners     = [r for r in records if r["static_status"] == "TARGET_HIT"]
    imm_losers  = [r for r in losers  if r["mfe_on_first_bar"]]

    n_tot = len(records)
    n_los = len(losers)
    n_win = len(winners)
    n_imm = len(imm_losers)

    # ── Section B/C/D/F/G comparison tables ───────────────────────────────────
    section_b = {
        k: _compare(losers, winners, k)
        for k in [
            "entry_dist_from_key_level_r",
            "entry_dist_from_retest_extreme_r",
            "entry_dist_from_h1_conf_close_r",
        ]
    }
    section_c = {
        k: _compare(losers, winners, k)
        for k in [
            "body_to_range_ratio",
            "favorable_extension_from_level_r",
            "favorable_extension_from_pullback_r",
            "entry_position_in_confirmation_range",
            "candle_range",
        ]
    }
    section_d = {
        k: _compare(losers, winners, k)
        for k in [
            "h1_close_to_retest_minutes",
            "retest_to_confirmation_close_minutes",
            "entry_slippage_r",
        ]
    }
    section_e = {
        k: _compare(losers, winners, k)
        for k in ["first_m5_mfe_r", "first_m5_mae_r", "first_m5_close_r"]
    }
    # Stopped within first M5 bar (sl_minutes == 5)
    section_e["losers_stopped_within_first_m5"] = sum(
        1 for r in losers if r.get("sl_minutes_from_entry") is not None and r["sl_minutes_from_entry"] <= 5
    )
    # Stopped before next M15 close = within first M15 bar = sl_minutes <= 15
    section_e["losers_stopped_before_next_m15_close"] = sum(
        1 for r in losers if r.get("sl_minutes_from_entry") is not None and r["sl_minutes_from_entry"] <= 15
    )
    section_e["immediate_peak_losers_stopped_before_m15_close"] = sum(
        1 for r in imm_losers if r.get("sl_minutes_from_entry") is not None and r["sl_minutes_from_entry"] <= 15
    )
    section_e["winners_stopped_within_first_m5"] = 0  # winners by definition are not stopped
    from collections import Counter
    loser_sl_bins = Counter()
    for r in losers:
        # sl_minutes estimated from SL within M5 bars — use first_m5_mae_r >= 1.0
        if r["first_m5_mae_r"] is not None and r["first_m5_mae_r"] >= 1.0:
            loser_sl_bins["first_m5"] += 1
        elif r["first_m5_close_r"] is not None and r["first_m5_close_r"] < -0.5:
            loser_sl_bins["deteriorating_first_m5"] += 1
        else:
            loser_sl_bins["survived_first_m5"] += 1
    section_e["loser_sl_timing_bins"] = dict(loser_sl_bins)

    section_f = {
        "stop_relation_distribution": {
            "BEYOND_RETEST_STRUCTURE_losers":  sum(1 for r in losers  if r["stop_relation"] == "BEYOND_RETEST_STRUCTURE"),
            "BEYOND_RETEST_STRUCTURE_winners": sum(1 for r in winners if r["stop_relation"] == "BEYOND_RETEST_STRUCTURE"),
            "AT_RETEST_STRUCTURE_losers":  sum(1 for r in losers  if r["stop_relation"] == "AT_RETEST_STRUCTURE"),
            "AT_RETEST_STRUCTURE_winners": sum(1 for r in winners if r["stop_relation"] == "AT_RETEST_STRUCTURE"),
            "INSIDE_RETEST_STRUCTURE_losers":  sum(1 for r in losers  if r["stop_relation"] == "INSIDE_RETEST_STRUCTURE"),
            "INSIDE_RETEST_STRUCTURE_winners": sum(1 for r in winners if r["stop_relation"] == "INSIDE_RETEST_STRUCTURE"),
        },
        "stop_vs_pullback_extreme": _compare(losers, winners, "stop_vs_pullback_extreme_price"),
        "stop_vs_key_level": _compare(losers, winners, "stop_vs_key_level_price"),
    }

    section_g = {
        k: _compare(losers, winners, k)
        for k in ["spread_pct_of_stop_distance", "spread_pct_of_tp1_distance"]
    }
    section_g["high_spread_trades"] = [
        {
            "signal_id": r["signal_id"],
            "spread_pct_stop": r["spread_pct_of_stop_distance"],
            "spread_pct_tp1":  r["spread_pct_of_tp1_distance"],
            "static_status":   r["static_status"],
        }
        for r in records
        if r["spread_pct_of_stop_distance"] is not None and r["spread_pct_of_stop_distance"] > 10.0
    ]

    # ── Section H: immediate-peak cohort vs winners ────────────────────────────
    section_h = {
        "immediate_peak_losers_count": n_imm,
        "non_immediate_peak_losers_count": n_los - n_imm,
        "winners_count": n_win,
        "immediate_peak_details": [
            {
                k: r[k]
                for k in [
                    "signal_id", "entry_price", "h1_key_level",
                    "m15_retest_extreme", "m15_rejection_close",
                    "initial_stop", "tp1",
                    "entry_dist_from_key_level_r",
                    "favorable_extension_from_level_r",
                    "first_m5_mfe_r", "first_m5_mae_r",
                    "spread_pct_of_stop_distance",
                ]
            }
            for r in imm_losers
        ],
        "comparison_immediate_vs_winners": {
            k: {
                "immediate_peak_losers": _agg_field(imm_losers, k),
                "all_winners": _agg_field(winners, k),
            }
            for k in [
                "entry_dist_from_key_level_r",
                "favorable_extension_from_level_r",
                "entry_position_in_confirmation_range",
                "spread_pct_of_stop_distance",
            ]
        },
    }

    # ── Section I: winner control answers ─────────────────────────────────────
    # Winners enter CLOSER to key level → YES if winner dist_from_level_r is smaller
    w_level_med = _med([r["entry_dist_from_key_level_r"] for r in winners])
    l_level_med = _med([r["entry_dist_from_key_level_r"] for r in losers])
    closer = "YES" if (w_level_med is not None and l_level_med is not None and w_level_med < l_level_med) else "NO"
    # with how much difference?
    level_diff = round(l_level_med - w_level_med, 4) if (l_level_med and w_level_med) else None

    w_conf_ext  = _med([r["favorable_extension_from_level_r"] for r in winners])
    l_conf_ext  = _med([r["favorable_extension_from_level_r"] for r in losers])
    smaller_ext = "YES" if (w_conf_ext and l_conf_ext and w_conf_ext < l_conf_ext) else "MIXED" if w_conf_ext else "INSUFFICIENT_EVIDENCE"

    w_pullback  = _med([r["entry_dist_from_retest_extreme_r"] for r in winners])
    l_pullback  = _med([r["entry_dist_from_retest_extreme_r"] for r in losers])
    diff_retest = "MIXED"
    if w_pullback is not None and l_pullback is not None:
        delta_pct = abs(w_pullback - l_pullback) / max(abs(l_pullback), 1e-9)
        diff_retest = "YES" if delta_pct >= 0.10 else "NO" if delta_pct < 0.05 else "MIXED"

    w_stop  = _med([r["stop_vs_pullback_extreme_price"] for r in winners])
    l_stop  = _med([r["stop_vs_pullback_extreme_price"] for r in losers])
    diff_stop = "YES" if (w_stop and l_stop and abs(w_stop - l_stop) / max(abs(l_stop), 1e-9) >= 0.10) else "NO"

    w_spread = _med([r["spread_pct_of_stop_distance"] for r in winners])
    l_spread = _med([r["spread_pct_of_stop_distance"] for r in losers])
    lower_spread = "YES" if (w_spread and l_spread and w_spread < l_spread) else "MIXED"

    w_survive = sum(1 for r in winners if r["first_m5_mae_r"] is not None and r["first_m5_mae_r"] < 1.0)
    l_survive = sum(1 for r in losers  if r["first_m5_mae_r"] is not None and r["first_m5_mae_r"] < 1.0)
    survive_pct_w = _pct(w_survive, n_win)
    survive_pct_l = _pct(l_survive, n_los)
    winners_survive_m5_more = "YES" if survive_pct_w > survive_pct_l + 10 else "MIXED"

    section_i = {
        "DO_WINNERS_ENTER_CLOSER_TO_KEY_LEVEL": closer,
        "winner_entry_dist_from_level_r_median": w_level_med,
        "loser_entry_dist_from_level_r_median": l_level_med,
        "loser_minus_winner_dist_from_level_r": level_diff,
        "DO_WINNERS_HAVE_SMALLER_CONFIRMATION_EXTENSION": smaller_ext,
        "winner_fav_ext_from_level_r_median": w_conf_ext,
        "loser_fav_ext_from_level_r_median": l_conf_ext,
        "DO_WINNERS_HAVE_DIFFERENT_RETEST_DEPTH": diff_retest,
        "winner_dist_from_pullback_r_median": w_pullback,
        "loser_dist_from_pullback_r_median": l_pullback,
        "DO_WINNERS_HAVE_DIFFERENT_STOP_GEOMETRY": diff_stop,
        "winner_stop_vs_pullback_median": w_stop,
        "loser_stop_vs_pullback_median": l_stop,
        "DO_WINNERS_HAVE_LOWER_SPREAD_RELATIVE_TO_STOP": lower_spread,
        "winner_spread_pct_stop_median": w_spread,
        "loser_spread_pct_stop_median": l_spread,
        "DO_WINNERS_SURVIVE_FIRST_M15_MORE_OFTEN": winners_survive_m5_more,
        "pct_winners_survive_first_m5": survive_pct_w,
        "pct_losers_survive_first_m5": survive_pct_l,
    }

    # ── Section J: core diagnostic ─────────────────────────────────────────────
    # Evidence assessment:
    # - Entry extended from level: compare dist_from_level_r losers vs winners
    # - Confirmation extension: how much of stop is "used up" at confirmation close
    # - Stop geometry: consistent stop_basis, all BEYOND_RETEST_STRUCTURE
    # - Execution cost: spread is small (<10% of stop)
    # - Latency: confirmation_close_to_entry is 0 (entry IS at next M5 open)

    all_fav_ext = [r["favorable_extension_from_level_r"] for r in records if r["favorable_extension_from_level_r"] is not None]
    all_dist_level = [r["entry_dist_from_key_level_r"] for r in records if r["entry_dist_from_key_level_r"] is not None]

    med_fav_ext_losers = l_conf_ext
    med_fav_ext_winners = w_conf_ext

    # Most losers have very high favorable extension at confirmation (price far from level by entry)
    # Most stop placements are BEYOND_RETEST_STRUCTURE
    # Spread is small relative to stop and TP1
    all_beyond = all(r["stop_relation"] == "BEYOND_RETEST_STRUCTURE" for r in records)
    high_spread = any(r["spread_pct_of_stop_distance"] is not None and r["spread_pct_of_stop_distance"] > 15 for r in records)

    # Determine primary hypothesis.
    # Evaluate Section I answers: if all are NO or MIXED → no single geometry defect.
    # A YES in any critical dimension would point to a specific hypothesis.
    critical_section_i = [
        closer,           # DO_WINNERS_ENTER_CLOSER_TO_KEY_LEVEL
        smaller_ext,      # DO_WINNERS_HAVE_SMALLER_CONFIRMATION_EXTENSION
        diff_retest,      # DO_WINNERS_HAVE_DIFFERENT_RETEST_DEPTH
        diff_stop,        # DO_WINNERS_HAVE_DIFFERENT_STOP_GEOMETRY
        lower_spread,     # DO_WINNERS_HAVE_LOWER_SPREAD_RELATIVE_TO_STOP
    ]
    yes_count = sum(1 for v in critical_section_i if v == "YES")

    if yes_count == 0:
        # No geometry metric separates winners from losers
        primary_hypothesis = "NO_SINGLE_DOMINANT_ENTRY_DEFECT"
    elif closer == "YES" and med_fav_ext_losers is not None and med_fav_ext_winners is not None and med_fav_ext_winners < med_fav_ext_losers:
        primary_hypothesis = "ENTRY_TOO_EXTENDED_AFTER_CONFIRMATION"
    elif diff_stop == "YES":
        primary_hypothesis = "STOP_GEOMETRY_PROBLEM"
    elif lower_spread == "YES":
        primary_hypothesis = "EXECUTION_COST_GEOMETRY_PROBLEM"
    else:
        primary_hypothesis = "NO_SINGLE_DOMINANT_ENTRY_DEFECT"

    entry_edge = "PROMISING_BUT_MANAGEMENT_SENSITIVE"  # from prior study
    timing_quality = (
        "POOR_IMMEDIATE_PEAK_DOMINANT"
        if n_imm / n_los >= 0.5 else "MIXED"
    )
    stop_quality = "CONSISTENT_BEYOND_RETEST" if all_beyond else "MIXED"
    latency_concern = "LOW"  # entry is at next M5 open after M15 close; no additional latency
    spread_concern = "LOW" if not high_spread else "MODERATE"

    section_j = {
        "ENTRY_DIRECTIONAL_EDGE": entry_edge,
        "ENTRY_TIMING_QUALITY": timing_quality,
        "STOP_GEOMETRY_QUALITY": stop_quality,
        "EXECUTION_LATENCY_CONCERN": latency_concern,
        "SPREAD_GEOMETRY_CONCERN": spread_concern,
        "ENTRY_FAILURE_PRIMARY_HYPOTHESIS": primary_hypothesis,
        "supporting_evidence": {
            "confirmation_extension_losers_median_r": med_fav_ext_losers,
            "confirmation_extension_winners_median_r": med_fav_ext_winners,
            "extension_delta_losers_minus_winners": _f(
                (med_fav_ext_losers or 0) - (med_fav_ext_winners or 0)
            ),
            "immediate_peak_losers_pct": _pct(n_imm, n_los),
            "all_stops_beyond_retest": all_beyond,
            "high_spread_cases": len(section_g["high_spread_trades"]),
        },
    }

    # ── build artifact ─────────────────────────────────────────────────────────
    artifact = {
        "study_type": "ENTRY_TIMING_AND_GEOMETRY_AUDIT",
        "base_commits": {
            "v3_entry": "c82d290",
            "post_entry": "1e321fe",
            "exit_counterfactual": "d9264a7",
            "failure_anatomy": "827a95f",
        },
        "source_sha256": source_fp,
        "total_trades": n_tot,
        "section_a_entry_sequences": [
            {k: v for k, v in r.items() if k in (
                "signal_id", "direction", "static_status",
                "h1_confirmation_close_ts", "h1_key_level", "h1_confirmation_close_price",
                "m15_retest_first_ts", "m15_retest_extreme",
                "m15_rejection_ts", "m15_rejection_type",
                "m15_rejection_open", "m15_rejection_high", "m15_rejection_low", "m15_rejection_close",
                "entry_decision_ts", "entry_price", "initial_stop", "tp1",
            )}
            for r in records
        ],
        "section_b_entry_distances": section_b,
        "section_c_confirmation_candle": section_c,
        "section_d_entry_latency": section_d,
        "section_e_first_m5_anatomy": section_e,
        "section_f_stop_geometry": section_f,
        "section_g_spread": section_g,
        "section_h_immediate_peak_cohort": section_h,
        "section_i_winner_control": section_i,
        "section_j_core_diagnostic": section_j,
        "per_trade_audit": records,
        "no_entry_rule_changed": True,
        "no_stop_rule_changed": True,
        "no_target_rule_changed": True,
        "no_management_policy_created": True,
        "no_exit_rule_optimized": True,
        "no_parameter_search": True,
        "validation_outcomes_accessed": False,
        "production_changed": False,
        "broker_writes": 0,
    }

    ab = json.dumps(artifact, sort_keys=True).encode()
    fp = hashlib.sha256(ab).hexdigest()
    artifact["artifact_fingerprint"] = fp

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    out = args.artifact_root / "kojo_v3_entry_geometry_audit.json"
    out.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"artifact: {out}", file=sys.stderr)

    # ── required report ────────────────────────────────────────────────────────
    print(json.dumps({
        "AUDIT_ARTIFACT": str(out),
        "AUDIT_ARTIFACT_FINGERPRINT": fp,
        "TOTAL_TRADES": n_tot,
        "STATIC_WINS": n_win,
        "STATIC_LOSSES": n_los,
        "IMMEDIATE_PEAK_LOSERS": n_imm,
        # Section B
        "ENTRY_DIST_FROM_LEVEL_R_losers":  section_b["entry_dist_from_key_level_r"]["losers"],
        "ENTRY_DIST_FROM_LEVEL_R_winners": section_b["entry_dist_from_key_level_r"]["winners"],
        "ENTRY_DIST_FROM_PULLBACK_R_losers":  section_b["entry_dist_from_retest_extreme_r"]["losers"],
        "ENTRY_DIST_FROM_PULLBACK_R_winners": section_b["entry_dist_from_retest_extreme_r"]["winners"],
        # Section C
        "FAV_EXT_FROM_LEVEL_R_AT_CONF_losers":  section_c["favorable_extension_from_level_r"]["losers"],
        "FAV_EXT_FROM_LEVEL_R_AT_CONF_winners": section_c["favorable_extension_from_level_r"]["winners"],
        "ENTRY_POSITION_IN_RANGE_losers":  section_c["entry_position_in_confirmation_range"]["losers"],
        "ENTRY_POSITION_IN_RANGE_winners": section_c["entry_position_in_confirmation_range"]["winners"],
        # Section D
        "H1_CLOSE_TO_RETEST_MIN_losers":  section_d["h1_close_to_retest_minutes"]["losers"],
        "H1_CLOSE_TO_RETEST_MIN_winners": section_d["h1_close_to_retest_minutes"]["winners"],
        "RETEST_TO_CONF_MIN_losers":  section_d["retest_to_confirmation_close_minutes"]["losers"],
        "RETEST_TO_CONF_MIN_winners": section_d["retest_to_confirmation_close_minutes"]["winners"],
        # Section E
        "FIRST_M5_MFE_R_losers":  section_e["first_m5_mfe_r"]["losers"],
        "FIRST_M5_MFE_R_winners": section_e["first_m5_mfe_r"]["winners"],
        "FIRST_M5_MAE_R_losers":  section_e["first_m5_mae_r"]["losers"],
        "FIRST_M5_MAE_R_winners": section_e["first_m5_mae_r"]["winners"],
        "LOSERS_STOPPED_WITHIN_FIRST_M5": section_e["losers_stopped_within_first_m5"],
        "LOSERS_STOPPED_BEFORE_NEXT_M15_CLOSE": section_e["losers_stopped_before_next_m15_close"],
        "IMMEDIATE_PEAK_LOSERS_STOPPED_BEFORE_M15_CLOSE": section_e["immediate_peak_losers_stopped_before_m15_close"],
        "WINNERS_STOPPED_WITHIN_FIRST_M5": section_e["winners_stopped_within_first_m5"],
        # Section F
        "STOP_RELATION_DIST": section_f["stop_relation_distribution"],
        # Section G
        "SPREAD_PCT_STOP_losers":  section_g["spread_pct_of_stop_distance"]["losers"],
        "SPREAD_PCT_STOP_winners": section_g["spread_pct_of_stop_distance"]["winners"],
        "HIGH_SPREAD_TRADES": section_g["high_spread_trades"],
        # Section I
        **{k: v for k, v in section_i.items()},
        # Section J
        **section_j,
        # Safety
        "ENTRY_RULE_CHANGED": False,
        "STOP_RULE_CHANGED": False,
        "TARGET_RULE_CHANGED": False,
        "TRADE_MANAGER_POLICY_CREATED": False,
        "EXIT_RULE_OPTIMIZED": False,
        "PARAMETER_SEARCH": False,
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "PRODUCTION_CHANGED": False,
        "BROKER_WRITES": 0,
        "READY_FOR_VALIDATION": False,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
