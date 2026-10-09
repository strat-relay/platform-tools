"""Read-only counterfactual study: EXIT_ON_M5_CLOSE_THROUGH_M15_RETEST_LEVEL.

Tests whether exiting at the first completed M5 close through the frozen
M15 retest level (pullback extreme) would have improved the frozen 38-trade
V3 discovery dataset.

Base commits:
    V3_ENTRY             = c82d290
    POST_ENTRY           = 1e321fe
    EXIT_COUNTERFACTUAL  = d9264a7
    FAILURE_ANATOMY      = 827a95f
    ENTRY_GEOMETRY_AUDIT = 3351fb3
    M5_MICROSTRUCTURE    = afc1cca

Safety constraints:
    BROKER_WRITES=0
    VALIDATION_OUTCOMES_ACCESSED=false
    ENTRY_EVALUATOR_CHANGED=false
    STOP_EVALUATOR_CHANGED=false
    TARGET_EVALUATOR_CHANGED=false
    TRADE_MANAGER_POLICY_CREATED=false
    EXIT_RULE_OPTIMIZED=false
    THRESHOLD_TUNED=false
    PARAMETER_SEARCH=false
    PRODUCTION_CHANGED=false
    READY_FOR_VALIDATION=false

Pricing convention:
    Exit price = CLOSE of the triggering M5 bar (completed bar only).
    If SL is hit intrabar in the same M5 bar, conservative SL policy governs
    (exit at -1.0R, not the close).
    If TP1 is hit intrabar before the M5 close, TP1 governs.
    Scan window: M5 bars whose open_ts is strictly before the terminal M15 bar
    (the M15 bar that closes at entry_ts + sl_minutes * 60).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from statistics import median, quantiles
from typing import Any

V3_ENTRY_COMMIT             = "c82d290"
POST_ENTRY_COMMIT           = "1e321fe"
EXIT_COUNTERFACTUAL_COMMIT  = "d9264a7"
FAILURE_ANATOMY_COMMIT      = "827a95f"
ENTRY_GEOMETRY_COMMIT       = "3351fb3"
M5_MICROSTRUCTURE_COMMIT    = "afc1cca"

M5_SECONDS  = 300
M15_SECONDS = 900

ENTRY_GEOMETRY_PATH = Path("artifacts/research/kojo_v3_entry_geometry_audit.json")
POST_ENTRY_PATH     = Path("artifacts/research/kojo_v3_post_entry_behavior.json")
FAILURE_ANAT_PATH   = Path("artifacts/research/kojo_v3_failure_anatomy.json")
M5_MICRO_PATH       = Path("artifacts/research/kojo_v3_early_post_entry_microstructure.json")


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 1) if d else 0.0


def _med(vals: list) -> float | None:
    return round(median(vals), 4) if vals else None


def _p25(vals: list) -> float | None:
    if len(vals) < 2:
        return round(vals[0], 4) if vals else None
    return round(quantiles(vals, n=4)[0], 4)


def _p75(vals: list) -> float | None:
    if len(vals) < 2:
        return round(vals[0], 4) if vals else None
    return round(quantiles(vals, n=4)[2], 4)


def _expectancy(rs: list[float]) -> float | None:
    return round(sum(rs) / len(rs), 4) if rs else None


def _profit_factor(rs: list[float]) -> float | None:
    gains  = sum(r for r in rs if r > 0)
    losses = sum(abs(r) for r in rs if r < 0)
    return round(gains / losses, 4) if losses > 1e-9 else None


def _max_dd(rs: list[float]) -> float:
    peak = equity = 0.0
    mdd  = 0.0
    for r in rs:
        equity += r
        if equity > peak:
            peak = equity
        mdd = max(mdd, peak - equity)
    return round(mdd, 4)


# ──────────────────────────────────────────────────────────────────────────────
# per-trade counterfactual logic
# ──────────────────────────────────────────────────────────────────────────────

def _find_m5_retest_exit(
    direction:   str,
    entry:       float,
    stop:        float,
    tp1:         float,
    retest:      float,
    risk:        float,
    entry_ts:    int,
    sl_minutes:  int,        # M15-resolution terminal time
    m5_by_ts:    dict[int, dict],
) -> dict[str, Any]:
    """Scan M5 bars for first close through the M15 retest level.

    Scan window: M5 bars whose open_ts < terminal_m15_open.
    terminal_m15_open = entry_ts + (sl_minutes - 15) * 60.
    For sl_minutes == 15, no bars are available (entire first M15 bar is
    already the terminal bar).

    Conservative intrabar policy (within any scanned M5 bar):
      - If SL adverse extreme is hit intrabar AND retest close fires → SL wins (-1.0R).
      - If TP1 favorable extreme is hit intrabar AND retest close fires → TP1 wins.
      - Otherwise → retest close exit at close price.
    """
    terminal_m15_open = entry_ts + (sl_minutes - M15_SECONDS // 60) * 60

    ts = entry_ts
    while ts < terminal_m15_open:
        bar = m5_by_ts.get(ts)
        if bar is None:
            ts += M5_SECONDS
            continue

        o   = float(bar["open"])
        h   = float(bar["high"])
        lo  = float(bar["low"])
        c   = float(bar["close"])

        # SL intrabar: conservative — assume SL hit before bar close
        sl_intrabar  = (h >= stop) if direction == "SHORT" else (lo <= stop)
        # TP1 intrabar: TP1 hit before bar close (favorable extreme reached)
        tp1_intrabar = (lo <= tp1) if direction == "SHORT" else (h >= tp1)

        # Retest close signal: M5 bar close crosses retest level adversely
        retest_fired = (c > retest) if direction == "SHORT" else (c < retest)

        if retest_fired:
            elapsed_min = (ts - entry_ts) // 60
            if sl_intrabar:
                # SL was hit intrabar; conservative policy → SL governs
                return {
                    "fired":              True,
                    "exit_ts":            ts,
                    "exit_bar_open_ts":   ts,
                    "exit_price":         None,
                    "exit_r":             -1.0,
                    "close_r":            round((entry - c) / risk if direction == "SHORT"
                                               else (c - entry) / risk, 4),
                    "capped_to_sl":       True,
                    "tp1_priority":       False,
                    "minutes_from_entry": elapsed_min,
                    "event_before_sl":    False,   # SL intrabar = SL is contemporaneous
                }
            if tp1_intrabar:
                # TP1 hit intrabar; TP1 governs
                tp1_r = round(
                    (entry - tp1) / risk if direction == "SHORT" else (tp1 - entry) / risk,
                    4,
                )
                return {
                    "fired":              True,
                    "exit_ts":            ts,
                    "exit_bar_open_ts":   ts,
                    "exit_price":         tp1,
                    "exit_r":             tp1_r,
                    "close_r":            round((entry - c) / risk if direction == "SHORT"
                                               else (c - entry) / risk, 4),
                    "capped_to_sl":       False,
                    "tp1_priority":       True,
                    "minutes_from_entry": elapsed_min,
                    "event_before_sl":    True,
                }
            # Clean retest close exit
            close_r = round(
                (entry - c) / risk if direction == "SHORT" else (c - entry) / risk,
                4,
            )
            return {
                "fired":              True,
                "exit_ts":            ts,
                "exit_bar_open_ts":   ts,
                "exit_price":         round(c, 3),
                "exit_r":             close_r,
                "close_r":            close_r,
                "capped_to_sl":       False,
                "tp1_priority":       False,
                "minutes_from_entry": elapsed_min,
                "event_before_sl":    True,
            }

        ts += M5_SECONDS

    # Signal never fired before the terminal M15 bar
    return {
        "fired":              False,
        "exit_ts":            None,
        "exit_price":         None,
        "exit_r":             None,
        "capped_to_sl":       False,
        "tp1_priority":       False,
        "minutes_from_entry": None,
        "event_before_sl":    False,
    }


def _classify(static_r: float, cf_r: float, fired: bool) -> str:
    if not fired:
        return "NOT_TRIGGERED"
    delta = cf_r - static_r
    if delta > 0.01:
        return "IMPROVED"
    if delta < -0.01:
        return "DEGRADED"
    return "NEUTRAL"


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="V3 M5 retest-exit counterfactual")
    parser.add_argument("--canonical", type=Path,
        default=Path("/private/tmp/claude-501/-Users-caleb-mt5-native-bridge"
                     "/4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"))
    parser.add_argument("--entry-geometry",  type=Path, default=ENTRY_GEOMETRY_PATH)
    parser.add_argument("--post-entry",      type=Path, default=POST_ENTRY_PATH)
    parser.add_argument("--failure-anatomy", type=Path, default=FAILURE_ANAT_PATH)
    parser.add_argument("--artifact-root",   type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    # ── load ─────────────────────────────────────────────────────────────────
    canonical_bytes = args.canonical.read_bytes()
    canonical_sha   = hashlib.sha256(canonical_bytes).hexdigest()
    canonical       = json.loads(canonical_bytes)
    m5_by_ts: dict[int, dict] = {int(b["time"]): b for b in canonical["m5_bars"]}
    print(f"M5 bars: {len(m5_by_ts)}", file=sys.stderr)

    eg = json.loads(args.entry_geometry.read_bytes())
    pe = json.loads(args.post_entry.read_bytes())
    fa = json.loads(args.failure_anatomy.read_bytes())

    eg_by_sig = {t["signal_id"]: t for t in eg["per_trade_audit"]}
    pe_by_sig = {t["signal_id"]: t for t in pe["trades"]}
    fa_by_sig = {t["signal_id"]: t for t in fa["per_trade_anatomy"]}

    ip_sig_ids: set[str] = {
        d["signal_id"]
        for d in eg["section_h_immediate_peak_cohort"]["immediate_peak_details"]
    }

    # ── per-trade ─────────────────────────────────────────────────────────────
    trade_results: list[dict] = []

    for sig_id, eg_t in eg_by_sig.items():
        pe_t = pe_by_sig[sig_id]
        fa_t = fa_by_sig[sig_id]

        direction  = eg_t["direction"]
        entry      = float(eg_t["entry_price"])
        stop       = float(eg_t["initial_stop"])
        tp1        = float(eg_t["tp1"])
        retest     = float(eg_t["m15_retest_extreme"])
        risk       = abs(entry - stop)
        entry_ts   = int(eg_t["entry_decision_ts"])
        sl_minutes = fa_t["sl_minutes"]
        static_r   = float(pe_t["static_realized_r"])
        is_winner  = eg_t["static_status"] == "TARGET_HIT"
        is_ip      = sig_id in ip_sig_ids

        cf = _find_m5_retest_exit(
            direction, entry, stop, tp1, retest, risk,
            entry_ts, sl_minutes, m5_by_ts,
        )

        if cf["fired"] and not cf["tp1_priority"]:
            cf_r = cf["exit_r"]
        elif cf["fired"] and cf["tp1_priority"]:
            cf_r = cf["exit_r"]  # TP1 hit → same as static TP1 outcome
        else:
            cf_r = static_r  # not triggered → static result

        classification = _classify(static_r, cf_r, cf["fired"])
        delta_r = round(cf_r - static_r, 4)

        trade_results.append({
            "signal_id":          sig_id,
            "direction":          direction,
            "static_status":      eg_t["static_status"],
            "is_winner":          is_winner,
            "is_immediate_peak":  is_ip,
            "entry_ts":           entry_ts,
            "entry_price":        round(entry, 3),
            "initial_stop":       round(stop, 3),
            "tp1_price":          round(tp1, 3),
            "m15_retest_level":   round(retest, 3),
            "risk_points":        round(risk, 3),
            "sl_minutes":         sl_minutes,
            "terminal_m15_open":  entry_ts + (sl_minutes - 15) * 60,
            "scan_window_m5_bars": max(0, (sl_minutes - 15) * 60 // M5_SECONDS),
            "static_r":           round(static_r, 4),
            "cf_fired":           cf["fired"],
            "cf_exit_ts":         cf["exit_ts"],
            "cf_exit_price":      cf["exit_price"],
            "cf_exit_r":          round(cf_r, 4),
            "cf_close_r":         cf.get("close_r"),
            "cf_capped_to_sl":    cf["capped_to_sl"],
            "cf_tp1_priority":    cf["tp1_priority"],
            "cf_minutes_from_entry": cf["minutes_from_entry"],
            "event_before_sl":    cf["event_before_sl"],
            "cf_delta_r":         delta_r,
            "classification":     classification,
        })

    # ── aggregate ─────────────────────────────────────────────────────────────
    losers  = [t for t in trade_results if not t["is_winner"]]
    winners = [t for t in trade_results if t["is_winner"]]
    ip_losers = [t for t in losers if t["is_immediate_peak"]]

    def _agg(trades: list[dict], rs_key: str) -> dict:
        rs = [t[rs_key] for t in trades]
        n  = len(rs)
        wins   = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        return {
            "n":              n,
            "win_count":      len(wins),
            "loss_count":     len(losses),
            "win_rate_pct":   _pct(len(wins), n),
            "expectancy_r":   _expectancy(rs),
            "total_r":        round(sum(rs), 4),
            "profit_factor":  _profit_factor(rs),
            "max_dd_r":       _max_dd(rs),
        }

    static_agg = _agg(trade_results, "static_r")
    cf_agg     = _agg(trade_results, "cf_exit_r")

    # ── classification counts ─────────────────────────────────────────────────
    losers_improved  = [t for t in losers if t["classification"] == "IMPROVED"]
    losers_neutral   = [t for t in losers if t["classification"] == "NEUTRAL"]
    losers_degraded  = [t for t in losers if t["classification"] == "DEGRADED"]
    losers_not_trig  = [t for t in losers if t["classification"] == "NOT_TRIGGERED"]

    winners_preserved  = [t for t in winners if t["classification"] in ("NOT_TRIGGERED", "NEUTRAL")]
    winners_converted  = [t for t in winners if t["classification"] == "DEGRADED"]
    winners_improved   = [t for t in winners if t["classification"] == "IMPROVED"]

    total_r_saved   = sum(t["cf_delta_r"] for t in losers if t["cf_delta_r"] > 0)
    total_r_lost    = sum(abs(t["cf_delta_r"]) for t in winners if t["cf_delta_r"] < 0)
    net_delta_r     = round(sum(t["cf_delta_r"] for t in trade_results), 4)

    # ── timing effectiveness ──────────────────────────────────────────────────
    # Losers where event fired
    fired_losers = [t for t in losers if t["cf_fired"]]
    before_sl_count      = sum(1 for t in fired_losers if t["event_before_sl"])
    after_or_at_sl_count = sum(1 for t in fired_losers if not t["event_before_sl"])

    fired_r_vals     = [t["cf_close_r"] for t in fired_losers
                        if t.get("cf_close_r") is not None]
    r_saved_vals     = [t["cf_delta_r"] for t in fired_losers if t["cf_delta_r"] > 0]

    minutes_vals     = [t["cf_minutes_from_entry"] for t in fired_losers
                        if t["cf_minutes_from_entry"] is not None]
    within_5m  = sum(1 for v in minutes_vals if v <= 5)
    within_10m = sum(1 for v in minutes_vals if v <= 10)
    within_15m = sum(1 for v in minutes_vals if v <= 15)
    within_30m = sum(1 for v in minutes_vals if v <= 30)

    # IP loser reach
    ip_event_before_sl = [t for t in ip_losers if t["cf_fired"] and t["event_before_sl"]]
    ip_r_saved_vals    = [t["cf_delta_r"] for t in ip_event_before_sl if t["cf_delta_r"] > 0]

    # ── winner false-positive detail ──────────────────────────────────────────
    fp_detail: list[dict] = []
    for t in winners_converted + winners_improved:
        # Find path bars between entry and exit
        n_scan = t["scan_window_m5_bars"]
        eg_t   = eg_by_sig[t["signal_id"]]
        tp1_v  = float(eg_t["tp1"])
        entry_ts_v = t["entry_ts"]
        retest_v   = t["m15_retest_level"]
        entry_v    = t["entry_price"]
        stop_v     = t["initial_stop"]
        risk_v     = t["risk_points"]
        direction_v = t["direction"]
        fa_t   = fa_by_sig[t["signal_id"]]
        sl_min_v = t["sl_minutes"]

        # Find static terminal bar info
        static_tp_ts = entry_ts_v + sl_min_v * 60  # terminal M15 bar close

        # Collect bar-level path from entry to static terminal
        path_bars: list[dict] = []
        ts = entry_ts_v
        terminal_m15_close = entry_ts_v + sl_min_v * 60
        while ts < terminal_m15_close:
            bar = m5_by_ts.get(ts)
            if bar:
                c = float(bar["close"])
                close_r = round(
                    (entry_v - c) / risk_v if direction_v == "SHORT"
                    else (c - entry_v) / risk_v,
                    4,
                )
                mfe_r = round(
                    (entry_v - float(bar["low"])) / risk_v if direction_v == "SHORT"
                    else (float(bar["high"]) - entry_v) / risk_v,
                    4,
                )
                mae_r = round(
                    (float(bar["high"]) - entry_v) / risk_v if direction_v == "SHORT"
                    else (entry_v - float(bar["low"])) / risk_v,
                    4,
                )
                path_bars.append({
                    "bar_index": (ts - entry_ts_v) // M5_SECONDS,
                    "ts": ts,
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low":  float(bar["low"]),
                    "close": c,
                    "close_r": close_r,
                    "bar_mfe_r": mfe_r,
                    "bar_mae_r": mae_r,
                })
            ts += M5_SECONDS

        # Find max adverse before exit, static TP1 info
        exit_ts_v = t["cf_exit_ts"]
        bars_before_exit = [b for b in path_bars if b["ts"] < exit_ts_v] if exit_ts_v else path_bars
        max_adverse_before_exit = max(
            (b["bar_mae_r"] for b in bars_before_exit), default=0.0
        )

        pe_t = pe_by_sig[t["signal_id"]]
        tp1_ts_static = None
        if pe_t.get("time_to_tp1") is not None:
            tp1_ts_static = t["entry_ts"] + int(pe_t["time_to_tp1"]) * 60

        time_from_event_to_tp1 = None
        if exit_ts_v and tp1_ts_static:
            time_from_event_to_tp1 = (tp1_ts_static - exit_ts_v) // 60

        fp_detail.append({
            "signal_id":             t["signal_id"],
            "direction":             direction_v,
            "entry_price":           entry_v,
            "m15_retest_level":      retest_v,
            "event_ts":              exit_ts_v,
            "event_r":               t["cf_exit_r"],
            "event_minutes_from_entry": t["cf_minutes_from_entry"],
            "static_r":              t["static_r"],
            "cf_r":                  t["cf_exit_r"],
            "max_adverse_r_before_event": max_adverse_before_exit,
            "static_tp1_ts":         tp1_ts_static,
            "time_from_event_to_tp1_min": time_from_event_to_tp1,
            "path_bars_before_exit": bars_before_exit,
        })

    # ── economics summary ─────────────────────────────────────────────────────
    economics = {
        "M5_RETEST_EXIT_ECONOMICALLY_PROMISING": (
            "YES" if net_delta_r > 0 and len(winners_converted) <= 2 else
            "MIXED" if net_delta_r > 0 else
            "NO"
        ),
        "M5_RETEST_EXIT_FALSE_POSITIVE_CONCERN": (
            "YES" if len(winners_converted) >= 3 else
            "MIXED" if len(winners_converted) >= 1 else
            "NO"
        ),
        "M5_RETEST_EXIT_TIMELY_ENOUGH_FOR_IMMEDIATE_FAILURES": (
            "NO" if len(ip_event_before_sl) == 0 else
            "MIXED" if len(ip_event_before_sl) < len(ip_losers) // 2 else
            "YES"
        ),
    }

    # ── build artifact ─────────────────────────────────────────────────────────
    artifact = {
        "study_type": "M5_RETEST_EXIT_COUNTERFACTUAL_OBSERVATIONAL",
        "management_concept": "retest_structure_reclaimed",
        "policy_name": "EXIT_ON_M5_CLOSE_THROUGH_M15_RETEST_LEVEL",
        "management_concept_source_status": "SOURCE_SUPPORTED",
        "m5_implementation_source_status": "IMPLEMENTATION_HYPOTHESIS",
        "base_commits": {
            "v3_entry":             V3_ENTRY_COMMIT,
            "post_entry":           POST_ENTRY_COMMIT,
            "exit_counterfactual":  EXIT_COUNTERFACTUAL_COMMIT,
            "failure_anatomy":      FAILURE_ANATOMY_COMMIT,
            "entry_geometry_audit": ENTRY_GEOMETRY_COMMIT,
            "m5_microstructure":    M5_MICROSTRUCTURE_COMMIT,
        },
        "canonical_source_sha256": canonical_sha,
        "methodology": {
            "exit_trigger":           "COMPLETED M5 bar close through M15 retest level (pullback extreme)",
            "scan_window":            "M5 bars with open_ts < terminal_m15_open (M15 bar containing static SL/TP)",
            "sl_intrabar_policy":     "CONSERVATIVE — if adverse extreme hits SL in same M5 bar, SL governs (-1.0R)",
            "tp1_intrabar_policy":    "TP1 governs if favorable extreme hits TP1 before bar close",
            "exit_price_basis":       "CLOSE of triggering completed M5 bar (or static SL/TP if conservative precedence)",
            "terminal_time_source":   "failure_anatomy.sl_minutes (M15 resolution)",
            "sl_resolution_note":     "Static model uses M15 bars; sl_minutes always multiple of 15; IP losers with sl_minutes=15 have zero scan window",
            "no_wick_only_breach":    True,
            "no_intrabar_breach":     True,
            "no_buffer":              True,
            "no_persistence_required": True,
            "no_threshold_tuned":     True,
            "no_policy_created":      True,
        },
        "total_trades": len(trade_results),
        "total_losers": len(losers),
        "total_winners": len(winners),
        "immediate_peak_losers": len(ip_losers),
        "static_aggregate": static_agg,
        "cf_aggregate": cf_agg,
        "comparison": {
            "STATIC_WIN_RATE":      static_agg["win_rate_pct"],
            "STATIC_EXPECTANCY_R":  static_agg["expectancy_r"],
            "STATIC_TOTAL_R":       static_agg["total_r"],
            "STATIC_PROFIT_FACTOR": static_agg["profit_factor"],
            "STATIC_MAX_DD_R":      static_agg["max_dd_r"],
            "M5_RETEST_EXIT_WIN_RATE":      cf_agg["win_rate_pct"],
            "M5_RETEST_EXIT_EXPECTANCY_R":  cf_agg["expectancy_r"],
            "M5_RETEST_EXIT_TOTAL_R":       cf_agg["total_r"],
            "M5_RETEST_EXIT_PROFIT_FACTOR": cf_agg["profit_factor"],
            "M5_RETEST_EXIT_MAX_DD_R":      cf_agg["max_dd_r"],
            "SIGNALS_FIRED":               sum(1 for t in trade_results if t["cf_fired"]),
            "STATIC_LOSERS_TOUCHED":       sum(1 for t in losers  if t["cf_fired"]),
            "STATIC_WINNERS_TOUCHED":      sum(1 for t in winners if t["cf_fired"]),
            "LOSERS_IMPROVED":   len(losers_improved),
            "LOSERS_NEUTRAL":    len(losers_neutral),
            "LOSERS_DEGRADED":   len(losers_degraded),
            "LOSERS_NOT_TRIGGERED": len(losers_not_trig),
            "WINNERS_PRESERVED": len(winners_preserved),
            "WINNERS_CONVERTED": len(winners_converted),
            "WINNER_R_LOST":     round(total_r_lost, 4),
            "TOTAL_R_SAVED_ON_LOSERS": round(total_r_saved, 4),
            "TOTAL_R_LOST_ON_WINNERS": round(total_r_lost, 4),
            "NET_DELTA_R":       net_delta_r,
        },
        "timing_effectiveness": {
            "fired_loser_count":           len(fired_losers),
            "EVENT_BEFORE_SL_COUNT":       before_sl_count,
            "EVENT_AFTER_OR_AT_SL_COUNT":  after_or_at_sl_count,
            "MEDIAN_R_AT_EVENT":           _med(fired_r_vals),
            "P25_R_AT_EVENT":              _p25(fired_r_vals),
            "P75_R_AT_EVENT":              _p75(fired_r_vals),
            "MEDIAN_R_SAVED_VS_SL":        _med(r_saved_vals) if r_saved_vals else 0.0,
            "EVENT_WITHIN_5M_OF_ENTRY":    within_5m,
            "EVENT_WITHIN_10M_OF_ENTRY":   within_10m,
            "EVENT_WITHIN_15M_OF_ENTRY":   within_15m,
            "EVENT_WITHIN_30M_OF_ENTRY":   within_30m,
            "IMMEDIATE_PEAK_LOSERS":              len(ip_losers),
            "IMMEDIATE_PEAK_LOSERS_EVENT_BEFORE_SL": len(ip_event_before_sl),
            "IMMEDIATE_PEAK_LOSERS_R_SAVED":         round(sum(ip_r_saved_vals), 4)
                                                     if ip_r_saved_vals else 0.0,
        },
        "false_positive_detail": fp_detail,
        "economics_summary": economics,
        "per_trade_results": trade_results,
        "no_trade_manager_policy_created": True,
        "no_exit_rule_optimized": True,
        "no_threshold_tuned": True,
        "no_parameter_search": True,
        "validation_outcomes_accessed": False,
        "entry_evaluator_changed": False,
        "stop_evaluator_changed": False,
        "target_evaluator_changed": False,
        "production_changed": False,
        "broker_writes": 0,
        "ready_for_validation": False,
    }

    artifact_bytes = json.dumps(artifact, sort_keys=True).encode()
    artifact_fp    = hashlib.sha256(artifact_bytes).hexdigest()
    artifact["artifact_fingerprint"] = artifact_fp

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    out_path = args.artifact_root / "kojo_v3_m5_retest_exit_counterfactual.json"
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"artifact: {out_path}", file=sys.stderr)

    # ── summary report ────────────────────────────────────────────────────────
    print(json.dumps({
        "STUDY":           "M5_RETEST_EXIT_COUNTERFACTUAL",
        "POLICY":          "EXIT_ON_M5_CLOSE_THROUGH_M15_RETEST_LEVEL",
        "ARTIFACT_FINGERPRINT": artifact_fp,
        "comparison":   artifact["comparison"],
        "timing_effectiveness": artifact["timing_effectiveness"],
        "false_positive_detail": [
            {k: v for k, v in fp.items() if k != "path_bars_before_exit"}
            for fp in fp_detail
        ],
        "economics_summary": economics,
        "TRADE_MANAGER_POLICY_CREATED": False,
        "EXIT_RULE_OPTIMIZED": False,
        "THRESHOLD_TUNED": False,
        "PARAMETER_SEARCH": False,
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "PRODUCTION_CHANGED": False,
        "BROKER_WRITES": 0,
        "READY_FOR_VALIDATION": False,
    }, indent=2))


if __name__ == "__main__":
    main()
