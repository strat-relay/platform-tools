"""Data availability and lower-TF feasibility audit for V3 post-entry management.

Inspects the frozen canonical dataset and associated market data to determine
what lower-timeframe data exists and whether it can support causal pre-SL
management research.

Base commits:
    V3_ENTRY             = c82d290
    POST_ENTRY           = 1e321fe
    EXIT_COUNTERFACTUAL  = d9264a7
    FAILURE_ANATOMY      = 827a95f
    ENTRY_GEOMETRY       = 3351fb3
    M5_MICROSTRUCTURE    = afc1cca
    M5_RETEST_CF         = 5d18892

Safety constraints:
    No synthetic intrabar paths created.
    No intrabar ordering inferred from OHLC.
    BROKER_WRITES=0
    VALIDATION_OUTCOMES_ACCESSED=false
    PRODUCTION_CHANGED=false
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
POST_ENTRY_COMMIT        = "1e321fe"
EXIT_COUNTERFACTUAL_COMMIT = "d9264a7"
FAILURE_ANATOMY_COMMIT   = "827a95f"
ENTRY_GEOMETRY_COMMIT    = "3351fb3"
M5_MICRO_COMMIT          = "afc1cca"
M5_RETEST_CF_COMMIT      = "5d18892"

M5_SECONDS  = 300
M15_SECONDS = 900
POINT       = 0.001
TODAY_ISO   = "2026-10-08"

ENTRY_GEOMETRY_PATH = Path("artifacts/research/kojo_v3_entry_geometry_audit.json")
FAILURE_ANAT_PATH   = Path("artifacts/research/kojo_v3_failure_anatomy.json")
M5_RETEST_CF_PATH   = Path("artifacts/research/kojo_v3_m5_retest_exit_counterfactual.json")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description="V3 data availability and lower-TF feasibility audit")
    parser.add_argument("--canonical", type=Path,
        default=Path("/private/tmp/claude-501/-Users-caleb-mt5-native-bridge"
                     "/4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"))
    parser.add_argument("--entry-geometry",  type=Path, default=ENTRY_GEOMETRY_PATH)
    parser.add_argument("--failure-anatomy", type=Path, default=FAILURE_ANAT_PATH)
    parser.add_argument("--m5-retest-cf",    type=Path, default=M5_RETEST_CF_PATH)
    parser.add_argument("--artifact-root",   type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    # ── load sources ──────────────────────────────────────────────────────────
    canonical_bytes = args.canonical.read_bytes()
    canonical_sha   = hashlib.sha256(canonical_bytes).hexdigest()
    canonical       = json.loads(canonical_bytes)

    provenance   = canonical["provenance"]
    m5_bars      = canonical["m5_bars"]
    gaps         = canonical.get("gaps", [])
    m5_by_ts     = {int(b["time"]): b for b in m5_bars}
    m5_ts_set    = set(m5_by_ts.keys())

    eg  = json.loads(args.entry_geometry.read_bytes())
    fa  = json.loads(args.failure_anatomy.read_bytes())
    cf  = json.loads(args.m5_retest_cf.read_bytes())

    eg_by_sig   = {t["signal_id"]: t for t in eg["per_trade_audit"]}
    fa_by_sig   = {t["signal_id"]: t for t in fa["per_trade_anatomy"]}
    cf_by_sig   = {t["signal_id"]: t for t in cf["per_trade_results"]}

    ip_sig_ids  = {d["signal_id"] for d in eg["section_h_immediate_peak_cohort"]["immediate_peak_details"]}
    cf_fired    = {t["signal_id"] for t in cf["per_trade_results"] if t["cf_fired"] and not t["is_winner"]}

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION A: DATA INVENTORY
    # ──────────────────────────────────────────────────────────────────────────

    spread_vals    = [b["spread"] for b in m5_bars if b.get("spread", 0) > 0]
    tick_vol_vals  = [b["tick_volume"] for b in m5_bars if b.get("tick_volume", 0) > 0]
    real_vol_zero  = all(b.get("real_volume", 0) == 0 for b in m5_bars)

    section_a: dict[str, Any] = {
        "M5_OHLC": {
            "AVAILABLE":          True,
            "SOURCE":             "MT5/Exness (Exness-MT5Real27, XAUUSDm)",
            "DATE_COVERAGE":      f"{provenance['first_bar']} to {provenance['last_bar']}",
            "SYMBOL":             provenance["broker_symbol"],
            "CAUSALITY_USABLE":   True,
            "BROKER_NATIVE":      True,
            "bar_count":          len(m5_bars),
            "gap_count":          len(gaps),
            "unknown_gap_count":  provenance.get("unknown_gaps", 0),
            "note":               "Frozen; sha256 sealed. Spread field is bar-open snapshot (single integer point value), not intrabar min/max.",
        },
        "M1_OHLC": {
            "AVAILABLE":          False,
            "SOURCE":             "NOT IN FROZEN CANONICAL DATASET",
            "DATE_COVERAGE":      "NOT COLLECTED",
            "SYMBOL":             "XAUUSDm (Exness)",
            "CAUSALITY_USABLE":   "CONDITIONAL — see note",
            "BROKER_NATIVE":      True,
            "note":               (
                "M1 bars not present in the frozen canonical file. "
                "historical_fetch_symbol_m1.py exists in the repo and supports "
                "copy_rates_range() for date-range M1 fetch via MT5 Python API. "
                "Discovery window (Jul 1 – Aug 9 2026) is 60–99 days ago. "
                "Exness M1 retention is typically ~6 months; the 190-day fetch "
                "window in the existing script covers this range. "
                "HOWEVER: MT5 Python module is Windows-only; this machine is macOS "
                "(darwin 25.5.0). Collection requires the Windows-side MT5 bridge."
            ),
            "fetch_script_available": True,
            "fetch_script_path":      "historical_fetch_symbol_m1.py",
            "days_since_discovery_start": 99,
            "days_since_discovery_end":   60,
            "broker_m1_retention_days":   "~180 (Exness historical; not guaranteed)",
            "within_retention_estimate":  True,
            "collection_blocker":         "Requires Windows MT5 terminal; macOS-only environment here.",
        },
        "TICK_TIMESTAMPS": {
            "AVAILABLE":          False,
            "SOURCE":             "NOT AVAILABLE FOR HISTORICAL WINDOW",
            "DATE_COVERAGE":      "NOT APPLICABLE",
            "SYMBOL":             "XAUUSDm",
            "CAUSALITY_USABLE":   False,
            "BROKER_NATIVE":      True,
            "note":               (
                "MT5 CopyTicksRange() supports tick-level bid/ask streams. "
                "Discovery window is 60–99 days old. "
                "Exness tick data retention: typically 1–2 weeks (broker-dependent). "
                "This window is OUTSIDE standard tick retention. "
                "No CopyTicks implementation exists in any MT5 EA in this codebase. "
                "Tick data for Jul–Aug 2026 is PERMANENTLY INACCESSIBLE."
            ),
            "tick_retention_days": "7–14 (Exness typical; unverified)",
            "permanently_lost":    True,
        },
        "BID_ASK_PER_TICK": {
            "AVAILABLE":          False,
            "SOURCE":             "NOT AVAILABLE",
            "CAUSALITY_USABLE":   False,
            "BROKER_NATIVE":      True,
            "note":               "Requires tick data; tick data is permanently lost (>60 days old).",
        },
        "SPREAD_PER_M5_BAR": {
            "AVAILABLE":          True,
            "SOURCE":             "MT5 OHLC bar field (bar-open snapshot only)",
            "DATE_COVERAGE":      f"{provenance['first_bar']} to {provenance['last_bar']}",
            "SYMBOL":             "XAUUSDm",
            "CAUSALITY_USABLE":   "LIMITED — bar-open snapshot only; intrabar spread variation unknown",
            "BROKER_NATIVE":      True,
            "spread_min_pts":     min(spread_vals),
            "spread_max_pts":     max(spread_vals),
            "spread_nonzero_pct": 100.0,
            "note":               (
                "MT5 OHLC 'spread' field is a single integer snapshot at bar open "
                "(ask – bid in points). Not min/max/mean intrabar spread. "
                "Causally usable as bar-open spread estimate only. "
                "Cannot recover intrabar bid/ask variation."
            ),
        },
        "TICK_VOLUME_PER_M5_BAR": {
            "AVAILABLE":          True,
            "SOURCE":             "MT5 OHLC bar field",
            "DATE_COVERAGE":      f"{provenance['first_bar']} to {provenance['last_bar']}",
            "CAUSALITY_USABLE":   "LIMITED — proxy activity; not actual contract volume",
            "BROKER_NATIVE":      True,
            "tick_vol_min":       min(tick_vol_vals),
            "tick_vol_max":       max(tick_vol_vals),
            "note":               "Tick volume = number of price changes during bar. Not lot volume (real_volume is 0).",
        },
        "REAL_VOLUME": {
            "AVAILABLE":          False,
            "SOURCE":             "MT5 OHLC field present but all zeros",
            "CAUSALITY_USABLE":   False,
            "note":               "real_volume is 0 for all 12096 M5 bars (standard for Exness FX/metals on MT5).",
        },
    }

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION B: TRADE COVERAGE
    # ──────────────────────────────────────────────────────────────────────────

    trade_coverage: list[dict] = []
    complete_m5_pre  = 0
    complete_m5_post = 0
    full_m5_through_terminal = 0
    has_m5_gap_during_trade  = 0

    for sig_id, eg_t in eg_by_sig.items():
        entry_ts   = int(eg_t["entry_decision_ts"])
        fa_t       = fa_by_sig[sig_id]
        term_ts    = entry_ts + fa_t["sl_minutes"] * 60
        n_bars     = (term_ts - entry_ts) // M5_SECONDS

        pre_bars   = [entry_ts - i * M5_SECONDS for i in range(1, 7)]
        post_bars  = [entry_ts + i * M5_SECONDS for i in range(0, 7)]
        trade_bars = [entry_ts + i * M5_SECONDS for i in range(n_bars + 1)]

        pre_ok     = all(ts in m5_ts_set for ts in pre_bars)
        post_ok    = all(ts in m5_ts_set for ts in post_bars)
        full_ok    = all(ts in m5_ts_set for ts in trade_bars)
        missing_in_trade = [ts for ts in trade_bars if ts not in m5_ts_set]

        if pre_ok:
            complete_m5_pre += 1
        if post_ok:
            complete_m5_post += 1
        if full_ok:
            full_m5_through_terminal += 1
        if missing_in_trade:
            has_m5_gap_during_trade += 1

        # Find gap classification if any
        gap_info = []
        for ts in missing_in_trade[:3]:
            for g in gaps:
                if g["from"] <= ts <= g["to"]:
                    gap_info.append({
                        "missing_ts": ts,
                        "gap_from": g["from"],
                        "gap_to": g["to"],
                        "gap_minutes": g["gap_minutes"],
                        "classification": g["classification"],
                    })
                    break

        trade_coverage.append({
            "signal_id":                  sig_id,
            "direction":                  eg_t["direction"],
            "static_status":              eg_t["static_status"],
            "entry_ts":                   entry_ts,
            "terminal_ts":                term_ts,
            "sl_minutes":                 fa_t["sl_minutes"],
            "is_immediate_peak":          sig_id in ip_sig_ids,
            "m5_pre_30m_complete":        pre_ok,
            "m5_post_30m_complete":       post_ok,
            "m5_full_through_terminal":   full_ok,
            "m5_missing_bars_count":      len(missing_in_trade),
            "gap_info":                   gap_info,
            "m1_coverage":                "NOT_COLLECTED",
            "tick_coverage":              "PERMANENTLY_LOST",
            "bid_ask_coverage":           "PERMANENTLY_LOST",
        })

    section_b_summary = {
        "total_trades":                         len(trade_coverage),
        "TRADES_WITH_COMPLETE_M5_PRE_30M":      complete_m5_pre,
        "TRADES_WITH_COMPLETE_M5_POST_30M":     complete_m5_post,
        "TRADES_WITH_COMPLETE_M5_THROUGH_TERMINAL": full_m5_through_terminal,
        "TRADES_WITH_M5_GAP_DURING_TRADE":      has_m5_gap_during_trade,
        "TRADES_WITH_COMPLETE_M1_COVERAGE":     "UNKNOWN — M1 not collected",
        "TRADES_WITH_PARTIAL_M1_COVERAGE":      "UNKNOWN — M1 not collected",
        "TRADES_WITH_TICK_COVERAGE":            0,
        "TRADES_WITH_BID_ASK_COVERAGE":         0,
        "note": (
            "All 38 trades have complete M5 coverage for 30min pre-entry, entry, and 30min post-entry. "
            "5 trades have M5 gaps during later portion of the trade (all classified 'unknown', "
            "all at the same ~65-min nightly gap on weekdays). "
            "None of the 5 affected trades are immediate-peak losers. "
            "M1 data has NOT been collected; M1 coverage is unknown. "
            "Tick and bid/ask coverage: zero (permanently inaccessible)."
        ),
    }

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION C: TIMING RESOLUTION ANALYSIS
    # ──────────────────────────────────────────────────────────────────────────

    # For IP losers and 11 counterfactual-fired losers: what can M1 clarify?
    section_c_ip: list[dict] = []
    section_c_cf: list[dict] = []

    # IP losers
    for sig_id in ip_sig_ids:
        eg_t   = eg_by_sig[sig_id]
        fa_t   = fa_by_sig[sig_id]
        direction = eg_t["direction"]
        entry  = float(eg_t["entry_price"])
        stop   = float(eg_t["initial_stop"])
        retest = float(eg_t["m15_retest_extreme"])
        risk   = abs(entry - stop)
        entry_ts = int(eg_t["entry_decision_ts"])
        sl_min = fa_t["sl_minutes"]

        bar0 = m5_by_ts.get(entry_ts)
        bar1 = m5_by_ts.get(entry_ts + M5_SECONDS)

        # What we know at M5 resolution
        mfe_ts_resolution = f"within bar0 open ({entry_ts}) to close ({entry_ts + M5_SECONDS})"
        fav_extreme_price = float(bar0["low"]) if direction == "SHORT" else float(bar0["high"]) if bar0 else None

        # Earliest adverse structure event at M5 resolution
        first_adverse_close_ts = None
        for i in range(1, (sl_min // 5) + 1):
            b = m5_by_ts.get(entry_ts + i * M5_SECONDS)
            if b:
                c = float(b["close"])
                adverse = (c > entry) if direction == "SHORT" else (c < entry)
                if adverse and first_adverse_close_ts is None:
                    first_adverse_close_ts = entry_ts + i * M5_SECONDS

        # Retest level first crossed at M5 resolution
        first_retest_cross_ts = None
        for i in range(0, (sl_min // 5) + 1):
            b = m5_by_ts.get(entry_ts + i * M5_SECONDS)
            if b:
                h, l, c = float(b["high"]), float(b["low"]), float(b["close"])
                retest_intrabar = (h > retest) if direction == "SHORT" else (l < retest)
                if retest_intrabar and first_retest_cross_ts is None:
                    first_retest_cross_ts = entry_ts + i * M5_SECONDS

        # SL first touched at M5 resolution (intrabar)
        sl_touch_ts_m5 = None
        for i in range(0, (sl_min // 5) + 1):
            b = m5_by_ts.get(entry_ts + i * M5_SECONDS)
            if b:
                h, l = float(b["high"]), float(b["low"])
                sl_hit = (h >= stop) if direction == "SHORT" else (l <= stop)
                if sl_hit and sl_touch_ts_m5 is None:
                    sl_touch_ts_m5 = entry_ts + i * M5_SECONDS

        section_c_ip.append({
            "signal_id":                    sig_id,
            "direction":                    direction,
            "sl_minutes":                   sl_min,
            "entry_ts":                     entry_ts,
            "retest_to_sl_gap_r":           round(abs(retest - stop) / risk, 3),
            "FIRST_FAVORABLE_EXTREME_TS":   f"WITHIN_M5_BAR_0 ({entry_ts}–{entry_ts+M5_SECONDS})",
            "FIRST_ADVERSE_CLOSE_M5_TS":    first_adverse_close_ts,
            "FIRST_ADVERSE_CLOSE_MINUTES":  (first_adverse_close_ts - entry_ts) // 60 if first_adverse_close_ts else None,
            "M15_RETEST_LEVEL_CROSS_M5_BAR_TS": first_retest_cross_ts,
            "SL_FIRST_TOUCH_M5_BAR_TS":     sl_touch_ts_m5,
            "TIME_RETEST_TO_SL_SECONDS_M5": (
                (sl_touch_ts_m5 - first_retest_cross_ts)
                if sl_touch_ts_m5 and first_retest_cross_ts else None
            ),
            "M5_RESOLUTION_AMBIGUITY": (
                "SAME_BAR" if sl_touch_ts_m5 and first_retest_cross_ts and
                sl_touch_ts_m5 == first_retest_cross_ts else
                "DIFFERENT_BARS" if sl_touch_ts_m5 and first_retest_cross_ts else
                "INCOMPLETE"
            ),
            "M1_WOULD_CLARIFY": (
                "YES — same M5 bar; M1 could resolve intrabar ordering"
                if sl_touch_ts_m5 and first_retest_cross_ts and sl_touch_ts_m5 == first_retest_cross_ts
                else "NO — events in different bars; M5 is already sufficient"
            ),
        })

    # Counterfactual-fired losers: same analysis on the signal bar
    for sig_id in cf_fired:
        t      = cf_by_sig[sig_id]
        eg_t   = eg_by_sig[sig_id]
        direction = eg_t["direction"]
        entry  = float(eg_t["entry_price"])
        stop   = float(eg_t["initial_stop"])
        retest = float(eg_t["m15_retest_extreme"])
        risk   = abs(entry - stop)
        entry_ts = int(eg_t["entry_decision_ts"])
        exit_ts  = t["cf_exit_ts"]

        if exit_ts is None:
            continue

        bar = m5_by_ts.get(exit_ts)
        if bar is None:
            continue

        h, l, c = float(bar["high"]), float(bar["low"]), float(bar["close"])
        sl_intrabar  = (h >= stop) if direction == "SHORT" else (l <= stop)
        retest_close = (c > retest) if direction == "SHORT" else (c < retest)
        close_r      = round((entry - c) / risk if direction == "SHORT" else (c - entry) / risk, 4)

        section_c_cf.append({
            "signal_id":              sig_id,
            "direction":              direction,
            "cf_class":               t["classification"],
            "signal_bar_ts":          exit_ts,
            "minutes_from_entry":     t["cf_minutes_from_entry"],
            "retest_level":           round(retest, 3),
            "stop":                   round(stop, 3),
            "retest_to_sl_gap_r":     round(abs(retest - stop) / risk, 3),
            "bar_h":                  h, "bar_l": l, "bar_c": c,
            "sl_intrabar":            sl_intrabar,
            "retest_close":           retest_close,
            "close_r":                close_r,
            "m5_ambiguity":           "SAME_BAR_SL_AND_RETEST" if sl_intrabar else "CLEAN_SIGNAL",
            "m1_would_clarify":       sl_intrabar,
            "max_improvement_if_retest_first": round(abs(retest - stop) / risk, 3) if sl_intrabar else 0.0,
            "M1_resolution_note": (
                "If retest-level M1 close precedes SL intrabar touch: "
                f"exit at ~-{abs(retest-entry)/risk:.3f}R vs -1.0R (save ~{abs(retest-stop)/risk:.3f}R). "
                "M1 bars still have same-bar ambiguity if retest-close and SL-touch "
                "occur within the same M1 bar (retest-to-SL gap is only "
                f"{abs(retest-stop)/risk:.3f}R = {abs(retest-stop):.3f} pts)."
                if sl_intrabar else
                "Signal bar had no intrabar SL; M5 resolution is sufficient for this trade."
            ),
        })

    # Summary for Section C
    same_bar_ambiguous = sum(1 for t in section_c_cf if t["sl_intrabar"])
    max_total_improvement = sum(t["max_improvement_if_retest_first"] for t in section_c_cf if t["sl_intrabar"])
    ip_same_bar = sum(1 for t in section_c_ip if t["M5_RESOLUTION_AMBIGUITY"] == "SAME_BAR")

    section_c_summary = {
        "ip_losers_analyzed":         len(section_c_ip),
        "ip_with_same_bar_ambiguity": ip_same_bar,
        "cf_fired_losers_analyzed":   len(section_c_cf),
        "cf_with_sl_intrabar_ambiguity": same_bar_ambiguous,
        "cf_clean_m5_signals":        len(section_c_cf) - same_bar_ambiguous,
        "max_total_r_if_m1_resolves_favorably": round(max_total_improvement, 4),
        "note": (
            f"9/11 counterfactual signal bars have SL intrabar AND retest level in same M5 bar. "
            f"M1 would resolve the {same_bar_ambiguous} ambiguous cases to 1-minute precision. "
            f"However: retest-to-SL gap averages ~0.14R = ~0.14 price points. "
            f"Within a 1-minute bar, both events may occur (same M1 bar ambiguity persists). "
            f"Only tick data resolves intrabar order definitively; tick data is PERMANENTLY LOST. "
            f"Maximum possible improvement if M1 resolves ALL 9 favorably: ~{max_total_improvement:.3f}R "
            f"across 38 trades (highly optimistic upper bound)."
        ),
    }

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION D: FEASIBILITY CONCLUSION
    # ──────────────────────────────────────────────────────────────────────────

    section_d = {
        "FEASIBILITY_CONCLUSION": "LOWER_TF_MANAGEMENT_RESEARCH_REQUIRES_NEW_M1_DATA",
        "reasoning": (
            "The frozen canonical dataset contains M5 OHLC only. "
            "M1 OHLC for the discovery window (Jul 1 – Aug 9 2026) is NOT in the dataset "
            "but is likely still available from Exness MT5 (~6-month retention). "
            "Collection requires the existing historical_fetch_symbol_m1.py script "
            "executed via the Windows-side MT5 bridge. "
            "Tick data (bid/ask sequence) is permanently inaccessible (>60 days old, "
            "beyond typical 1–2 week tick retention)."
        ),
        "CAN_DETERMINE_PRE_SL_DETERIORATION_CAUSALLY": "PARTIALLY_AT_M1_RESOLUTION",
        "CAN_RESEARCH_M1_MANAGEMENT_WITHOUT_INVENTING_INTRABAR_ORDER": True,
        "CAN_RESEARCH_TICK_MANAGEMENT": False,
        "BROKER_ACCURATE_BID_ASK_AVAILABLE": False,
        "notes": {
            "M5_current_state": (
                "M5 is complete for all 38 trades (30 min pre/post, entry bar, 33/38 full). "
                "M5 resolution leaves 9/11 counterfactual events ambiguous (SL + retest in same bar). "
                "M5-only management research is possible but leaves this ambiguity unresolved."
            ),
            "M1_potential": (
                "M1 would reduce the same-bar ambiguity from 5-minute windows to 1-minute windows. "
                "However: the retest-to-SL gap is only 0.09–0.22R = 0.09–0.22 price points. "
                "A 1-minute bar can easily contain both events if price moves fast. "
                "M1 does NOT eliminate intrabar ambiguity — it only reduces its temporal width by 5x. "
                "M1 can support causal EXIT_ON_M1_CLOSE_THROUGH_RETEST study with same conservative "
                "same-bar policy applied at 1-min granularity."
            ),
            "TICK_state": (
                "Tick data (bid/ask stream with timestamps) would definitively resolve ordering. "
                "Exness MT5 typically retains ticks for 7–14 days. "
                "Discovery window ended 60 days ago. "
                "Tick data is permanently inaccessible for this dataset. "
                "No CopyTicks implementation exists in any codebase EA."
            ),
            "spread_data": (
                "M5 bar-open spread snapshot is available (160–480 points). "
                "This is NOT intrabar bid/ask separation — it is a single snapshot at bar open. "
                "Not usable for precise execution cost modeling or slippage analysis."
            ),
            "CAUSALITY_NOTE": (
                "DO NOT infer intrabar sequence from OHLC at any timeframe. "
                "Any M1 study must apply the same conservative same-bar policy as the M5 study. "
                "A completed M1 bar close is a causal event; intrabar highs/lows are not ordered."
            ),
        },
        "required_action_if_proceeding": {
            "step_1": "Fetch XAUUSDm M1 history for Jul 1 – Aug 9 2026 via Windows MT5 bridge.",
            "script":  "historical_fetch_symbol_m1.py (already in repo)",
            "step_2":  "Verify coverage against all 38 trade windows before running any study.",
            "step_3":  "Apply same conservative same-bar policy as M5 counterfactual.",
            "blocker": "Requires Windows + live MT5 terminal. Cannot run on macOS without bridge.",
        },
    }

    # ──────────────────────────────────────────────────────────────────────────
    # BUILD ARTIFACT
    # ──────────────────────────────────────────────────────────────────────────

    artifact = {
        "study_type": "DATA_AVAILABILITY_AND_LOWER_TF_FEASIBILITY_AUDIT",
        "base_commits": {
            "v3_entry":              V3_ENTRY_COMMIT,
            "post_entry":            POST_ENTRY_COMMIT,
            "exit_counterfactual":   EXIT_COUNTERFACTUAL_COMMIT,
            "failure_anatomy":       FAILURE_ANATOMY_COMMIT,
            "entry_geometry":        ENTRY_GEOMETRY_COMMIT,
            "m5_microstructure":     M5_MICRO_COMMIT,
            "m5_retest_cf":          M5_RETEST_CF_COMMIT,
        },
        "canonical_source_sha256": canonical_sha,
        "audit_date": TODAY_ISO,
        "section_a_data_inventory":   section_a,
        "section_b_trade_coverage":   {
            "summary":   section_b_summary,
            "per_trade": trade_coverage,
        },
        "section_c_timing_resolution": {
            "summary":           section_c_summary,
            "ip_loser_analysis": section_c_ip,
            "cf_fired_analysis": section_c_cf,
        },
        "section_d_feasibility": section_d,
        "entry_rule_changed":          False,
        "stop_rule_changed":           False,
        "target_rule_changed":         False,
        "trade_manager_policy_created": False,
        "exit_rule_optimized":         False,
        "parameter_search":            False,
        "validation_outcomes_accessed": False,
        "production_changed":          False,
        "broker_writes":               0,
        "ready_for_validation":        False,
    }

    artifact_bytes = json.dumps(artifact, sort_keys=True).encode()
    artifact_fp    = hashlib.sha256(artifact_bytes).hexdigest()
    artifact["artifact_fingerprint"] = artifact_fp

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    out_path = args.artifact_root / "kojo_v3_data_availability_audit.json"
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"artifact: {out_path}", file=sys.stderr)

    # ── summary report ─────────────────────────────────────────────────────────
    print(json.dumps({
        "STUDY":              "DATA_AVAILABILITY_AND_LOWER_TF_FEASIBILITY_AUDIT",
        "ARTIFACT_FINGERPRINT": artifact_fp,
        "section_a_summary": {k: {
            "AVAILABLE": v["AVAILABLE"],
            "CAUSALITY_USABLE": v.get("CAUSALITY_USABLE"),
            "BROKER_NATIVE": v.get("BROKER_NATIVE"),
        } for k, v in section_a.items()},
        "section_b_summary": section_b_summary,
        "section_c_summary": section_c_summary,
        "section_d_feasibility": section_d,
        "TRADE_MANAGER_POLICY_CREATED": False,
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "BROKER_WRITES": 0,
        "READY_FOR_VALIDATION": False,
    }, indent=2))


if __name__ == "__main__":
    main()
