"""Failure-to-continue anatomy for KOJO_STRUCTURE_RECLAIM_V3 static losers.

Reconciles the '74 of 76' error from the counterfactual summary, then
performs detailed post-MFE path analysis on all 38 discovery trades
(26 losers primary focus, 12 winners as control).

Safety constraints (unchanged from commits c82d290 / 1e321fe / d9264a7):
    BROKER_WRITES=0
    VALIDATION_OUTCOMES_ACCESSED=false
    ENTRY_EVALUATOR_CHANGED=false
    TARGET_EVALUATOR_CHANGED=false
    PRODUCTION_CHANGED=false
    PARAMETER_SEARCH=false
    TRADE_MANAGER_POLICY_CREATED=false
    EXIT_RULE_OPTIMIZED=false
    THRESHOLD_TUNED=false
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from statistics import median, quantiles
from typing import Any

V3_COMMIT          = "c82d290"
POST_ENTRY_COMMIT  = "1e321fe"
CF_COMMIT          = "d9264a7"

M5_SECONDS  = 300
M15_SECONDS = 900
H1_SECONDS  = 3600
DISCOVERY_END = 1786319999

POST_ENTRY_PATH = Path("artifacts/research/kojo_v3_post_entry_behavior.json")
DISCOVERY_PATH  = Path("artifacts/backtests/kojo-v3-discovery-run-1/kojo-v3-discovery-run-1/result.json")
CANONICAL_PATH  = Path(
    "/private/tmp/claude-501/-Users-caleb-mt5-native-bridge/"
    "4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"
)


# ──────────────────────────────────────────────────────────────────────────────
# stats helpers
# ──────────────────────────────────────────────────────────────────────────────

def _med(vs: list) -> Any:
    fvs = [float(v) for v in vs if v is not None]
    return round(median(fvs), 1) if fvs else None


def _p25(vs: list) -> Any:
    fvs = [float(v) for v in vs if v is not None]
    if len(fvs) < 2:
        return round(fvs[0], 1) if fvs else None
    return round(quantiles(fvs, n=4)[0], 1)


def _p75(vs: list) -> Any:
    fvs = [float(v) for v in vs if v is not None]
    if len(fvs) < 2:
        return round(fvs[0], 1) if fvs else None
    return round(quantiles(fvs, n=4)[2], 1)


def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 1) if d else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# per-trade anatomy
# ──────────────────────────────────────────────────────────────────────────────

def _analyze_trade(
    trade: dict,
    exit_ts: int,
    m15_bars_all: list[dict],
    h1_bars_all: list[dict],
) -> dict:
    direction   = trade["direction"]
    entry_price = float(trade["entry_price"])
    stop_price  = float(trade["stop_price"])
    level_price = float(trade["level_price"])
    decision_ts = int(trade["decision_ts"])
    risk        = abs(entry_price - stop_price)
    is_long     = direction == "LONG"

    mfe_r   = float(trade["mfe_r"])
    mfe_min = trade.get("time_to_mfe_minutes") or 0

    # Approximate MFE price
    if is_long:
        mfe_price = entry_price + mfe_r * risk
    else:
        mfe_price = entry_price - mfe_r * risk

    sl_minutes = (exit_ts - decision_ts) // 60

    # post-entry completed M15 bars (open_ts >= decision_ts, close_ts <= exit_ts)
    post_m15 = [
        b for b in m15_bars_all
        if int(b["time"]) >= decision_ts
        and int(b["time"]) + M15_SECONDS <= exit_ts + 1
    ]

    # post-MFE completed M15 bars
    mfe_ts_approx = decision_ts + mfe_min * 60
    post_mfe_m15 = [b for b in post_m15 if int(b["time"]) >= mfe_ts_approx]

    # ── observation 1: key level relation post-MFE ─────────────────────────────
    reclaim_count        = 0
    consecutive_reclaim  = 0
    max_consecutive      = 0
    reclaim_restored     = False
    running_wrong_side   = 0
    max_adverse_during_reclaim = 0.0

    first_post_mfe_reclaim_min = None
    currently_on_wrong_side = False

    for b in post_mfe_m15:
        t   = int(b["time"])
        c   = float(b["close"])
        hi  = float(b["high"])
        lo  = float(b["low"])
        bar_min = (t - decision_ts) // 60

        wrong_side = (c < level_price) if is_long else (c > level_price)

        if wrong_side:
            if not currently_on_wrong_side and first_post_mfe_reclaim_min is None:
                first_post_mfe_reclaim_min = bar_min
            currently_on_wrong_side = True
            running_wrong_side += 1
            max_consecutive = max(max_consecutive, running_wrong_side)
            reclaim_count += 1
            # adverse excursion while on wrong side
            if is_long:
                adv_r = (entry_price - lo) / risk
            else:
                adv_r = (hi - entry_price) / risk
            max_adverse_during_reclaim = max(max_adverse_during_reclaim, adv_r)
        else:
            if currently_on_wrong_side:
                reclaim_restored = True
            currently_on_wrong_side = False
            running_wrong_side = 0

    # classify reclaim pattern
    has_post_mfe_reclaim = first_post_mfe_reclaim_min is not None
    transient_reclaim    = has_post_mfe_reclaim and reclaim_restored
    persistent_reclaim   = has_post_mfe_reclaim and not reclaim_restored

    # ── observation 2: new favorable extension after MFE ─────────────────────
    new_extension_after_mfe = False
    new_extension_min       = None

    for b in post_mfe_m15:
        t   = int(b["time"])
        hi  = float(b["high"])
        lo  = float(b["low"])
        bar_min = (t - decision_ts) // 60

        if is_long:
            favorable_price = hi
            better = favorable_price > mfe_price
        else:
            favorable_price = lo
            better = favorable_price < mfe_price

        if better and new_extension_min is None:
            new_extension_after_mfe = True
            new_extension_min = bar_min

    # ── observation 3: opposite M15 structure persistence ─────────────────────
    opp_rej_min  = trade.get("opposite_m15_rejection_ts_minutes")
    opp_persists = False

    if opp_rej_min is not None:
        # find the M15 bar after the rejection bar
        rejection_bar_time = decision_ts + opp_rej_min * 60
        bars_after_rej = [
            b for b in post_m15
            if int(b["time"]) > rejection_bar_time
        ]
        if bars_after_rej:
            next_bar = bars_after_rej[0]
            o2, h2, lo2, c2 = (float(next_bar[x]) for x in ("open", "high", "low", "close"))
            # for LONG: opposite = bearish close confirms persistence
            # for SHORT: opposite = bullish close confirms persistence
            if is_long:
                opp_persists = c2 < o2  # next bar is also bearish
            else:
                opp_persists = c2 > o2  # next bar is also bullish

    # ── observation 4: H1 lead time vs first M15 adverse event ───────────────
    h1_det_min   = trade.get("alignment_deterioration_ts_minutes")
    # first M15 adverse event POST-MFE
    m15_adverse_candidates = [
        ts for ts in [
            trade.get("time_to_first_reclaim"),
            trade.get("opposite_m15_rejection_ts_minutes"),
        ]
        if ts is not None and ts > mfe_min
    ]
    first_m15_post_mfe_adverse = min(m15_adverse_candidates) if m15_adverse_candidates else None

    # Lead time: compare when information becomes ACTIONABLE (bar close).
    # M15 bar with open at T_m15 closes at T_m15 + 15 min.
    # H1 bar with open at T_h1 closes at T_h1 + 60 min.
    # Actionable M15 lead = (h1_det_min + 60) - (first_m15_post_mfe_adverse + 15)
    # Positive = M15 actionable earlier than H1.
    m15_lead_before_h1 = None
    if h1_det_min is not None and first_m15_post_mfe_adverse is not None:
        m15_lead_before_h1 = (h1_det_min + 60) - (first_m15_post_mfe_adverse + 15)

    # ── observation 5: stall timings ─────────────────────────────────────────
    # time from MFE to first adverse M15 event (post-MFE)
    mfe_to_first_adverse_m15 = None
    if first_m15_post_mfe_adverse is not None:
        mfe_to_first_adverse_m15 = first_m15_post_mfe_adverse - mfe_min

    # time from MFE to SL (extension of loss after max favorable point)
    mfe_to_sl = sl_minutes - mfe_min

    # time from last favorable extension to SL
    last_fav_min = new_extension_min if new_extension_after_mfe else mfe_min
    last_fav_to_sl = sl_minutes - last_fav_min

    # ── cohort assignment ─────────────────────────────────────────────────────
    cohorts = []

    if persistent_reclaim:
        cohorts.append("PERSISTENT_RECLAIM_BEFORE_FAILURE")
    if transient_reclaim:
        cohorts.append("TRANSIENT_RECLAIM_THEN_CONTINUATION")
    if opp_persists:
        cohorts.append("STRUCTURE_FLIP_BEFORE_FAILURE")
    if h1_det_min is not None:
        cohorts.append("H1_DETERIORATION_LATE_CONFIRMATION")

    # STALL: no level reclaim, no structure flip, but trade took >45 min MFE→SL
    if not has_post_mfe_reclaim and not opp_persists and mfe_to_sl > 45:
        cohorts.append("STALL_THEN_FAILURE")

    if not cohorts:
        cohorts.append("AMBIGUOUS")

    primary_cohort = cohorts[0]

    return {
        "signal_id": trade["signal_id"],
        "direction": direction,
        "static_status": trade["static_status"],
        "mfe_r": mfe_r,
        "mfe_minutes": mfe_min,
        "sl_minutes": sl_minutes,
        "mfe_to_sl_minutes": mfe_to_sl,
        # level relation
        "has_post_mfe_level_reclaim": has_post_mfe_reclaim,
        "transient_reclaim": transient_reclaim,
        "persistent_reclaim": persistent_reclaim,
        "reclaim_restored_to_trade_side": reclaim_restored,
        "max_consecutive_wrong_side_m15_closes": max_consecutive,
        "max_adverse_r_during_reclaim": round(max_adverse_during_reclaim, 4) if max_adverse_during_reclaim else None,
        "first_post_mfe_reclaim_minutes": first_post_mfe_reclaim_min,
        # extension
        "new_favorable_extension_after_mfe": new_extension_after_mfe,
        "new_extension_minutes": new_extension_min,
        # structure persistence
        "opposite_m15_rejection_ts_minutes": opp_rej_min,
        "opposite_m15_structure_persists": opp_persists,
        # H1 lead time
        "h1_deterioration_ts_minutes": h1_det_min,
        "first_m15_post_mfe_adverse_minutes": first_m15_post_mfe_adverse,
        "m15_lead_before_h1_minutes": m15_lead_before_h1,
        # stall
        "mfe_to_first_adverse_m15_minutes": mfe_to_first_adverse_m15,
        "last_favorable_extension_to_sl_minutes": last_fav_to_sl,
        # cohort
        "all_cohorts": cohorts,
        "primary_cohort": primary_cohort,
    }


# ──────────────────────────────────────────────────────────────────────────────
# aggregate helper
# ──────────────────────────────────────────────────────────────────────────────

def _agg(recs: list[dict], label: str) -> dict:
    n = len(recs)
    if not n:
        return {"label": label, "count": 0}

    def _cnt(key: str, val: Any = True) -> int:
        return sum(1 for r in recs if r.get(key) == val)

    return {
        "label": label,
        "count": n,
        # level reclaim
        "has_post_mfe_reclaim": _cnt("has_post_mfe_level_reclaim"),
        "transient_reclaim": _cnt("transient_reclaim"),
        "persistent_reclaim": _cnt("persistent_reclaim"),
        # extension
        "new_favorable_extension_after_mfe": _cnt("new_favorable_extension_after_mfe"),
        # structure
        "opposite_m15_seen": sum(1 for r in recs if r.get("opposite_m15_rejection_ts_minutes") is not None),
        "opposite_m15_persists": _cnt("opposite_m15_structure_persists"),
        # H1
        "h1_deterioration": sum(1 for r in recs if r.get("h1_deterioration_ts_minutes") is not None),
        # timing medians
        "entry_to_mfe_median": _med([r["mfe_minutes"] for r in recs]),
        "entry_to_mfe_p25":    _p25([r["mfe_minutes"] for r in recs]),
        "entry_to_mfe_p75":    _p75([r["mfe_minutes"] for r in recs]),
        "mfe_to_sl_median":    _med([r.get("mfe_to_sl_minutes") for r in recs]),
        "mfe_to_sl_p25":       _p25([r.get("mfe_to_sl_minutes") for r in recs]),
        "mfe_to_sl_p75":       _p75([r.get("mfe_to_sl_minutes") for r in recs]),
        "mfe_to_first_adverse_m15_median": _med([r.get("mfe_to_first_adverse_m15_minutes") for r in recs]),
        "m15_lead_before_h1_median": _med([
            r["m15_lead_before_h1_minutes"] for r in recs
            if r.get("m15_lead_before_h1_minutes") is not None
        ]),
        # cohort distribution
        "cohort_counts": {
            cohort: sum(1 for r in recs if cohort in (r.get("all_cohorts") or []))
            for cohort in [
                "PERSISTENT_RECLAIM_BEFORE_FAILURE",
                "TRANSIENT_RECLAIM_THEN_CONTINUATION",
                "STRUCTURE_FLIP_BEFORE_FAILURE",
                "H1_DETERIORATION_LATE_CONFIRMATION",
                "STALL_THEN_FAILURE",
                "AMBIGUOUS",
            ]
        },
        "primary_cohort_counts": {},
    }


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-entry",    type=Path, default=POST_ENTRY_PATH)
    parser.add_argument("--discovery",     type=Path, default=DISCOVERY_PATH)
    parser.add_argument("--canonical",     type=Path, default=CANONICAL_PATH)
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    # ── load data ──────────────────────────────────────────────────────────────
    pe        = json.loads(args.post_entry.read_text())
    disc      = json.loads(args.discovery.read_text())
    raw_can   = args.canonical.read_bytes()
    canonical = json.loads(raw_can)
    source_sha256 = hashlib.sha256(raw_can).hexdigest()

    outcomes_by_sid = {o["signal_id"]: o for o in disc["outcomes"]}
    trade_obs       = pe["trades"]

    m15_all = sorted(
        [b for b in canonical["m15_bars"] if int(b["time"]) <= DISCOVERY_END],
        key=lambda b: int(b["time"]),
    )
    h1_all = sorted(
        [b for b in canonical["h1_bars"]  if int(b["time"]) <= DISCOVERY_END],
        key=lambda b: int(b["time"]),
    )

    # ── reconcile '74 of 76' error ─────────────────────────────────────────────
    # Count losers touched by each signal (from counterfactual artifact)
    cf_path = args.artifact_root / "kojo_v3_exit_counterfactual.json"
    cf = json.loads(cf_path.read_text())
    reclaim_fired_sids = {
        entry["signal_id"]
        for pol in cf["policy_aggregates"].values()
        if pol["policy"] == "EXIT_ON_KEY_LEVEL_RECLAIM"
        for entry in pol["vs_static"].get("improvements", [])
                    + pol["vs_static"].get("degradations", [])
                    + pol["vs_static"].get("neutral", [])
    }
    h1_fired_sids = {
        entry["signal_id"]
        for pol in cf["policy_aggregates"].values()
        if pol["policy"] == "EXIT_ON_H1_ALIGNMENT_DETERIORATION"
        for entry in pol["vs_static"].get("improvements", [])
                    + pol["vs_static"].get("degradations", [])
                    + pol["vs_static"].get("neutral", [])
    }
    losers = [t for t in trade_obs if t["static_status"] == "STOPPED"]
    loser_sids = {t["signal_id"] for t in losers}
    affected_loser_sids = (reclaim_fired_sids | h1_fired_sids) & loser_sids
    unaffected_loser_count = len(loser_sids) - len(affected_loser_sids)

    reconciliation = {
        "error_in_prior_summary": (
            "The phrase '74 of 76 loser-exits' was an arithmetic error in "
            "the written summary. There are 26 static losers × 3 non-baseline "
            "policies = 78 loser-policy slots, but that framing is also "
            "not meaningful. The correct statement: 18 of 26 static losers "
            "had NO exit signal fire under any policy studied; 8 unique losers "
            "had at least one signal fire (KEY_LEVEL_RECLAIM or "
            "H1_ALIGNMENT_DETERIORATION). '74 of 76' does not correspond to "
            "any valid calculation from the 38-trade dataset."
        ),
        "static_losses": 26,
        "static_wins": 12,
        "losers_with_any_signal_fired": len(affected_loser_sids),
        "losers_unaffected_by_any_signal": unaffected_loser_count,
        "reclaim_fired_on_winner_count": len(reclaim_fired_sids - loser_sids),
        "note": (
            "8 losers had key-level-reclaim fire. 8 had H1-deterioration fire. "
            "The union is still 8 (the identical set of 8 losers), meaning "
            "every H1-deterioration loser also had a reclaim signal. "
            "18 of 26 losers were unreachable by either exit signal."
        ),
    }

    # ── analyze all trades ─────────────────────────────────────────────────────
    winners = [t for t in trade_obs if t["static_status"] == "TARGET_HIT"]
    all_records = []
    for t in trade_obs:
        out     = outcomes_by_sid[t["signal_id"]]
        exit_ts = int(out["exit_timestamp"])
        rec     = _analyze_trade(t, exit_ts, m15_all, h1_all)
        all_records.append(rec)

    loser_recs  = [r for r in all_records if r["static_status"] == "STOPPED"]
    winner_recs = [r for r in all_records if r["static_status"] == "TARGET_HIT"]

    # ── aggregate ─────────────────────────────────────────────────────────────
    loser_agg  = _agg(loser_recs,  "STATIC_LOSERS")
    winner_agg = _agg(winner_recs, "STATIC_WINNERS")

    # fill primary cohort counts
    for agg, recs in [(loser_agg, loser_recs), (winner_agg, winner_recs)]:
        agg["primary_cohort_counts"] = {}
        for c in [
            "PERSISTENT_RECLAIM_BEFORE_FAILURE",
            "TRANSIENT_RECLAIM_THEN_CONTINUATION",
            "STRUCTURE_FLIP_BEFORE_FAILURE",
            "H1_DETERIORATION_LATE_CONFIRMATION",
            "STALL_THEN_FAILURE",
            "AMBIGUOUS",
        ]:
            agg["primary_cohort_counts"][c] = sum(
                1 for r in recs if r["primary_cohort"] == c
            )

    # ── H1 lead time stats ─────────────────────────────────────────────────────
    h1_det_losers = [
        r for r in loser_recs if r.get("h1_deterioration_ts_minutes") is not None
    ]
    h1_lead_vals = [
        r["m15_lead_before_h1_minutes"] for r in h1_det_losers
        if r.get("m15_lead_before_h1_minutes") is not None
    ]

    # ── most common loser sequence ─────────────────────────────────────────────
    from collections import Counter  # noqa: PLC0415
    primary_cohort_counter = Counter(r["primary_cohort"] for r in loser_recs)
    most_common_loser_seq  = primary_cohort_counter.most_common(1)[0][0]
    most_common_count      = primary_cohort_counter.most_common(1)[0][1]

    # does same sequence appear in winners?
    winner_same_seq_count = winner_agg["cohort_counts"].get(most_common_loser_seq, 0)

    # ── immediate-peak losers (MFE on first bar) ───────────────────────────────
    immediate_peak_losers = [r for r in loser_recs if r["mfe_minutes"] == 0]
    immediate_peak_winners = [r for r in winner_recs if r["mfe_minutes"] == 0]

    # ── discriminating structure assessment ───────────────────────────────────
    # Look for features where losers >> winners
    features = {}
    for key in ["transient_reclaim", "persistent_reclaim",
                "opposite_m15_persists", "h1_deterioration"]:
        l_rate = _pct(loser_agg.get(key, 0),  len(loser_recs))
        w_rate = _pct(winner_agg.get(key, 0), len(winner_recs))
        features[key] = {
            "loser_pct": l_rate,
            "winner_pct": w_rate,
            "loser_minus_winner": round(l_rate - w_rate, 1),
        }

    max_discrimination = max(
        f["loser_minus_winner"] for f in features.values()
    )
    discriminating_structure = max_discrimination >= 20.0  # 20pp threshold

    # ── per-cohort time profiles (losers only) ────────────────────────────────
    cohort_profiles = {}
    for cohort in [
        "PERSISTENT_RECLAIM_BEFORE_FAILURE",
        "TRANSIENT_RECLAIM_THEN_CONTINUATION",
        "STRUCTURE_FLIP_BEFORE_FAILURE",
        "H1_DETERIORATION_LATE_CONFIRMATION",
        "STALL_THEN_FAILURE",
        "AMBIGUOUS",
    ]:
        members = [r for r in loser_recs if cohort in r.get("all_cohorts", [])]
        if not members:
            cohort_profiles[cohort] = {"count": 0}
            continue
        cohort_profiles[cohort] = {
            "count": len(members),
            "mfe_r_median": _med([r["mfe_r"] for r in members]),
            "entry_to_mfe_median": _med([r["mfe_minutes"] for r in members]),
            "mfe_to_sl_median": _med([r.get("mfe_to_sl_minutes") for r in members]),
            "mfe_to_first_adverse_median": _med([
                r.get("mfe_to_first_adverse_m15_minutes") for r in members
            ]),
            "new_extension_count": sum(1 for r in members if r["new_favorable_extension_after_mfe"]),
        }

    # ── build artifact ─────────────────────────────────────────────────────────
    artifact = {
        "study_type": "FAILURE_TO_CONTINUE_ANATOMY",
        "v3_entry_commit": V3_COMMIT,
        "post_entry_commit": POST_ENTRY_COMMIT,
        "counterfactual_commit": CF_COMMIT,
        "source_sha256": source_sha256,
        "reconciliation": reconciliation,
        "loser_aggregate": loser_agg,
        "winner_aggregate": winner_agg,
        "cohort_profiles": cohort_profiles,
        "discriminating_features": features,
        "h1_lead_time_stats": {
            "h1_deterioration_loser_count": len(h1_det_losers),
            "h1_deterioration_winner_count": sum(
                1 for r in winner_recs if r.get("h1_deterioration_ts_minutes") is not None
            ),
            "lead_time_values_minutes": h1_lead_vals,
            "median_m15_lead_before_h1": _med(h1_lead_vals),
            "p25": _p25(h1_lead_vals),
            "p75": _p75(h1_lead_vals),
        },
        "most_common_loser_primary_cohort": most_common_loser_seq,
        "most_common_loser_primary_cohort_count": most_common_count,
        "most_common_sequence_in_winners_count": winner_same_seq_count,
        "failure_to_continue_has_discriminating_structure": discriminating_structure,
        "per_trade_anatomy": all_records,
        "observation_methodology": {
            "transient_reclaim": "M15 close crosses level AND at least one subsequent M15 close restores trade side before SL",
            "persistent_reclaim": "M15 close crosses level AND price never returns to trade side before SL",
            "opposite_m15_persists": "first opposite-direction M15 bar is immediately followed by another bar closing in the same adverse direction",
            "new_extension_after_mfe": "any post-MFE M15 bar achieves a more favorable high/low than the MFE bar",
            "h1_deterioration": "H1 bar closes beyond structural level (same test as V3 initial confirmation)",
            "m15_lead_before_h1": "H1 deterioration minutes minus first post-MFE M15 adverse event minutes (positive = M15 earlier)",
            "stall_cohort_threshold": "no level reclaim, no opposite structure persistence, mfe_to_sl > 45 minutes",
            "primary_cohort_priority": "PERSISTENT > TRANSIENT > STRUCTURE_FLIP > H1_DET > STALL > AMBIGUOUS",
            "note": "thresholds are descriptive conventions, not optimization artifacts",
        },
        "immediate_peak_stats": {
            "definition": "trades where time_to_mfe_minutes == 0 (MFE on first post-entry M5 bar)",
            "loser_count": len(immediate_peak_losers),
            "winner_count": len(immediate_peak_winners),
            "loser_sl_times_minutes": sorted(r["sl_minutes"] for r in immediate_peak_losers),
            "loser_primary_cohorts": dict(
                Counter(r["primary_cohort"] for r in immediate_peak_losers)
            ),
            "interpretation": (
                "These losers peaked on the entry bar itself: the entry bar's "
                "intrabar high/low was the trade's best price. Any management "
                "rule acting on completed bars cannot act before the first "
                "M15 bar closes (15 min), by which time most of these trades "
                "have already deteriorated."
            ),
        },
        "no_management_policy_created": True,
        "no_exit_rule_optimized": True,
        "no_threshold_tuned": True,
        "no_parameter_search": True,
        "validation_outcomes_accessed": False,
        "entry_evaluator_changed": False,
        "production_changed": False,
        "broker_writes": 0,
    }

    ab = json.dumps(artifact, sort_keys=True).encode()
    artifact_fp = hashlib.sha256(ab).hexdigest()
    artifact["artifact_fingerprint"] = artifact_fp

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    out_path = args.artifact_root / "kojo_v3_failure_anatomy.json"
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"artifact: {out_path}", file=sys.stderr)

    # ── required report ────────────────────────────────────────────────────────
    report = {
        "RECONCILIATION": reconciliation,
        "STATIC_LOSSES": 26,
        "STATIC_WINNERS": 12,

        "LOSERS_WITH_TRANSIENT_RECLAIM":  loser_agg["transient_reclaim"],
        "WINNERS_WITH_TRANSIENT_RECLAIM": winner_agg["transient_reclaim"],
        "LOSERS_WITH_PERSISTENT_RECLAIM":  loser_agg["persistent_reclaim"],
        "WINNERS_WITH_PERSISTENT_RECLAIM": winner_agg["persistent_reclaim"],

        "LOSERS_WITH_OPPOSITE_STRUCTURE_PERSISTENCE":  loser_agg["opposite_m15_persists"],
        "WINNERS_WITH_OPPOSITE_STRUCTURE_PERSISTENCE": winner_agg["opposite_m15_persists"],

        "LOSERS_MEDIAN_ENTRY_TO_MFE_MIN":  loser_agg["entry_to_mfe_median"],
        "WINNERS_MEDIAN_ENTRY_TO_MFE_MIN": winner_agg["entry_to_mfe_median"],

        "LOSERS_MEDIAN_MFE_TO_FIRST_ADVERSE_EVENT_MIN":  loser_agg["mfe_to_first_adverse_m15_median"],
        "WINNERS_MEDIAN_MFE_TO_FIRST_ADVERSE_EVENT_MIN": winner_agg["mfe_to_first_adverse_m15_median"],

        "LOSERS_MEDIAN_MFE_TO_SL_MIN": loser_agg["mfe_to_sl_median"],

        "H1_DETERIORATION_COUNT": len(h1_det_losers),
        "MEDIAN_M15_LEAD_TIME_BEFORE_H1_DETERIORATION_MIN": _med(h1_lead_vals),

        "COHORT_DISTRIBUTION_LOSERS": loser_agg["primary_cohort_counts"],
        "COHORT_DISTRIBUTION_LOSERS_ALL_APPLICABLE": loser_agg["cohort_counts"],
        "COHORT_DISTRIBUTION_WINNERS_ALL_APPLICABLE": winner_agg["cohort_counts"],

        "MOST_COMMON_LOSER_SEQUENCE": most_common_loser_seq,
        "MOST_COMMON_COUNT": most_common_count,
        "DOES_SAME_SEQUENCE_COMMONLY_OCCUR_IN_WINNERS": winner_same_seq_count > (len(winner_recs) // 4),
        "SAME_SEQUENCE_IN_WINNERS_COUNT": winner_same_seq_count,

        "DISCRIMINATING_FEATURES": features,
        "FAILURE_TO_CONTINUE_HAS_DISCRIMINATING_STRUCTURE": discriminating_structure,

        "IMMEDIATE_PEAK_LOSER_COUNT": len(immediate_peak_losers),
        "IMMEDIATE_PEAK_WINNER_COUNT": len(immediate_peak_winners),
        "IMMEDIATE_PEAK_NOTE": (
            f"{len(immediate_peak_losers)}/26 losers had MFE on the first "
            "post-entry bar (minute 0). These peaked before any completed "
            "M15 bar closed; bar-close exit rules cannot act in time."
        ),

        "H1_LEAD_TIME_BASIS": "actionable (bar-close to bar-close); M15 close = open+15min, H1 close = open+60min",

        "ANATOMY_ARTIFACT": str(out_path),
        "ANATOMY_ARTIFACT_FINGERPRINT": artifact_fp,

        "TRADE_MANAGER_POLICY_CREATED": False,
        "EXIT_RULE_OPTIMIZED": False,
        "PARAMETER_SEARCH": False,
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "ENTRY_EVALUATOR_CHANGED": False,
        "PRODUCTION_CHANGED": False,
        "BROKER_WRITES": 0,
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
