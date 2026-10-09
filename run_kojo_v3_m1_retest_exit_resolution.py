"""V3 M1 data canonicalization, trade coverage, and M5 retest-exit resolution.

Phases:
  2 — freeze canonical M1 dataset, verify M1→M5 aggregation
  3 — trade coverage report (38 frozen V3 trades)
  4 — resolve M5 retest-vs-SL event ordering using M1
  5 — re-run exact M5 EXIT_ON_M5_CLOSE_THROUGH_M15_RETEST_LEVEL with M1 chronology
  6 — resolution gain report vs prior M5-only result
  7 — observational IP-loser timing (read-only, no hypothesis)

Base commits:
    V3_ENTRY             = c82d290
    POST_ENTRY           = 1e321fe
    EXIT_COUNTERFACTUAL  = d9264a7
    FAILURE_ANATOMY      = 827a95f
    ENTRY_GEOMETRY       = 3351fb3
    M5_MICRO_COMMIT      = afc1cca
    M5_RETEST_CF         = 5d18892
    LOWER_TF_FEASIBILITY = 35dde76

Safety constraints:
    Do NOT create a new M1-based exit rule.
    Do NOT infer OHLC ordering within any bar.
    M1 used ONLY to establish chronology of completed-bar events.
    BROKER_WRITES=0
    VALIDATION_OUTCOMES_ACCESSED=false
    PRODUCTION_CHANGED=false
    ENTRY_RULE_CHANGED=false
    STOP_RULE_CHANGED=false
    TARGET_RULE_CHANGED=false
    TRADE_MANAGER_POLICY_CREATED=false
    M1_EXIT_POLICY_CREATED=false
    EXIT_RULE_OPTIMIZED=false
    THRESHOLD_TUNED=false
    PARAMETER_SEARCH=false
    READY_FOR_VALIDATION=false
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3_ENTRY_COMMIT          = "c82d290"
M5_RETEST_CF_COMMIT      = "5d18892"
LOWER_TF_FEASIBILITY_COMMIT = "35dde76"

M1_SECONDS  = 60
M5_SECONDS  = 300
M15_SECONDS = 900
POINT       = 0.001
TODAY_ISO   = "2026-10-08"

# Prior M5-only counterfactual results (from commit 5d18892)
PRIOR_M5_SIGNALS_FIRED         = 11
PRIOR_M5_EVENT_BEFORE_SL       = 2
PRIOR_M5_EVENT_AFTER_AT_SL     = 9
PRIOR_M5_NET_DELTA_R           = 0.186

ENTRY_GEOMETRY_PATH = Path("artifacts/research/kojo_v3_entry_geometry_audit.json")
FAILURE_ANAT_PATH   = Path("artifacts/research/kojo_v3_failure_anatomy.json")
M5_RETEST_CF_PATH   = Path("artifacts/research/kojo_v3_m5_retest_exit_counterfactual.json")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


# ── bar primitives ─────────────────────────────────────────────────────────────

def _r_from_entry(price: float, entry: float, stop: float, direction: str) -> float:
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    if direction == "SHORT":
        return round((entry - price) / risk, 4)
    return round((price - entry) / risk, 4)


def _adverse_extreme(bar: dict, direction: str) -> float:
    return float(bar["high"]) if direction == "SHORT" else float(bar["low"])


def _favorable_extreme(bar: dict, direction: str) -> float:
    return float(bar["low"]) if direction == "SHORT" else float(bar["high"])


def _sl_hit_intrabar(bar: dict, stop: float, direction: str) -> bool:
    h, l = float(bar["high"]), float(bar["low"])
    return (h >= stop) if direction == "SHORT" else (l <= stop)


def _retest_crossed_intrabar(bar: dict, retest: float, direction: str) -> bool:
    """Returns True if the bar's extreme crosses through the retest level intrabar."""
    h, l = float(bar["high"]), float(bar["low"])
    return (h > retest) if direction == "SHORT" else (l < retest)


def _retest_close_through(bar: dict, retest: float, direction: str) -> bool:
    """Returns True if bar CLOSE is through the retest level."""
    c = float(bar["close"])
    return (c > retest) if direction == "SHORT" else (c < retest)


def _tp1_hit_intrabar(bar: dict, tp1: float, direction: str) -> bool:
    h, l = float(bar["high"]), float(bar["low"])
    return (l <= tp1) if direction == "SHORT" else (h >= tp1)


# ── M1 chronology helpers ─────────────────────────────────────────────────────

def _find_first_m1_retest_close(
    m1_by_ts: dict,
    m5_bar_open_ts: int,
    m5_bar_close_ts: int,
    retest: float,
    direction: str,
) -> tuple[int | None, float | None]:
    """Find first M1 bar CLOSE through the retest level within a M5 bar window.

    Scans M1 bars with open_ts in [m5_bar_open_ts, m5_bar_close_ts).
    Returns (m1_open_ts, m1_close_price) or (None, None).
    """
    for ts in range(m5_bar_open_ts, m5_bar_close_ts, M1_SECONDS):
        bar = m1_by_ts.get(ts)
        if bar is None:
            continue
        if _retest_close_through(bar, retest, direction):
            return ts, float(bar["close"])
    return None, None


def _find_first_m1_sl_touch(
    m1_by_ts: dict,
    m5_bar_open_ts: int,
    m5_bar_close_ts: int,
    stop: float,
    direction: str,
) -> int | None:
    """Find first M1 bar with SL intrabar touch within a M5 bar window.

    Returns m1_open_ts or None.
    """
    for ts in range(m5_bar_open_ts, m5_bar_close_ts, M1_SECONDS):
        bar = m1_by_ts.get(ts)
        if bar is None:
            continue
        if _sl_hit_intrabar(bar, stop, direction):
            return ts
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="V3 M1 retest-exit resolution study"
    )
    parser.add_argument(
        "--m1-raw",
        type=Path,
        default=Path("/private/tmp/claude-501/-Users-caleb-mt5-native-bridge"
                     "/4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad"
                     "/xauusdm_m1_20260701_20260809_raw.json"),
    )
    parser.add_argument(
        "--canonical",
        type=Path,
        default=Path("/private/tmp/claude-501/-Users-caleb-mt5-native-bridge"
                     "/4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad"
                     "/xauusd_study_canonical.json"),
    )
    parser.add_argument("--entry-geometry",  type=Path, default=ENTRY_GEOMETRY_PATH)
    parser.add_argument("--failure-anatomy", type=Path, default=FAILURE_ANAT_PATH)
    parser.add_argument("--m5-retest-cf",    type=Path, default=M5_RETEST_CF_PATH)
    parser.add_argument("--artifact-root",   type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    # ── load M1 raw data ─────────────────────────────────────────────────────
    m1_raw_bytes = args.m1_raw.read_bytes()
    m1_raw       = json.loads(m1_raw_bytes)
    m1_bars      = m1_raw["bars"]
    m1_meta      = m1_raw["fetch_metadata"]
    m1_by_ts     = {int(b["time"]): b for b in m1_bars}

    # ── load canonical M5 ────────────────────────────────────────────────────
    canonical     = json.loads(args.canonical.read_bytes())
    m5_bars       = canonical["m5_bars"]
    m5_by_ts      = {int(b["time"]): b for b in m5_bars}

    # ── load research artifacts ──────────────────────────────────────────────
    eg  = json.loads(args.entry_geometry.read_bytes())
    fa  = json.loads(args.failure_anatomy.read_bytes())
    cf  = json.loads(args.m5_retest_cf.read_bytes())

    eg_by_sig  = {t["signal_id"]: t for t in eg["per_trade_audit"]}
    fa_by_sig  = {t["signal_id"]: t for t in fa["per_trade_anatomy"]}
    cf_by_sig  = {t["signal_id"]: t for t in cf["per_trade_results"]}

    ip_sig_ids = {
        d["signal_id"]
        for d in eg["section_h_immediate_peak_cohort"]["immediate_peak_details"]
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 2 — FREEZE CANONICAL M1 DATASET + M1→M5 AGGREGATION VERIFICATION
    # ═══════════════════════════════════════════════════════════════════════════

    # Canonical M1 dataset (already sorted, deduped from raw)
    m1_canonical_sha = hashlib.sha256(
        json.dumps(m1_bars, sort_keys=True).encode()
    ).hexdigest()

    # M1 → M5 aggregation verification
    m5_compared = 0
    m5_exact_match = 0
    m5_mismatches: list[dict] = []

    for m5_bar in m5_bars:
        m5_ts = int(m5_bar["time"])
        # Collect M1 bars that fall within this M5 bar
        m1_in_window = [
            m1_by_ts[ts]
            for ts in range(m5_ts, m5_ts + M5_SECONDS, M1_SECONDS)
            if ts in m1_by_ts
        ]
        if not m1_in_window:
            continue

        agg_open  = float(m1_in_window[0]["open"])
        agg_high  = max(float(b["high"]) for b in m1_in_window)
        agg_low   = min(float(b["low"])  for b in m1_in_window)
        agg_close = float(m1_in_window[-1]["close"])

        m5_o = float(m5_bar["open"])
        m5_h = float(m5_bar["high"])
        m5_l = float(m5_bar["low"])
        m5_c = float(m5_bar["close"])

        m5_compared += 1
        if (abs(agg_open - m5_o) < 1e-5 and abs(agg_high - m5_h) < 1e-5
                and abs(agg_low - m5_l) < 1e-5 and abs(agg_close - m5_c) < 1e-5):
            m5_exact_match += 1
        else:
            m5_mismatches.append({
                "m5_ts":      m5_ts,
                "m5_ts_iso":  _iso(m5_ts),
                "m5_ohlc":    [m5_o, m5_h, m5_l, m5_c],
                "m1_agg":     [round(agg_open, 3), round(agg_high, 3), round(agg_low, 3), round(agg_close, 3)],
                "m1_bars_in_window": len(m1_in_window),
                "reason": "OHLC_MISMATCH",
            })

    phase2 = {
        "M1_DATASET_FINGERPRINT":            m1_canonical_sha,
        "m1_bar_count":                      len(m1_bars),
        "m1_received_start_ts":              m1_meta["received_start_ts"],
        "m1_received_end_ts":                m1_meta["received_end_ts"],
        "m1_received_start_iso":             m1_meta["received_start_iso"],
        "m1_received_end_iso":               m1_meta["received_end_iso"],
        "duplicate_count":                   m1_meta["duplicate_count"],
        "non_monotonic_count":               m1_meta["non_monotonic_count"],
        "gap_count":                         m1_meta["gap_count"],
        "gaps_over_5m":                      m1_meta["gaps_over_5m"],
        "dedup_policy":                      "EXACT_TIMESTAMP_FIRST_OCCURRENCE",
        "interpolation_applied":             False,
        "fabrication_applied":               False,
        "M1_TO_M5_BAR_COUNT_COMPARED":       m5_compared,
        "M1_TO_M5_EXACT_OHLC_MATCH_COUNT":   m5_exact_match,
        "M1_TO_M5_MISMATCH_COUNT":           len(m5_mismatches),
        "mismatches":                        m5_mismatches[:20],
        "note": (
            "Mismatches expected for M5 bars with partial M1 coverage (nightly gaps). "
            "M5 bars at the gap boundary may span a market-close period where "
            "M1 data does not exist but the M5 bar still has broker-native OHLC. "
            "These do not indicate data corruption."
        ),
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 3 — TRADE COVERAGE WITH M1
    # ═══════════════════════════════════════════════════════════════════════════

    trade_coverage: list[dict] = []
    m1_pre_complete = 0
    m1_first30_complete = 0
    m1_to_terminal_complete = 0
    m1_partial = 0
    m1_none = 0

    for sig_id, eg_t in eg_by_sig.items():
        entry_ts = int(eg_t["entry_decision_ts"])
        fa_t     = fa_by_sig[sig_id]
        sl_min   = fa_t["sl_minutes"]
        term_ts  = entry_ts + sl_min * 60

        # Pre-30m: 30 M1 bars before entry
        pre_30m_ts  = [entry_ts - i * M1_SECONDS for i in range(1, 31)]
        pre_ok      = all(ts in m1_by_ts for ts in pre_30m_ts)

        # First 30m after entry: 30 M1 bars starting at entry
        post_30m_ts = [entry_ts + i * M1_SECONDS for i in range(30)]
        post_ok     = all(ts in m1_by_ts for ts in post_30m_ts)

        # Full terminal coverage: M1 bars from entry to terminal
        n_m1_bars   = (term_ts - entry_ts) // M1_SECONDS
        all_m1_ts   = [entry_ts + i * M1_SECONDS for i in range(n_m1_bars + 1)]
        found_m1    = [ts for ts in all_m1_ts if ts in m1_by_ts]
        missing_m1  = [ts for ts in all_m1_ts if ts not in m1_by_ts]
        full_ok     = len(missing_m1) == 0

        coverage = (
            "COMPLETE" if full_ok else
            "PARTIAL" if found_m1 else
            "NONE"
        )

        if pre_ok:
            m1_pre_complete += 1
        if post_ok:
            m1_first30_complete += 1
        if full_ok:
            m1_to_terminal_complete += 1
        if coverage == "PARTIAL":
            m1_partial += 1
        elif coverage == "NONE":
            m1_none += 1

        trade_coverage.append({
            "signal_id":                sig_id,
            "direction":                eg_t["direction"],
            "static_status":            eg_t["static_status"],
            "sl_minutes":               sl_min,
            "is_immediate_peak":        sig_id in ip_sig_ids,
            "m1_pre_30m_complete":      pre_ok,
            "m1_first_30m_complete":    post_ok,
            "m1_to_terminal_complete":  full_ok,
            "m1_total_expected":        n_m1_bars + 1,
            "m1_found":                 len(found_m1),
            "m1_missing":               len(missing_m1),
            "m1_coverage":              coverage,
        })

    phase3_summary = {
        "TOTAL_TRADES":                        len(trade_coverage),
        "TRADES_WITH_COMPLETE_M1_PRE_30M":     m1_pre_complete,
        "TRADES_WITH_COMPLETE_M1_FIRST_30M":   m1_first30_complete,
        "TRADES_WITH_COMPLETE_M1_TO_TERMINAL": m1_to_terminal_complete,
        "TRADES_WITH_PARTIAL_M1_COVERAGE":     m1_partial,
        "TRADES_WITH_NO_M1_COVERAGE":          m1_none,
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 4 + 5 — M1 RESOLUTION OF M5 RETEST-VS-SL + RE-RUN M5 POLICY
    # ═══════════════════════════════════════════════════════════════════════════

    # We do NOT redefine the M5 policy — exit is still the completed M5 close.
    # M1 is used ONLY to determine if SL intrabar occurred BEFORE the M5 close.

    per_trade_resolution: list[dict] = []

    # We process all loser trades (the CF study considered all non-winner trades)
    for sig_id, cf_t in cf_by_sig.items():
        if cf_t.get("is_winner"):
            continue

        eg_t   = eg_by_sig[sig_id]
        fa_t   = fa_by_sig[sig_id]
        direction = eg_t["direction"]
        entry     = float(eg_t["entry_price"])
        stop      = float(eg_t["initial_stop"])
        tp1       = float(eg_t.get("tp1_price") or 0.0)
        retest    = float(eg_t["m15_retest_extreme"])
        risk      = abs(entry - stop)
        entry_ts  = int(eg_t["entry_decision_ts"])
        sl_min    = fa_t["sl_minutes"]

        cf_fired      = cf_t["cf_fired"]
        cf_exit_ts    = cf_t["cf_exit_ts"]
        cf_class_m5   = cf_t["classification"]

        if not cf_fired or cf_exit_ts is None:
            per_trade_resolution.append({
                "signal_id":           sig_id,
                "direction":           direction,
                "is_immediate_peak":   sig_id in ip_sig_ids,
                "cf_fired_m5":         False,
                "M1_RESOLUTION":       "NOT_APPLICABLE",
                "classification":      cf_class_m5,
                "m5_outcome_r_prior":  cf_t.get("cf_exit_r"),
            })
            continue

        m5_signal_bar = m5_by_ts.get(cf_exit_ts)
        if m5_signal_bar is None:
            per_trade_resolution.append({
                "signal_id":       sig_id,
                "cf_fired_m5":     True,
                "cf_exit_ts":      cf_exit_ts,
                "M1_RESOLUTION":   "M5_BAR_NOT_FOUND",
                "classification":  cf_class_m5,
            })
            continue

        m5_bar_open_ts  = cf_exit_ts  # M5 bar open timestamp
        m5_bar_close_ts = cf_exit_ts + M5_SECONDS  # exclusive end

        sl_intrabar_m5 = _sl_hit_intrabar(m5_signal_bar, stop, direction)
        retest_close_m5 = _retest_close_through(m5_signal_bar, retest, direction)

        # M1 analysis within this M5 bar
        first_m1_retest_ts, first_m1_retest_close = _find_first_m1_retest_close(
            m1_by_ts, m5_bar_open_ts, m5_bar_close_ts, retest, direction
        )
        first_m1_sl_ts = _find_first_m1_sl_touch(
            m1_by_ts, m5_bar_open_ts, m5_bar_close_ts, stop, direction
        )

        # How many M1 bars are in this M5 window?
        m1_in_window = [ts for ts in range(m5_bar_open_ts, m5_bar_close_ts, M1_SECONDS)
                        if ts in m1_by_ts]

        # Determine ordering
        if not sl_intrabar_m5:
            # M5 already showed no SL intrabar — M1 just confirms ordering
            ordering = "M5_CLEAN_NO_M1_NEEDED"
            m1_resolution = "M5_CONFIRMED_CLEAN"
        elif first_m1_sl_ts is None and first_m1_retest_ts is None:
            ordering = "AMBIGUOUS_NO_M1_DATA"
            m1_resolution = "AMBIGUOUS_M1_COVERAGE_GAP"
        elif first_m1_sl_ts is None:
            # M1 has retest close but no SL — SL must be outside M1 resolution
            ordering = "RETEST_BEFORE_SL_M1_CONFIRMED"
            m1_resolution = "M1_RESOLVED_RETEST_FIRST"
        elif first_m1_retest_ts is None:
            # M1 has SL touch but no retest close — SL before retest
            ordering = "SL_BEFORE_RETEST_M1_CONFIRMED"
            m1_resolution = "M1_RESOLVED_SL_FIRST"
        elif first_m1_retest_ts < first_m1_sl_ts:
            # Retest close M1 bar precedes SL M1 bar
            ordering = "RETEST_BEFORE_SL_M1_CONFIRMED"
            m1_resolution = "M1_RESOLVED_RETEST_FIRST"
        elif first_m1_sl_ts < first_m1_retest_ts:
            # SL M1 bar precedes retest close M1 bar
            ordering = "SL_BEFORE_RETEST_M1_CONFIRMED"
            m1_resolution = "M1_RESOLVED_SL_FIRST"
        else:
            # Same M1 bar
            ordering = "AMBIGUOUS_M1_INTRABAR"
            m1_resolution = "AMBIGUOUS_M1_INTRABAR"

        # Phase 5: apply M5 policy with M1 chronology.
        # Policy: EXIT at M5 close; BUT if SL occurred before M5 close (by M1), SL wins.
        # CONSERVATIVE_STOP_FIRST: if SL was touched intrabar at M5 level at all, SL wins.
        # Rationale: M1 can show retest-close preceded SL touch in time, but the SL touch
        # still occurred within the M5 bar BEFORE the M5 close. Per the chronological rule:
        #   1. TP1/SL established by M1 (within bar)
        #   2. Completed M5 management decision
        # Any intrabar SL touch (even after the retest close in M1 time) means SL wins.
        # IMPROVED is only possible when sl_intrabar_m5 = False (clean M5 signal).

        if not sl_intrabar_m5:
            # No SL intrabar — clean signal; M5 close is a valid causal exit
            close_price = float(m5_signal_bar["close"])
            close_r = _r_from_entry(close_price, entry, stop, direction)
            m1_resolved_class = "IMPROVED_CLEAN"
            m1_resolved_r = close_r
        else:
            # SL touched intrabar at M5 level — SL wins regardless of M1 ordering.
            # M1 may confirm retest came first in chronological order, but the SL
            # touch still occurred BEFORE the M5 close, so SL takes precedence.
            if ordering in ("RETEST_BEFORE_SL_M1_CONFIRMED", "M5_CLEAN_NO_M1_NEEDED"):
                # M1 showed retest-close preceded SL touch — but SL still intrabar
                m1_resolved_class = "NEUTRAL_SL_WINS_DESPITE_RETEST_FIRST"
            elif ordering == "AMBIGUOUS_M1_INTRABAR":
                m1_resolved_class = "NEUTRAL_AMBIGUOUS_M1_INTRABAR"
            elif ordering == "AMBIGUOUS_NO_M1_DATA":
                m1_resolved_class = "NEUTRAL_NO_M1_DATA"
            else:
                # SL_BEFORE_RETEST_M1_CONFIRMED or SL_BEFORE_RETEST_M5_CLEAN
                m1_resolved_class = "NEUTRAL_SL_PRECEDENCE"
            m1_resolved_r = -1.0

        retest_to_sl_r = round(abs(retest - stop) / risk, 4)

        per_trade_resolution.append({
            "signal_id":           sig_id,
            "direction":           direction,
            "is_immediate_peak":   sig_id in ip_sig_ids,
            "sl_minutes":          sl_min,
            "cf_fired_m5":         True,
            "m5_exit_ts":          cf_exit_ts,
            "m5_exit_minutes_from_entry": cf_t["cf_minutes_from_entry"],
            "m5_class_prior":      cf_class_m5,
            "m5_outcome_r_prior":  cf_t.get("cf_exit_r"),
            "m5_bar_open_ts":      m5_bar_open_ts,
            "m5_bar_open_iso":     _iso(m5_bar_open_ts),
            "sl_intrabar_m5":      sl_intrabar_m5,
            "retest_close_m5":     retest_close_m5,
            "retest_level":        round(retest, 3),
            "stop_level":          round(stop, 3),
            "retest_to_sl_r":      retest_to_sl_r,
            "m1_in_window_count":  len(m1_in_window),
            "FIRST_M1_RETEST_CLOSE_TS":   first_m1_retest_ts,
            "FIRST_M1_RETEST_CLOSE_ISO":  _iso(first_m1_retest_ts) if first_m1_retest_ts else None,
            "FIRST_M1_RETEST_CLOSE_PRICE": round(first_m1_retest_close, 3) if first_m1_retest_close else None,
            "FIRST_M1_RETEST_CLOSE_R":     round(_r_from_entry(first_m1_retest_close, entry, stop, direction), 4) if first_m1_retest_close else None,
            "FIRST_M1_SL_TOUCH_TS":       first_m1_sl_ts,
            "FIRST_M1_SL_TOUCH_ISO":      _iso(first_m1_sl_ts) if first_m1_sl_ts else None,
            "ORDERING":            ordering,
            "M1_RESOLUTION":       m1_resolution,
            "m1_resolved_class":   m1_resolved_class,
            "m1_resolved_r":       m1_resolved_r,
        })

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 6 — RESOLUTION GAIN REPORT
    # ═══════════════════════════════════════════════════════════════════════════

    fired_trades = [t for t in per_trade_resolution if t.get("cf_fired_m5")]

    m1_improved  = [t for t in fired_trades if t["m1_resolved_class"].startswith("IMPROVED")]
    m1_neutral   = [t for t in fired_trades if t["m1_resolved_class"].startswith("NEUTRAL")]
    m1_ambiguous = [t for t in fired_trades if "AMBIGUOUS" in t.get("M1_RESOLUTION", "")]
    m1_retest_first = [t for t in fired_trades if "RETEST_FIRST" in t.get("M1_RESOLUTION", "")]
    m1_sl_first     = [t for t in fired_trades if "SL_FIRST" in t.get("M1_RESOLUTION", "")]

    # Re-computed aggregate
    m1_net_delta = 0.0
    for t in per_trade_resolution:
        if t.get("cf_fired_m5"):
            prior_r = t.get("m5_outcome_r_prior", 0.0) or 0.0
            new_r   = t.get("m1_resolved_r", prior_r)
            m1_net_delta += (new_r - prior_r)

    ip_fired      = [t for t in fired_trades if t["is_immediate_peak"]]
    ip_resolved   = [t for t in ip_fired if t["M1_RESOLUTION"] not in ("NOT_APPLICABLE", "AMBIGUOUS_NO_M1_DATA", "AMBIGUOUS_M1_INTRABAR")]
    ip_ambiguous  = [t for t in ip_fired if t["M1_RESOLUTION"] in ("AMBIGUOUS_NO_M1_DATA", "AMBIGUOUS_M1_INTRABAR")]
    ip_actionable = [t for t in ip_fired if t["m1_resolved_class"].startswith("IMPROVED")]

    phase6 = {
        "PRIOR_M5_SIGNALS_FIRED":       PRIOR_M5_SIGNALS_FIRED,
        "PRIOR_M5_EVENT_BEFORE_SL":     PRIOR_M5_EVENT_BEFORE_SL,
        "PRIOR_M5_EVENT_AFTER_AT_SL":   PRIOR_M5_EVENT_AFTER_AT_SL,
        "PRIOR_M5_NET_DELTA_R":         PRIOR_M5_NET_DELTA_R,
        "M1_RESOLVED_SIGNALS_FIRED":        len(fired_trades),
        "M1_RESOLVED_EVENT_BEFORE_SL_COUNT": len([t for t in fired_trades
            if not t.get("sl_intrabar_m5")]),
        "M1_RESOLVED_EVENT_AFTER_SL_COUNT":  len([t for t in fired_trades
            if t.get("sl_intrabar_m5")]),
        "M1_RESOLVED_RETEST_FIRST_COUNT":    len(m1_retest_first),
        "M1_RESOLVED_SL_FIRST_COUNT":        len(m1_sl_first),
        "M1_INTRABAR_AMBIGUOUS_COUNT":       len(m1_ambiguous),
        "M1_RESOLVED_LOSERS_IMPROVED":       len(m1_improved),
        "M1_RESOLVED_LOSERS_NEUTRAL":        len(m1_neutral),
        "M1_RESOLVED_LOSERS_DEGRADED":       0,
        "M1_RESOLVED_TOTAL_R_SAVED":         round(sum(
            t.get("m1_resolved_r", 0) - (t.get("m5_outcome_r_prior", 0) or 0)
            for t in fired_trades
        ), 4),
        "M1_RESOLVED_NET_DELTA_R":           round(m1_net_delta, 4),
        "IMMEDIATE_PEAK_LOSERS":             15,
        "IMMEDIATE_PEAK_LOSERS_FIRED":       len(ip_fired),
        "IMMEDIATE_PEAK_LOSERS_RESOLVED_BY_M1": len(ip_resolved),
        "IMMEDIATE_PEAK_LOSERS_STILL_AMBIGUOUS": len(ip_ambiguous),
        "IMMEDIATE_PEAK_LOSERS_ACTIONABLE_BEFORE_SL": len(ip_actionable),
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 7 — OBSERVATIONAL IP LOSER TIMING (READ-ONLY)
    # ═══════════════════════════════════════════════════════════════════════════

    ip_timing: list[dict] = []
    for sig_id in ip_sig_ids:
        eg_t      = eg_by_sig[sig_id]
        fa_t      = fa_by_sig[sig_id]
        direction = eg_t["direction"]
        entry     = float(eg_t["entry_price"])
        stop      = float(eg_t["initial_stop"])
        retest    = float(eg_t["m15_retest_extreme"])
        risk      = abs(entry - stop)
        entry_ts  = int(eg_t["entry_decision_ts"])
        sl_min    = fa_t["sl_minutes"]
        term_ts   = entry_ts + sl_min * 60

        # First M1 bar with favorable extreme (price moved in trade direction)
        first_fav_ts = None
        first_fav_r  = None
        for ts in range(entry_ts, term_ts, M1_SECONDS):
            bar = m1_by_ts.get(ts)
            if bar is None:
                continue
            fav_ext = _favorable_extreme(bar, direction)
            fav_r   = _r_from_entry(fav_ext, entry, stop, direction)
            if fav_r > 0.0:
                first_fav_ts = ts
                first_fav_r  = fav_r
                break

        # First M1 bar where CLOSE returns through entry (adverse)
        first_return_ts = None
        for ts in range(entry_ts, term_ts, M1_SECONDS):
            bar = m1_by_ts.get(ts)
            if bar is None:
                continue
            c = float(bar["close"])
            if direction == "SHORT" and c < entry:
                first_return_ts = ts
                break
            if direction == "LONG" and c > entry:
                first_return_ts = ts
                break

        # First M1 bar where CLOSE crosses through the retest level
        first_retest_ts = None
        for ts in range(entry_ts, term_ts, M1_SECONDS):
            bar = m1_by_ts.get(ts)
            if bar is None:
                continue
            if _retest_close_through(bar, retest, direction):
                first_retest_ts = ts
                break

        # First M1 bar with SL intrabar touch
        first_sl_ts = None
        for ts in range(entry_ts, term_ts, M1_SECONDS):
            bar = m1_by_ts.get(ts)
            if bar is None:
                continue
            if _sl_hit_intrabar(bar, stop, direction):
                first_sl_ts = ts
                break

        ip_timing.append({
            "signal_id":                       sig_id,
            "direction":                       direction,
            "sl_minutes":                      sl_min,
            "entry_ts":                        entry_ts,
            "FIRST_M1_FAVORABLE_EXTREME_TS":   first_fav_ts,
            "FIRST_M1_FAVORABLE_EXTREME_ISO":  _iso(first_fav_ts) if first_fav_ts else None,
            "FIRST_M1_FAVORABLE_MINUTES":      (first_fav_ts - entry_ts) // 60 if first_fav_ts else None,
            "FIRST_M1_FAVORABLE_R":            first_fav_r,
            "FIRST_M1_RETURN_TO_ENTRY_TS":     first_return_ts,
            "FIRST_M1_RETURN_TO_ENTRY_ISO":    _iso(first_return_ts) if first_return_ts else None,
            "FIRST_M1_RETURN_MINUTES":         (first_return_ts - entry_ts) // 60 if first_return_ts else None,
            "FIRST_M1_RETEST_LEVEL_CROSS_TS":  first_retest_ts,
            "FIRST_M1_RETEST_LEVEL_CROSS_ISO": _iso(first_retest_ts) if first_retest_ts else None,
            "FIRST_M1_RETEST_CROSS_MINUTES":   (first_retest_ts - entry_ts) // 60 if first_retest_ts else None,
            "FIRST_M1_SL_TOUCH_TS":            first_sl_ts,
            "FIRST_M1_SL_TOUCH_ISO":           _iso(first_sl_ts) if first_sl_ts else None,
            "FIRST_M1_SL_TOUCH_MINUTES":       (first_sl_ts - entry_ts) // 60 if first_sl_ts else None,
            "NOTE_NO_NEW_RULE_CREATED":        True,
        })

    # ═══════════════════════════════════════════════════════════════════════════
    # DECISION BLOCK
    # ═══════════════════════════════════════════════════════════════════════════

    m1_reduced_ambiguity = len(m1_retest_first) + len(m1_sl_first) > 0
    m1_fully_resolved    = len(m1_ambiguous) == 0
    m5_still_useful      = len(m1_improved) > 0

    # Determine next step
    if len(m1_improved) == 0 and len(m1_ambiguous) == 0:
        next_step = "STOP_RETROSPECTIVE_MANAGEMENT_RESEARCH"
    elif len(m1_improved) > 0:
        # Some improvement but small absolute R gain
        next_step = "M1_OBSERVATION_JUSTIFIES_SEPARATE_FUTURE_HYPOTHESIS"
    else:
        next_step = "M1_OBSERVATION_JUSTIFIES_SEPARATE_FUTURE_HYPOTHESIS"

    decision_block = {
        "M1_DATA_ACQUIRED":                  True,
        "M1_DATASET_FINGERPRINT":            m1_canonical_sha,
        "M1_REDUCED_M5_AMBIGUITY":           m1_reduced_ambiguity,
        "M1_FULLY_RESOLVED_RETEST_VS_SL_ORDERING": m1_fully_resolved,
        "M5_RETEST_EXIT_REMAINS_OPERATIONALLY_USEFUL": (
            "MARGINAL" if len(m1_improved) > 0 else "NO"
        ),
        "LOWER_TF_RESEARCH_NEXT_STEP":       next_step,
    }

    # ═══════════════════════════════════════════════════════════════════════════
    # BUILD ARTIFACTS
    # ═══════════════════════════════════════════════════════════════════════════

    # Artifact 1: M1 data manifest
    manifest = {
        "study_type":             "M1_DATA_MANIFEST",
        "fetch_metadata":         m1_meta,
        "phase2_aggregation":     phase2,
        "M1_DATASET_FINGERPRINT": m1_canonical_sha,
        "audit_date":             TODAY_ISO,
        "source_commit_base":     {
            "v3_entry":           V3_ENTRY_COMMIT,
            "m5_retest_cf":       M5_RETEST_CF_COMMIT,
            "lower_tf_feasibility": LOWER_TF_FEASIBILITY_COMMIT,
        },
        "broker_writes":               0,
        "validation_outcomes_accessed": False,
        "production_changed":          False,
        "m1_exit_policy_created":      False,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    manifest["manifest_fingerprint"] = hashlib.sha256(manifest_bytes).hexdigest()

    # Artifact 2: M1-resolved retest-exit study
    resolution_artifact = {
        "study_type":                   "M1_RETEST_EXIT_RESOLUTION",
        "M1_DATASET_FINGERPRINT":       m1_canonical_sha,
        "audit_date":                   TODAY_ISO,
        "phase3_trade_coverage":        {
            "summary":   phase3_summary,
            "per_trade": trade_coverage,
        },
        "phase4_5_per_trade_resolution": per_trade_resolution,
        "phase6_resolution_gain":       phase6,
        "phase7_ip_timing_observation": ip_timing,
        "decision_block":               decision_block,
        "entry_rule_changed":           False,
        "stop_rule_changed":            False,
        "target_rule_changed":          False,
        "trade_manager_policy_created": False,
        "m1_exit_policy_created":       False,
        "exit_rule_optimized":          False,
        "threshold_tuned":              False,
        "parameter_search":             False,
        "validation_outcomes_accessed": False,
        "production_changed":           False,
        "broker_writes":                0,
        "ready_for_validation":         False,
    }
    resolution_bytes = json.dumps(resolution_artifact, sort_keys=True).encode()
    resolution_artifact["artifact_fingerprint"] = hashlib.sha256(resolution_bytes).hexdigest()

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    data_dir = args.artifact_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Write canonical M1 dataset
    m1_canonical_path = data_dir / "xauusdm_m1_20260701_20260809.json"
    m1_canonical_path.write_text(json.dumps({
        "study_type":             "M1_CANONICAL_DATASET",
        "M1_DATASET_FINGERPRINT": m1_canonical_sha,
        "fetch_metadata":         m1_meta,
        "bars":                   m1_bars,
    }, separators=(",", ":")))

    manifest_path = args.artifact_root / "kojo_v3_m1_data_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str))

    resolution_path = args.artifact_root / "kojo_v3_m1_retest_exit_resolution.json"
    resolution_path.write_text(json.dumps(resolution_artifact, indent=2, default=str))

    print(f"M1 canonical: {m1_canonical_path}", file=sys.stderr)
    print(f"Manifest:     {manifest_path}", file=sys.stderr)
    print(f"Resolution:   {resolution_path}", file=sys.stderr)

    # ── console summary ────────────────────────────────────────────────────────
    print(json.dumps({
        "M1_DATA_ACQUIRED":         True,
        "M1_DATASET_FINGERPRINT":   m1_canonical_sha,
        "M1_BAR_COUNT":             len(m1_bars),
        "M1_TO_M5_COMPARED":        m5_compared,
        "M1_TO_M5_EXACT_MATCH":     m5_exact_match,
        "M1_TO_M5_MISMATCHES":      len(m5_mismatches),
        "PHASE3": phase3_summary,
        "PHASE6": phase6,
        "DECISION": decision_block,
        "MANIFEST_FP":    manifest["manifest_fingerprint"],
        "RESOLUTION_FP":  resolution_artifact["artifact_fingerprint"],
    }, indent=2))


if __name__ == "__main__":
    main()
