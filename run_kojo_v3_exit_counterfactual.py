"""Counterfactual exit study for KOJO_STRUCTURE_RECLAIM_V3.

Compares three observational exit policies against the static SL/TP baseline
using the frozen 38-trade discovery dataset.

Policies:
    EXIT_ON_KEY_LEVEL_RECLAIM         — exit at close of first M15 bar whose
                                        close recrosses the structural level
    EXIT_ON_H1_ALIGNMENT_DETERIORATION — exit at close of first H1 bar whose
                                         close recrosses the structural level
    EXIT_ON_FIRST_OF_THE_TWO          — exit at whichever fires first

Safety constraints (unchanged from v3 commit c82d290):
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

V3_COMMIT        = "c82d290"
POST_ENTRY_COMMIT = "1e321fe"

POST_ENTRY_PATH = Path(
    "artifacts/research/kojo_v3_post_entry_behavior.json"
)

POLICY_NAMES = [
    "STATIC_BASELINE",
    "EXIT_ON_KEY_LEVEL_RECLAIM",
    "EXIT_ON_H1_ALIGNMENT_DETERIORATION",
    "EXIT_ON_FIRST_OF_THE_TWO",
]


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pct(n: int, d: int) -> float:
    return round(n / d * 100, 2) if d else 0.0


def _med(vals: list[float]) -> float | None:
    return round(median(vals), 4) if vals else None


def _p25(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return round(vals[0], 4) if vals else None
    return round(quantiles(vals, n=4)[0], 4)


def _p75(vals: list[float]) -> float | None:
    if len(vals) < 2:
        return round(vals[0], 4) if vals else None
    return round(quantiles(vals, n=4)[2], 4)


def _expectancy(rs: list[float]) -> float | None:
    return round(sum(rs) / len(rs), 4) if rs else None


def _profit_factor(rs: list[float]) -> float | None:
    gains = sum(r for r in rs if r > 0)
    losses = sum(abs(r) for r in rs if r < 0)
    if losses < 1e-9:
        return None
    return round(gains / losses, 4)


def _max_drawdown_r(rs: list[float]) -> float:
    """Compute max drawdown in R units from sequential trade sequence."""
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for r in rs:
        equity += r
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def _capped_exit_r(signal_r: float | None, static_r: float) -> float:
    """Return exit R for a policy that fires at signal_r.

    Caps signal_r at -1.0 (the static SL floor) because if the bar that
    triggers the signal also breaches the SL intrabar, the conservative
    STOP_FIRST policy has already exited at -1.0R before bar close.
    """
    if signal_r is None:
        return static_r
    return max(signal_r, -1.0)


# ──────────────────────────────────────────────────────────────────────────────
# per-policy realized_r for a single trade
# ──────────────────────────────────────────────────────────────────────────────

def _apply_policy(trade: dict[str, Any], policy: str) -> dict[str, Any]:
    static_r      = float(trade["static_realized_r"])
    reclaim_r     = trade.get("trade_r_at_reclaim")
    det_r         = trade.get("trade_r_at_deterioration")
    reclaim_ts    = trade.get("time_to_first_reclaim")     # minutes from entry
    det_ts        = trade.get("alignment_deterioration_ts_minutes")

    if policy == "STATIC_BASELINE":
        return {
            "policy": policy,
            "realized_r": static_r,
            "exit_source": "STATIC",
            "signal_fired": False,
            "signal_ts_minutes": None,
        }

    if policy == "EXIT_ON_KEY_LEVEL_RECLAIM":
        if reclaim_r is not None:
            realized = _capped_exit_r(reclaim_r, static_r)
            return {
                "policy": policy,
                "realized_r": realized,
                "exit_source": "KEY_LEVEL_RECLAIM",
                "signal_fired": True,
                "signal_ts_minutes": reclaim_ts,
                "uncapped_signal_r": round(reclaim_r, 4),
                "capped_to_sl": reclaim_r < -1.0,
            }
        return {
            "policy": policy,
            "realized_r": static_r,
            "exit_source": "STATIC",
            "signal_fired": False,
            "signal_ts_minutes": None,
        }

    if policy == "EXIT_ON_H1_ALIGNMENT_DETERIORATION":
        if det_r is not None:
            realized = _capped_exit_r(det_r, static_r)
            return {
                "policy": policy,
                "realized_r": realized,
                "exit_source": "H1_ALIGNMENT_DETERIORATION",
                "signal_fired": True,
                "signal_ts_minutes": det_ts,
                "uncapped_signal_r": round(det_r, 4),
                "capped_to_sl": det_r < -1.0,
            }
        return {
            "policy": policy,
            "realized_r": static_r,
            "exit_source": "STATIC",
            "signal_fired": False,
            "signal_ts_minutes": None,
        }

    if policy == "EXIT_ON_FIRST_OF_THE_TWO":
        candidates = []
        if reclaim_ts is not None:
            candidates.append(("KEY_LEVEL_RECLAIM", reclaim_ts, reclaim_r))
        if det_ts is not None:
            candidates.append(("H1_ALIGNMENT_DETERIORATION", det_ts, det_r))
        if not candidates:
            return {
                "policy": policy,
                "realized_r": static_r,
                "exit_source": "STATIC",
                "signal_fired": False,
                "signal_ts_minutes": None,
            }
        # earliest signal wins
        first_source, first_ts, first_r = min(candidates, key=lambda c: c[1])
        realized = _capped_exit_r(first_r, static_r)
        return {
            "policy": policy,
            "realized_r": realized,
            "exit_source": first_source,
            "signal_fired": True,
            "signal_ts_minutes": first_ts,
            "uncapped_signal_r": round(first_r, 4),
            "capped_to_sl": first_r < -1.0,
        }

    raise ValueError(f"unknown policy: {policy}")


# ──────────────────────────────────────────────────────────────────────────────
# aggregate statistics for a policy
# ──────────────────────────────────────────────────────────────────────────────

def _aggregate(
    trade_obs: list[dict],
    policy_results: list[dict],
    policy: str,
) -> dict:
    rs = [p["realized_r"] for p in policy_results]
    n  = len(rs)

    fired_trades    = [p for p in policy_results if p["signal_fired"]]
    not_fired       = [p for p in policy_results if not p["signal_fired"]]

    wins  = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]

    # trades where policy fires: did it improve or worsen vs static?
    improvements = []
    degradations = []
    neutral      = []
    for t, p in zip(trade_obs, policy_results):
        if not p["signal_fired"]:
            continue
        static_r = float(t["static_realized_r"])
        delta = p["realized_r"] - static_r
        if delta > 0.01:
            improvements.append({"signal_id": t["signal_id"], "static_r": static_r,
                                  "policy_r": p["realized_r"], "delta_r": round(delta, 4),
                                  "exit_source": p["exit_source"]})
        elif delta < -0.01:
            degradations.append({"signal_id": t["signal_id"], "static_r": static_r,
                                   "policy_r": p["realized_r"], "delta_r": round(delta, 4),
                                   "exit_source": p["exit_source"]})
        else:
            neutral.append({"signal_id": t["signal_id"], "static_r": static_r,
                             "policy_r": p["realized_r"], "delta_r": round(delta, 4)})

    # winner conversion: static winner → policy loser
    static_wins_lost = [
        d for d in degradations
        if d["static_r"] > 0 and d["policy_r"] <= 0
    ]

    # cap-to-sl count
    capped = sum(1 for p in fired_trades if p.get("capped_to_sl", False))

    return {
        "policy": policy,
        "total_trades": n,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate_pct": _pct(len(wins), n),
        "expectancy_r": _expectancy(rs),
        "total_r": round(sum(rs), 4),
        "profit_factor": _profit_factor(rs),
        "max_drawdown_r": _max_drawdown_r(rs),
        "realized_r_median": _med(rs),
        "realized_r_p25": _p25(rs),
        "realized_r_p75": _p75(rs),
        "signal_fired_count": len(fired_trades),
        "signal_fired_pct": _pct(len(fired_trades), n),
        "capped_to_sl_count": capped,
        "vs_static": {
            "improved_count":   len(improvements),
            "degraded_count":   len(degradations),
            "neutral_count":    len(neutral),
            "static_winners_converted_to_loss": len(static_wins_lost),
            "net_r_delta_vs_static": round(
                sum(d["delta_r"] for d in improvements)
                + sum(d["delta_r"] for d in degradations), 4
            ),
            "improvements": improvements,
            "degradations": degradations,
            "neutral": neutral,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="V3 exit counterfactual study")
    parser.add_argument("--post-entry", type=Path, default=POST_ENTRY_PATH)
    parser.add_argument("--artifact-root", type=Path, default=Path("artifacts/research"))
    args = parser.parse_args()

    raw = args.post_entry.read_bytes()
    source_fp = hashlib.sha256(raw).hexdigest()
    post_entry = json.loads(raw)

    print(f"post-entry artifact fingerprint: {source_fp[:16]}...", file=sys.stderr)

    trade_obs = post_entry["trades"]
    n = len(trade_obs)
    print(f"trades: {n}", file=sys.stderr)

    # ── compute per-policy results ─────────────────────────────────────────────
    all_policy_results: dict[str, list[dict]] = {}
    for policy in POLICY_NAMES:
        results = [_apply_policy(t, policy) for t in trade_obs]
        all_policy_results[policy] = results

    # ── compute aggregates ─────────────────────────────────────────────────────
    aggregates = {
        p: _aggregate(trade_obs, all_policy_results[p], p)
        for p in POLICY_NAMES
    }

    # ── cross-policy comparison table ──────────────────────────────────────────
    comparison = []
    baseline_exp = aggregates["STATIC_BASELINE"]["expectancy_r"]
    for p in POLICY_NAMES:
        agg = aggregates[p]
        comparison.append({
            "policy": p,
            "win_rate_pct": agg["win_rate_pct"],
            "expectancy_r": agg["expectancy_r"],
            "total_r": agg["total_r"],
            "profit_factor": agg["profit_factor"],
            "max_drawdown_r": agg["max_drawdown_r"],
            "signal_fired_count": agg["signal_fired_count"],
            "static_winners_converted": agg["vs_static"]["static_winners_converted_to_loss"],
            "net_r_delta_vs_baseline": round(
                (agg["expectancy_r"] or 0) - (baseline_exp or 0), 4
            ),
        })

    # ── per-trade breakdown ────────────────────────────────────────────────────
    per_trade_breakdown = []
    for i, t in enumerate(trade_obs):
        row = {
            "signal_id": t["signal_id"],
            "direction": t["direction"],
            "static_status": t["static_status"],
            "static_realized_r": t["static_realized_r"],
        }
        for p in POLICY_NAMES[1:]:  # skip baseline (= static)
            pr = all_policy_results[p][i]
            row[p] = {
                "realized_r": pr["realized_r"],
                "exit_source": pr["exit_source"],
                "signal_fired": pr["signal_fired"],
                "delta_r": round(pr["realized_r"] - t["static_realized_r"], 4),
            }
        per_trade_breakdown.append(row)

    # ── build artifact ─────────────────────────────────────────────────────────
    artifact = {
        "study_type": "EXIT_COUNTERFACTUAL_OBSERVATIONAL",
        "v3_entry_commit": V3_COMMIT,
        "post_entry_study_commit": POST_ENTRY_COMMIT,
        "post_entry_artifact_fingerprint": source_fp,
        "discovery_boundary": {
            "start": post_entry["discovery_start"],
            "end": post_entry["discovery_end"],
        },
        "policies_studied": POLICY_NAMES,
        "methodology": {
            "exit_price_basis": "CLOSE of triggering completed bar (M15 or H1)",
            "sl_floor_applied": "signal exit capped at -1.0R; bars breaching SL intrabar result in static -1.0R",
            "same_bar_policy": "CONSERVATIVE_STOP_FIRST (existing V3 policy)",
            "level_reclaim_definition": post_entry["level_reclaim_basis"],
            "h1_deterioration_definition": post_entry["alignment_deterioration_proxy"],
            "trade_sequence_for_drawdown": "chronological by decision_timestamp",
            "no_threshold_tuned": True,
            "no_exit_optimized": True,
            "m15_opposite_rejection_excluded": True,
        },
        "comparison_table": comparison,
        "policy_aggregates": aggregates,
        "per_trade_breakdown": per_trade_breakdown,
        "no_management_policy_created": True,
        "no_exit_rule_optimized": True,
        "no_parameter_search": True,
        "validation_outcomes_accessed": False,
        "entry_evaluator_changed": False,
        "target_evaluator_changed": False,
        "production_changed": False,
        "broker_writes": 0,
    }

    artifact_bytes = json.dumps(artifact, sort_keys=True).encode()
    artifact_fp = hashlib.sha256(artifact_bytes).hexdigest()
    artifact["artifact_fingerprint"] = artifact_fp

    args.artifact_root.mkdir(parents=True, exist_ok=True)
    out_path = args.artifact_root / "kojo_v3_exit_counterfactual.json"
    out_path.write_text(json.dumps(artifact, indent=2, default=str))
    print(f"artifact: {out_path}", file=sys.stderr)

    # ── print summary report ───────────────────────────────────────────────────
    print(json.dumps({
        "V3_ENTRY_COMMIT": V3_COMMIT,
        "POST_ENTRY_STUDY_COMMIT": POST_ENTRY_COMMIT,
        "COUNTERFACTUAL_ARTIFACT": str(out_path),
        "COUNTERFACTUAL_ARTIFACT_FINGERPRINT": artifact_fp,
        "TOTAL_TRADES": n,
        "comparison_table": comparison,
        "key_level_reclaim_detail": aggregates["EXIT_ON_KEY_LEVEL_RECLAIM"]["vs_static"],
        "h1_deterioration_detail": aggregates["EXIT_ON_H1_ALIGNMENT_DETERIORATION"]["vs_static"],
        "first_of_two_detail": aggregates["EXIT_ON_FIRST_OF_THE_TWO"]["vs_static"],
        "TRADE_MANAGER_POLICY_CREATED": False,
        "EXIT_RULE_OPTIMIZED": False,
        "THRESHOLD_TUNED": False,
        "PARAMETER_SEARCH": False,
        "VALIDATION_OUTCOMES_ACCESSED": False,
        "ENTRY_EVALUATOR_CHANGED": False,
        "PRODUCTION_CHANGED": False,
        "BROKER_WRITES": 0,
        "READY_FOR_VALIDATION": False,
    }, indent=2))


if __name__ == "__main__":
    main()
