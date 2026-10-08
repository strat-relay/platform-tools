"""Post-entry behavior diagnostic for KOJO_STRUCTURE_RECLAIM_V3 discovery trades.

Read-only observational study of the 38 accepted trades from the frozen
V3 discovery run (kojo-v3-discovery-run-1, commit c82d290).

Safety constraints:
    BROKER_WRITES=0
    VALIDATION_OUTCOMES_ACCESSED=false
    ENTRY_EVALUATOR_CHANGED=false
    TARGET_EVALUATOR_CHANGED=false
    PRODUCTION_CHANGED=false
    PARAMETER_SEARCH=false
    TRADE_MANAGER_POLICY_CREATED=false
    EXIT_RULE_OPTIMIZED=false
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, quantiles
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy_backtest.kojo_structure_reclaim_v3 import (
    _rejection_wick_bullish,
    _rejection_wick_bearish,
    WICK_REACTION_MIN_FRACTION,
    BODY_CLOSE_MIN_FRACTION,
)

UTC = timezone.utc
DISCOVERY_START = 1782864000
DISCOVERY_END   = 1786319999
M5_SECONDS  = 300
M15_SECONDS = 900
H1_SECONDS  = 3600
V3_COMMIT   = "c82d290"
SAME_BAR_POLICY = "CONSERVATIVE_STOP_FIRST"

CANONICAL_PATH = Path(
    "/private/tmp/claude-501/-Users-caleb-mt5-native-bridge/"
    "4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"
)
DISCOVERY_RESULT_PATH = Path(
    "artifacts/backtests/kojo-v3-discovery-run-1/"
    "kojo-v3-discovery-run-1/result.json"
)


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 2) if d else 0.0


def _median_or_none(values: list[float]) -> float | None:
    return round(median(values), 4) if values else None


def _pct25_or_none(values: list[float]) -> float | None:
    if len(values) < 2:
        return round(values[0], 4) if values else None
    return round(quantiles(values, n=4)[0], 4)


def _pct75_or_none(values: list[float]) -> float | None:
    if len(values) < 2:
        return round(values[0], 4) if values else None
    return round(quantiles(values, n=4)[2], 4)


def _body_close_bearish(b: dict) -> bool:
    o, h, lo, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
    total_range = h - lo
    if total_range < 1e-9:
        return False
    body = abs(c - o)
    return c < o and body / total_range >= BODY_CLOSE_MIN_FRACTION


def _body_close_bullish(b: dict) -> bool:
    o, h, lo, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
    total_range = h - lo
    if total_range < 1e-9:
        return False
    body = abs(c - o)
    return c > o and body / total_range >= BODY_CLOSE_MIN_FRACTION


# ──────────────────────────────────────────────────────────────────────────────
# per-trade post-entry simulation
# ──────────────────────────────────────────────────────────────────────────────

def _simulate_trade(
    sig: dict,
    outcome: dict,
    m5_bars: list[dict],
    m15_bars: list[dict],
    h1_bars: list[dict],
) -> dict:
    direction    = sig["direction"]
    entry_price  = float(sig["entry_price"])
    stop_price   = float(sig["stop_price"])
    tp1_price    = float(sig["provenance"]["tp1"])
    tp2_raw      = sig["provenance"].get("tp2")
    tp2_price    = float(tp2_raw) if tp2_raw is not None else None
    decision_ts  = int(sig["decision_timestamp"])
    level_price  = float(sig["provenance"]["structural_level_price"])
    signal_id    = sig["signal_id"]

    risk = abs(entry_price - stop_price)
    if risk < 1e-9:
        risk = 1e-9

    is_long = direction == "LONG"

    # static outcome from backtest engine
    static_status   = outcome["status"]          # TARGET_HIT | STOPPED
    static_exit_ts  = outcome["exit_timestamp"]
    static_realized = float(outcome["realized_r"])

    # post-entry M5 bars: completed bars with open_ts >= decision_ts and
    # close_ts <= static_exit_ts (don't peek past exit)
    post_m5 = [
        b for b in m5_bars
        if int(b["time"]) >= decision_ts and int(b["time"]) + M5_SECONDS <= static_exit_ts + 1
    ]

    # post-entry M15 bars
    post_m15 = [
        b for b in m15_bars
        if int(b["time"]) >= decision_ts and int(b["time"]) + M15_SECONDS <= static_exit_ts + 1
    ]

    # post-entry H1 bars
    post_h1 = [
        b for b in h1_bars
        if int(b["time"]) >= decision_ts and int(b["time"]) + H1_SECONDS <= static_exit_ts + 1
    ]

    # ── excursion tracking ─────────────────────────────────────────────────────
    max_favorable_price = entry_price
    max_adverse_price   = entry_price
    mfe_r = 0.0
    mae_r = 0.0
    time_to_mfe   = None
    time_to_mae   = None

    milestones = {0.25: None, 0.50: None, 0.75: None, 1.00: None}
    tp1_hit_ts   = None
    tp2_hit_ts   = None

    minutes_from_entry = lambda ts: round((ts - decision_ts) / 60)

    for bar in post_m5:
        t   = int(bar["time"])
        lo  = float(bar["low"])
        hi  = float(bar["high"])
        mins = minutes_from_entry(t)

        if is_long:
            fav_price = hi
            adv_price = lo
        else:
            fav_price = lo  # lower = more favorable for short
            adv_price = hi

        # favorable excursion
        if is_long:
            fav_r = (fav_price - entry_price) / risk
        else:
            fav_r = (entry_price - fav_price) / risk

        if fav_r > mfe_r:
            mfe_r = fav_r
            max_favorable_price = fav_price
            time_to_mfe = mins

        # adverse excursion
        if is_long:
            adv_r = (entry_price - adv_price) / risk
        else:
            adv_r = (adv_price - entry_price) / risk

        if adv_r > mae_r:
            mae_r = adv_r
            max_adverse_price = adv_price
            time_to_mae = mins

        # milestone detection
        for frac in (0.25, 0.50, 0.75, 1.00):
            if milestones[frac] is None and fav_r >= frac:
                milestones[frac] = mins

        # TP1 / TP2 hit detection (using bar range, conservative on same-bar)
        if tp1_hit_ts is None:
            if is_long and hi >= tp1_price:
                tp1_hit_ts = mins
            elif not is_long and lo <= tp1_price:
                tp1_hit_ts = mins

        if tp2_price is not None and tp2_hit_ts is None:
            if is_long and hi >= tp2_price:
                tp2_hit_ts = mins
            elif not is_long and lo <= tp2_price:
                tp2_hit_ts = mins

    # ── broken level reclaim (M15) ─────────────────────────────────────────────
    level_reclaim_ts    = None
    level_reclaim_price = None
    level_reclaim_r     = None

    for bar in post_m15:
        t = int(bar["time"])
        c = float(bar["close"])
        if is_long:
            reclaimed = c < level_price
        else:
            reclaimed = c > level_price
        if reclaimed and level_reclaim_ts is None:
            level_reclaim_ts    = minutes_from_entry(t)
            level_reclaim_price = c
            if is_long:
                level_reclaim_r = (c - entry_price) / risk
            else:
                level_reclaim_r = (entry_price - c) / risk
            level_reclaim_r = round(level_reclaim_r, 4)

    # ── opposite M15 rejection ─────────────────────────────────────────────────
    opp_rejection_ts   = None
    opp_rejection_type = None
    opp_rejection_r    = None

    for bar in post_m15:
        if opp_rejection_ts is not None:
            break
        t = int(bar["time"])
        c = float(bar["close"])
        detected = False
        rtype    = None

        if is_long:
            # opposite for LONG = bearish rejection
            if _rejection_wick_bearish(bar):
                detected = True
                rtype = "WICK_REJECTION_BEARISH"
            elif _body_close_bearish(bar):
                detected = True
                rtype = "BODY_CLOSE_BEARISH"
        else:
            # opposite for SHORT = bullish rejection
            if _rejection_wick_bullish(bar):
                detected = True
                rtype = "WICK_REJECTION_BULLISH"
            elif _body_close_bullish(bar):
                detected = True
                rtype = "BODY_CLOSE_BULLISH"

        if detected:
            opp_rejection_ts   = minutes_from_entry(t)
            opp_rejection_type = rtype
            if is_long:
                opp_rejection_r = (c - entry_price) / risk
            else:
                opp_rejection_r = (entry_price - c) / risk
            opp_rejection_r = round(opp_rejection_r, 4)

    # ── H1 alignment deterioration ─────────────────────────────────────────────
    # Proxy: first completed H1 bar whose close crosses back through the
    # structural level against the trade direction. This reuses the same
    # H1-close-vs-level test that V3 uses for initial H1 confirmation.
    align_det_ts     = None
    align_det_reason = None
    align_det_r      = None

    for bar in post_h1:
        t = int(bar["time"])
        c = float(bar["close"])
        if is_long:
            deteriorated = c < level_price
        else:
            deteriorated = c > level_price
        if deteriorated and align_det_ts is None:
            align_det_ts     = minutes_from_entry(t)
            align_det_reason = "H1_CLOSE_RECROSSES_STRUCTURAL_LEVEL"
            if is_long:
                align_det_r = (c - entry_price) / risk
            else:
                align_det_r = (entry_price - c) / risk
            align_det_r = round(align_det_r, 4)

    # ── TP1/TP2 path behavior ──────────────────────────────────────────────────
    tp1_reached = tp1_hit_ts is not None
    tp2_reached = tp2_hit_ts is not None

    if tp1_reached:
        max_r_after_tp1_fav = max(
            (
                ((float(b["high"]) - tp1_price) / risk if is_long
                 else (tp1_price - float(b["low"])) / risk)
                for b in post_m5
                if minutes_from_entry(int(b["time"])) > tp1_hit_ts
            ),
            default=0.0,
        )
        tp1_path = {
            "tp1_reached": True,
            "max_r_after_tp1": round(max_r_after_tp1_fav, 4),
            "tp2_reached": tp2_reached,
        }
    else:
        # fraction of TP1 distance achieved
        tp1_dist = abs(tp1_price - entry_price)
        frac_achieved = mfe_r * risk / tp1_dist if tp1_dist > 1e-9 else 0.0
        nearest_r = mfe_r
        tp1_path = {
            "tp1_reached": False,
            "fraction_of_tp1_distance_achieved": round(frac_achieved, 4),
            "nearest_favorable_r": round(nearest_r, 4),
            "adverse_event_before_tp1": (
                level_reclaim_ts is not None
                or opp_rejection_ts is not None
                or align_det_ts is not None
            ),
        }

    # ── any source-supported adverse event before SL ──────────────────────────
    # use earliest timestamp among observed adverse events
    adverse_events = [
        ts for ts in [level_reclaim_ts, opp_rejection_ts, align_det_ts]
        if ts is not None
    ]
    first_adverse_ts = min(adverse_events) if adverse_events else None

    # R at first adverse event
    r_at_first_adverse = None
    if first_adverse_ts is not None:
        for ts, r in [
            (level_reclaim_ts, level_reclaim_r),
            (opp_rejection_ts, opp_rejection_r),
            (align_det_ts, align_det_r),
        ]:
            if ts == first_adverse_ts:
                r_at_first_adverse = r
                break

    has_adverse_before_terminal = first_adverse_ts is not None  # always before terminal since we clamp post_m5

    # ── loser cohort ───────────────────────────────────────────────────────────
    loser_cohort = None
    if static_status == "STOPPED":
        if has_adverse_before_terminal:
            loser_cohort = "THESIS_DEGRADED_BEFORE_SL"
        elif mfe_r >= 0.50:
            loser_cohort = "FAVORABLE_THEN_REVERSED"
        elif mfe_r < 0.25:
            loser_cohort = "IMMEDIATE_FAILURE"
        else:
            loser_cohort = "AMBIGUOUS"

    return {
        "signal_id": signal_id,
        "direction": direction,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "risk_points": round(risk, 4),
        "level_price": level_price,
        "decision_ts": decision_ts,
        "static_status": static_status,
        "static_realized_r": round(static_realized, 4),
        # excursion
        "mfe_r": round(mfe_r, 4),
        "mae_r": round(mae_r, 4),
        "max_favorable_price": round(max_favorable_price, 4),
        "max_adverse_price": round(max_adverse_price, 4),
        "time_to_mfe_minutes": time_to_mfe,
        "time_to_mae_minutes": time_to_mae,
        # milestones
        "hit_0_25r": milestones[0.25] is not None,
        "time_to_0_25r": milestones[0.25],
        "hit_0_50r": milestones[0.50] is not None,
        "time_to_0_50r": milestones[0.50],
        "hit_0_75r": milestones[0.75] is not None,
        "time_to_0_75r": milestones[0.75],
        "hit_1_00r": milestones[1.00] is not None,
        "time_to_1_00r": milestones[1.00],
        "hit_tp1": tp1_reached,
        "time_to_tp1": tp1_hit_ts,
        "hit_tp2": tp2_reached,
        "time_to_tp2": tp2_hit_ts,
        # broken level reclaim
        "key_level_reclaimed": level_reclaim_ts is not None,
        "time_to_first_reclaim": level_reclaim_ts,
        "price_at_reclaim": level_reclaim_price,
        "trade_r_at_reclaim": level_reclaim_r,
        # opposite M15 rejection
        "opposite_m15_rejection_seen": opp_rejection_ts is not None,
        "opposite_m15_rejection_type": opp_rejection_type,
        "opposite_m15_rejection_ts_minutes": opp_rejection_ts,
        "trade_r_at_rejection": opp_rejection_r,
        # H1 alignment deterioration
        "alignment_deterioration_seen": align_det_ts is not None,
        "alignment_deterioration_ts_minutes": align_det_ts,
        "alignment_deterioration_reason": align_det_reason,
        "trade_r_at_deterioration": align_det_r,
        "alignment_diagnostic_unavailable": False,
        # TP path
        "tp1_path": tp1_path,
        # adverse event summary
        "any_adverse_event_before_terminal": has_adverse_before_terminal,
        "first_adverse_event_ts_minutes": first_adverse_ts,
        "trade_r_at_first_adverse_event": r_at_first_adverse,
        # loser cohort
        "loser_cohort": loser_cohort,
    }


# ──────────────────────────────────────────────────────────────────────────────
# aggregate statistics
# ──────────────────────────────────────────────────────────────────────────────

def _agg_profile(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "count": 0}

    mfe_rs = [t["mfe_r"] for t in trades]
    mae_rs = [t["mae_r"] for t in trades]

    tt_05 = [t["time_to_0_50r"] for t in trades if t["time_to_0_50r"] is not None]
    tt_10 = [t["time_to_1_00r"] for t in trades if t["time_to_1_00r"] is not None]

    lv_reclaim   = sum(1 for t in trades if t["key_level_reclaimed"])
    opp_rejection = sum(1 for t in trades if t["opposite_m15_rejection_seen"])
    align_det    = sum(1 for t in trades if t["alignment_deterioration_seen"])

    return {
        "label": label,
        "count": len(trades),
        "mfe_median": _median_or_none(mfe_rs),
        "mfe_p25": _pct25_or_none(mfe_rs),
        "mfe_p75": _pct75_or_none(mfe_rs),
        "mae_median": _median_or_none(mae_rs),
        "mae_p25": _pct25_or_none(mae_rs),
        "mae_p75": _pct75_or_none(mae_rs),
        "time_to_0_50r_median_minutes": _median_or_none(tt_05),
        "time_to_1_00r_median_minutes": _median_or_none(tt_10),
        "key_level_reclaim_count": lv_reclaim,
        "key_level_reclaim_pct": _pct(lv_reclaim, len(trades)),
        "opposite_m15_rejection_count": opp_rejection,
        "opposite_m15_rejection_pct": _pct(opp_rejection, len(trades)),
        "alignment_deterioration_count": align_det,
        "alignment_deterioration_pct": _pct(align_det, len(trades)),
    }


def _counterfactual_event(trades: list[dict], event_key: str, r_key: str, label: str) -> dict:
    event_trades = [t for t in trades if t[event_key]]
    if not event_trades:
        return {"event_type": label, "trade_count_with_event": 0}

    r_vals = [t[r_key] for t in event_trades if t[r_key] is not None]
    before_sl   = sum(1 for t in event_trades if t["static_status"] == "STOPPED")
    before_tp1  = sum(1 for t in event_trades if t["static_status"] == "TARGET_HIT")
    in_loser    = sum(1 for t in event_trades if t["static_status"] == "STOPPED")
    in_winner   = sum(1 for t in event_trades if t["static_status"] == "TARGET_HIT")

    return {
        "event_type": label,
        "trade_count_with_event": len(event_trades),
        "median_r_at_event": _median_or_none(r_vals),
        "p25_r_at_event": _pct25_or_none(r_vals),
        "p75_r_at_event": _pct75_or_none(r_vals),
        "event_before_sl_count": before_sl,
        "event_before_tp1_count": before_tp1,
        "event_in_static_loser_count": in_loser,
        "event_in_static_winner_count": in_winner,
    }


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="V3 post-entry behavior diagnostic")
    parser.add_argument("--canonical",      type=Path, default=CANONICAL_PATH)
    parser.add_argument("--discovery",      type=Path, default=DISCOVERY_RESULT_PATH)
    parser.add_argument("--artifact-root",  type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    # ── load data ──────────────────────────────────────────────────────────────
    raw_canonical  = args.canonical.read_bytes()
    source_sha256  = hashlib.sha256(raw_canonical).hexdigest()
    canonical      = json.loads(raw_canonical)

    discovery_raw  = args.discovery.read_bytes()
    discovery_fp   = hashlib.sha256(discovery_raw).hexdigest()
    discovery      = json.loads(discovery_raw)

    # filter to discovery window only — no validation data accessed
    m5_bars  = sorted(
        [b for b in canonical["m5_bars"]  if int(b["time"]) <= DISCOVERY_END],
        key=lambda b: int(b["time"]),
    )
    m15_bars = sorted(
        [b for b in canonical["m15_bars"] if int(b["time"]) <= DISCOVERY_END],
        key=lambda b: int(b["time"]),
    )
    h1_bars  = sorted(
        [b for b in canonical["h1_bars"]  if int(b["time"]) <= DISCOVERY_END],
        key=lambda b: int(b["time"]),
    )

    signals  = discovery["signals"]
    outcomes = {o["signal_id"]: o for o in discovery["outcomes"]}

    print(f"signals: {len(signals)}, outcomes: {len(outcomes)}", file=sys.stderr)
    print(f"M5: {len(m5_bars)}, M15: {len(m15_bars)}, H1: {len(h1_bars)}", file=sys.stderr)

    # ── per-trade simulation ───────────────────────────────────────────────────
    trade_obs: list[dict] = []
    for sig in signals:
        sid = sig["signal_id"]
        out = outcomes.get(sid)
        if out is None:
            print(f"WARNING: no outcome for {sid}", file=sys.stderr)
            continue
        obs = _simulate_trade(sig, out, m5_bars, m15_bars, h1_bars)
        trade_obs.append(obs)

    # ── partition into winners / losers ───────────────────────────────────────
    winners = [t for t in trade_obs if t["static_status"] == "TARGET_HIT"]
    losers  = [t for t in trade_obs if t["static_status"] == "STOPPED"]

    n_total   = len(trade_obs)
    n_winners = len(winners)
    n_losers  = len(losers)

    # ── milestone rates ────────────────────────────────────────────────────────
    pct_all_0_25r = _pct(sum(1 for t in trade_obs if t["hit_0_25r"]), n_total)
    pct_all_0_50r = _pct(sum(1 for t in trade_obs if t["hit_0_50r"]), n_total)
    pct_all_1_00r = _pct(sum(1 for t in trade_obs if t["hit_1_00r"]), n_total)

    pct_los_0_25r = _pct(sum(1 for t in losers if t["hit_0_25r"]), n_losers)
    pct_los_0_50r = _pct(sum(1 for t in losers if t["hit_0_50r"]), n_losers)
    pct_los_1_00r = _pct(sum(1 for t in losers if t["hit_1_00r"]), n_losers)

    # ── adverse event rates ────────────────────────────────────────────────────
    n_lv_reclaim   = sum(1 for t in trade_obs if t["key_level_reclaimed"])
    n_opp_rej      = sum(1 for t in trade_obs if t["opposite_m15_rejection_seen"])
    n_align_det    = sum(1 for t in trade_obs if t["alignment_deterioration_seen"])

    losers_with_any_adverse = sum(1 for t in losers if t["any_adverse_event_before_terminal"])
    pct_losers_adverse = _pct(losers_with_any_adverse, n_losers)

    # ── loser cohorts ──────────────────────────────────────────────────────────
    immediate_fail_count       = sum(1 for t in losers if t["loser_cohort"] == "IMMEDIATE_FAILURE")
    favorable_reversed_count   = sum(1 for t in losers if t["loser_cohort"] == "FAVORABLE_THEN_REVERSED")
    thesis_degraded_count      = sum(1 for t in losers if t["loser_cohort"] == "THESIS_DEGRADED_BEFORE_SL")
    ambiguous_count            = sum(1 for t in losers if t["loser_cohort"] == "AMBIGUOUS")

    # ── winner / loser profiles ────────────────────────────────────────────────
    winner_profile = _agg_profile(winners, "STATIC_WINNERS")
    loser_profile  = _agg_profile(losers,  "STATIC_LOSERS")

    # ── loser sub-profiles ─────────────────────────────────────────────────────
    loser_profiles_by_cohort = {
        c: _agg_profile([t for t in losers if t["loser_cohort"] == c], c)
        for c in ("IMMEDIATE_FAILURE", "FAVORABLE_THEN_REVERSED", "THESIS_DEGRADED_BEFORE_SL", "AMBIGUOUS")
    }

    # ── counterfactual event study (H) ────────────────────────────────────────
    cf_level_reclaim  = _counterfactual_event(trade_obs, "key_level_reclaimed",      "trade_r_at_reclaim",       "KEY_LEVEL_RECLAIM")
    cf_opp_rejection  = _counterfactual_event(trade_obs, "opposite_m15_rejection_seen", "trade_r_at_rejection",  "OPPOSITE_M15_REJECTION")
    cf_align_det      = _counterfactual_event(trade_obs, "alignment_deterioration_seen", "trade_r_at_deterioration", "ALIGNMENT_DETERIORATION")

    # ── entry edge diagnostic (I) ─────────────────────────────────────────────
    losers_mfe_median  = _median_or_none([t["mfe_r"] for t in losers])
    winners_mfe_median = _median_or_none([t["mfe_r"] for t in winners])

    # Directional edge assessment:
    # Evidence FOR edge: meaningful % of losers reach 0.25–0.50R (not immediate failure),
    # AND losers have source-supported adverse events explaining reversals.
    # Evidence AGAINST: most losers never move favorably at all.
    if n_total < 30:
        edge_diagnostic = "INSUFFICIENT_SAMPLE"
    elif pct_los_0_25r >= 40 and pct_losers_adverse >= 50:
        edge_diagnostic = "PROMISING_BUT_MANAGEMENT_SENSITIVE"
    elif pct_los_0_25r < 25 and immediate_fail_count / n_losers > 0.5:
        edge_diagnostic = "WEAK_ENTRY_EDGE"
    else:
        edge_diagnostic = "MIXED_INCONCLUSIVE"

    # ── build artifact ─────────────────────────────────────────────────────────
    artifact = {
        "study_type": "POST_ENTRY_BEHAVIOR_DIAGNOSTIC",
        "v3_entry_commit": V3_COMMIT,
        "discovery_result_fingerprint": discovery_fp,
        "canonical_source_sha256": source_sha256,
        "discovery_start": datetime.fromtimestamp(DISCOVERY_START, UTC).isoformat(),
        "discovery_end":   datetime.fromtimestamp(DISCOVERY_END,   UTC).isoformat(),
        "same_bar_policy": SAME_BAR_POLICY,
        "post_entry_bar_source": "M5_COMPLETED_BARS",
        "level_reclaim_basis": "COMPLETED_M15_CLOSE_CROSSES_STRUCTURAL_LEVEL",
        "opposite_rejection_primitives": [
            "_rejection_wick_bullish/_rejection_wick_bearish (V3 identical)",
            "body_close_bearish/bullish (V3 BODY_CLOSE_MIN_FRACTION=0.25)",
        ],
        "alignment_deterioration_proxy": "H1_CLOSE_RECROSSES_STRUCTURAL_LEVEL (same test as V3 initial H1 confirmation)",
        "loser_cohort_conventions": {
            "THESIS_DEGRADED_BEFORE_SL": "any adverse event (level reclaim | opp rejection | H1 deterioration) before terminal",
            "FAVORABLE_THEN_REVERSED": "no adverse event, mfe_r >= 0.50",
            "IMMEDIATE_FAILURE": "no adverse event, mfe_r < 0.25",
            "AMBIGUOUS": "remaining; 0.25 <= mfe_r < 0.50 without adverse event",
            "priority_order": "THESIS_DEGRADED first, then FAVORABLE_THEN_REVERSED, then IMMEDIATE_FAILURE, then AMBIGUOUS",
            "note": "thresholds are diagnostic conventions, not optimization results",
        },
        "no_management_policy_created": True,
        "no_exit_rule_optimized": True,
        "no_parameter_search": True,
        "validation_outcomes_accessed": False,
        "entry_evaluator_changed": False,
        "target_evaluator_changed": False,
        "production_changed": False,
        "broker_writes": 0,
        # ── per-trade observations ─────────────────────────────────────────────
        "trades": trade_obs,
        # ── aggregate section A ────────────────────────────────────────────────
        "section_a_excursion": {
            "all_trades": {
                "mfe_median": _median_or_none([t["mfe_r"] for t in trade_obs]),
                "mfe_p25": _pct25_or_none([t["mfe_r"] for t in trade_obs]),
                "mfe_p75": _pct75_or_none([t["mfe_r"] for t in trade_obs]),
                "mae_median": _median_or_none([t["mae_r"] for t in trade_obs]),
                "mae_p25": _pct25_or_none([t["mae_r"] for t in trade_obs]),
                "mae_p75": _pct75_or_none([t["mae_r"] for t in trade_obs]),
            },
            "winners": {
                "mfe_median": winners_mfe_median,
                "mae_median": _median_or_none([t["mae_r"] for t in winners]),
            },
            "losers": {
                "mfe_median": losers_mfe_median,
                "mae_median": _median_or_none([t["mae_r"] for t in losers]),
                "loss_with_mfe_lt_0_25r": sum(1 for t in losers if not t["hit_0_25r"]),
                "loss_after_reaching_0_25r": sum(1 for t in losers if t["hit_0_25r"]),
                "loss_after_reaching_0_50r": sum(1 for t in losers if t["hit_0_50r"]),
                "loss_after_reaching_1r": sum(1 for t in losers if t["hit_1_00r"]),
            },
        },
        # ── aggregate section B ────────────────────────────────────────────────
        "section_b_level_reclaim": {
            "total_events": n_lv_reclaim,
            "losers_with_reclaim": sum(1 for t in losers if t["key_level_reclaimed"]),
            "winners_with_reclaim": sum(1 for t in winners if t["key_level_reclaimed"]),
            "pct_losers_reclaim_before_sl": _pct(sum(1 for t in losers if t["key_level_reclaimed"]), n_losers),
            "pct_winners_reclaim_before_tp1": _pct(sum(1 for t in winners if t["key_level_reclaimed"]), n_winners),
        },
        # ── aggregate section C ────────────────────────────────────────────────
        "section_c_opposite_rejection": {
            "total_events": n_opp_rej,
            "losers_with_event": sum(1 for t in losers if t["opposite_m15_rejection_seen"]),
            "winners_with_event": sum(1 for t in winners if t["opposite_m15_rejection_seen"]),
            "pct_losers": _pct(sum(1 for t in losers if t["opposite_m15_rejection_seen"]), n_losers),
            "pct_winners": _pct(sum(1 for t in winners if t["opposite_m15_rejection_seen"]), n_winners),
        },
        # ── aggregate section D ────────────────────────────────────────────────
        "section_d_failure_to_continue": {
            "all_losers": {
                "time_to_first_0_25r_median": _median_or_none(
                    [t["time_to_0_25r"] for t in losers if t["time_to_0_25r"] is not None]
                ),
                "time_to_first_0_50r_median": _median_or_none(
                    [t["time_to_0_50r"] for t in losers if t["time_to_0_50r"] is not None]
                ),
                "time_to_first_1r_median": _median_or_none(
                    [t["time_to_1_00r"] for t in losers if t["time_to_1_00r"] is not None]
                ),
            },
            "never_positive_0_25r": sum(1 for t in losers if not t["hit_0_25r"]),
            "reached_0_25r_then_failed": sum(1 for t in losers if t["hit_0_25r"] and not t["hit_0_50r"]),
            "reached_0_50r_then_failed": sum(1 for t in losers if t["hit_0_50r"] and not t["hit_1_00r"]),
            "reached_1r_then_failed": sum(1 for t in losers if t["hit_1_00r"]),
        },
        # ── aggregate section E ────────────────────────────────────────────────
        "section_e_alignment_deterioration": {
            "total_events": n_align_det,
            "losers_with_event": sum(1 for t in losers if t["alignment_deterioration_seen"]),
            "winners_with_event": sum(1 for t in winners if t["alignment_deterioration_seen"]),
            "alignment_diagnostic_unavailable": False,
            "diagnostic_method": "H1_CLOSE_RECROSSES_STRUCTURAL_LEVEL",
        },
        # ── aggregate section G ────────────────────────────────────────────────
        "section_g_anatomy": {
            "winner_profile": winner_profile,
            "loser_profile": loser_profile,
            "loser_cohort_profiles": loser_profiles_by_cohort,
            "loser_cohort_counts": {
                "IMMEDIATE_FAILURE": immediate_fail_count,
                "FAVORABLE_THEN_REVERSED": favorable_reversed_count,
                "THESIS_DEGRADED_BEFORE_SL": thesis_degraded_count,
                "AMBIGUOUS": ambiguous_count,
            },
        },
        # ── aggregate section H ────────────────────────────────────────────────
        "section_h_counterfactual": {
            "key_level_reclaim": cf_level_reclaim,
            "opposite_m15_rejection": cf_opp_rejection,
            "alignment_deterioration": cf_align_det,
        },
        # ── aggregate section I ────────────────────────────────────────────────
        "section_i_entry_edge": {
            "static_wins": n_winners,
            "static_losses": n_losers,
            "winners_median_mfe_r": winners_mfe_median,
            "losers_median_mfe_r": losers_mfe_median,
            "pct_all_trades_reach_0_25r": pct_all_0_25r,
            "pct_all_trades_reach_0_50r": pct_all_0_50r,
            "pct_all_trades_reach_1r": pct_all_1_00r,
            "pct_static_losers_reach_0_25r": pct_los_0_25r,
            "pct_static_losers_reach_0_50r": pct_los_0_50r,
            "pct_static_losers_reach_1r": pct_los_1_00r,
            "pct_static_losers_with_source_adverse_event_before_sl": pct_losers_adverse,
            "entry_directional_edge_diagnostic": edge_diagnostic,
            "edge_diagnostic_reasoning": (
                f"n_total={n_total}, pct_losers_reach_0.25r={pct_los_0_25r}%, "
                f"pct_losers_adverse_event={pct_losers_adverse}%, "
                f"immediate_failures={immediate_fail_count}/{n_losers}"
            ),
        },
    }

    # fingerprint artifact
    artifact_bytes = json.dumps(artifact, sort_keys=True).encode()
    artifact_fp = hashlib.sha256(artifact_bytes).hexdigest()
    artifact["artifact_fingerprint"] = artifact_fp

    # write
    args.artifact_root.mkdir(parents=True, exist_ok=True)
    out_path = args.artifact_root / "kojo_v3_post_entry_behavior.json"
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"artifact: {out_path}", file=sys.stderr)

    # ── required final report ──────────────────────────────────────────────────
    report = {
        "V3_ENTRY_COMMIT": V3_COMMIT,
        "POST_ENTRY_ARTIFACT": str(out_path),
        "POST_ENTRY_ARTIFACT_FINGERPRINT": artifact_fp,
        "TOTAL_TRADES": n_total,
        "STATIC_WINS": n_winners,
        "STATIC_LOSSES": n_losers,
        "LOSERS_MEDIAN_MFE_R": losers_mfe_median,
        "LOSERS_MEDIAN_MAE_R": _median_or_none([t["mae_r"] for t in losers]),
        "WINNERS_MEDIAN_MFE_R": winners_mfe_median,
        "WINNERS_MEDIAN_MAE_R": _median_or_none([t["mae_r"] for t in winners]),
        "PCT_ALL_REACH_0_25R": pct_all_0_25r,
        "PCT_ALL_REACH_0_50R": pct_all_0_50r,
        "PCT_ALL_REACH_1R": pct_all_1_00r,
        "PCT_LOSERS_REACH_0_25R": pct_los_0_25r,
        "PCT_LOSERS_REACH_0_50R": pct_los_0_50r,
        "PCT_LOSERS_REACH_1R": pct_los_1_00r,
        "KEY_LEVEL_RECLAIM_EVENTS": n_lv_reclaim,
        "OPPOSITE_M15_REJECTION_EVENTS": n_opp_rej,
        "ALIGNMENT_DETERIORATION_EVENTS": n_align_det,
        "PCT_LOSERS_WITH_ADVERSE_EVENT_BEFORE_SL": pct_losers_adverse,
        "IMMEDIATE_FAILURE_COUNT": immediate_fail_count,
        "FAVORABLE_THEN_REVERSED_COUNT": favorable_reversed_count,
        "THESIS_DEGRADED_BEFORE_SL_COUNT": thesis_degraded_count,
        "AMBIGUOUS_COUNT": ambiguous_count,
        "ENTRY_DIRECTIONAL_EDGE_DIAGNOSTIC": edge_diagnostic,
        "TRADE_MANAGER_POLICY_CREATED": False,
        "EXIT_RULE_OPTIMIZED": False,
        "PARAMETER_SEARCH": False,
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "ENTRY_EVALUATOR_CHANGED": False,
        "TARGET_EVALUATOR_CHANGED": False,
        "PRODUCTION_CHANGED": False,
        "BROKER_WRITES": 0,
        "READY_FOR_VALIDATION": False,
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
