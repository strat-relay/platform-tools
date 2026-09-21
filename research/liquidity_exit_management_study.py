#!/usr/bin/env python3
"""Read-only matched post-entry management study over frozen Liquidity state.

This deliberately does not fetch market data or touch execution services.  It
reconciles the frozen control records and emits explicit capability gaps when a
post-entry candle path is not present in the source artifacts.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "research"
PROTOCOL_PATH = OUT / "liquidity_exit_management_protocol.json"
STATE_SOURCES = {
    "LIQUIDITY_DISPLACEMENT_SCALP_V1": ROOT / "liquidity_displacement_forward_state.json",
    "LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1": ROOT / "liquidity_displacement_xau33_state.json",
    "LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1": ROOT / "liquidity_displacement_btc25_state.json",
    "LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1": ROOT / "liquidity_displacement_usdjpy25_state.json",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pct(values, q):
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mean(values):
    return sum(values) / len(values) if values else None


def load_rows():
    rows = []
    for strategy, path in STATE_SOURCES.items():
        state = json.loads(path.read_text())
        for row in state.get("signals", {}).values():
            if row.get("entry_realistic") is None:
                continue
            item = dict(row)
            item["strategy_id"] = strategy
            item["source_path"] = str(path.relative_to(ROOT))
            item["source_sha256"] = sha256(path)
            rows.append(item)
    return rows


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    source_hashes = {k: sha256(v) for k, v in STATE_SOURCES.items()}
    protocol = {
        "schema": "liquidity-exit-management-protocol-v1",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "study_scope": "matched historical filled Liquidity entries; no production writes",
        "population_rule": "all state.signal rows with entry_realistic present, excluding UNFILLED_EXPIRED",
        "control_current": {"target_r": 1.25, "max_hold_minutes": 120, "breakeven": False, "trailing": False, "partial": False},
        "excursion_horizons_minutes": [15, 30, 60, 120, 180, 240, 360],
        "profiles": {
            "PROFILE_0_CURRENT": {"target_r": 1.25, "max_hold_minutes": 120},
            "PROFILE_1_FIXED_TARGET_HOLD": {"target_r": [1.5, 2.0, 2.5, 3.0], "max_hold_minutes": [120, 240, 360], "management": "none"},
            "PROFILE_2_BREAKEVEN": {"breakeven_at_r": [0.75, 1.0, 1.25], "target_r": [2.0, 2.5, 3.0], "max_hold_minutes": [240, 360]},
            "PROFILE_3_TRAILING": {"activation_r": [0.75, 1.0], "trail_distance_r": [0.5, 1.0], "semantics": "R-based stop ratchet; research only"},
            "PROFILE_4_PARTIAL_RUNNER": {"partial_fraction": [0.5], "partial_at_r": [1.0], "runner_target_r": [2.0, 2.5, 3.0], "max_hold_minutes": [240, 360], "broker_feasibility_separate": True},
            "PROFILE_5_NO_TP_TIME_EXIT": {"target_r": None, "max_hold_minutes": [240, 360]},
        },
        "cost_semantics": {"entry_cost": "use stored entry_realistic exactly once", "exit_cost": "use stored frozen result semantics; no fictional second entry cost", "research_only": True},
        "production_safety": {"broker_writes": 0, "strategy_logic_changed": False, "signal_generation_changed": False, "execution_changed": False, "risk_sizing_changed": False},
        "source_hashes": source_hashes,
        "data_capability": {"post_entry_candles_present": False, "decision": "control reconciliation only; alternatives unavailable until immutable post-entry candle paths are supplied"},
    }
    protocol["protocol_sha256"] = hashlib.sha256(json.dumps(protocol, sort_keys=True).encode()).hexdigest()
    write_json(PROTOCOL_PATH, protocol)

    rows = load_rows()
    ledger_path = OUT / "liquidity_post_entry_excursion_ledger.jsonl"
    with ledger_path.open("w") as f:
        for row in rows:
            record = {
                "strategy_id": row["strategy_id"], "setup_id": row["setup_id"],
                "signal_timestamp": row.get("simulated_fill_timestamp"), "direction": row.get("direction"),
                "entry_realistic": row.get("entry_realistic"), "stop_loss": row.get("stop_loss"),
                "risk_distance": row.get("stop_distance"), "control_status": row.get("status"),
                "control_exit_reason": row.get("exit_reason"), "control_exit_timestamp": row.get("simulated_close_timestamp"),
                "control_r_realistic": row.get("r_realistic"), "control_duration_minutes": row.get("duration_minutes"),
                "aggregate_mfe_r": row.get("mfe_r"), "aggregate_mae_r": row.get("mae_r"),
                "horizons": {str(h): {"mfe_r": None, "mae_r": None, "first_hit_r": {}, "return_time_minutes": None, "availability": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"} for h in protocol["excursion_horizons_minutes"]},
                "post_1_25r_continuation": {"available": False, "reason": "No immutable post-entry candle path in source state"},
                "source_path": row["source_path"], "source_sha256": row["source_sha256"],
            }
            f.write(json.dumps(record, sort_keys=True) + "\n")

    by_strategy = defaultdict(list)
    for r in rows: by_strategy[r["strategy_id"]].append(r)
    def summary(items):
        rs = [x["r_realistic"] for x in items if isinstance(x.get("r_realistic"), (int, float))]
        wins = [x for x in items if x.get("status") == "TARGET_HIT"]
        losses = [x for x in items if x.get("status") == "STOPPED"]
        gross_win = sum(max(0, x.get("r_realistic") or 0) for x in items)
        gross_loss = abs(sum(min(0, x.get("r_realistic") or 0) for x in items))
        return {"fills": len(items), "win_rate": len(wins) / len(items) if items else None, "expectancy_r": mean(rs), "profit_factor": gross_win / gross_loss if gross_loss else None, "median_hold_minutes": pct([x["duration_minutes"] for x in items if x.get("duration_minutes") is not None], .5), "p90_hold_minutes": pct([x["duration_minutes"] for x in items if x.get("duration_minutes") is not None], .9), "median_mfe_r": pct([x["mfe_r"] for x in items if x.get("mfe_r") is not None], .5), "p90_mfe_r": pct([x["mfe_r"] for x in items if x.get("mfe_r") is not None], .9), "median_mae_r": pct([x["mae_r"] for x in items if x.get("mae_r") is not None], .5), "exit_buckets": dict(Counter(str(x.get("exit_reason")) for x in items))}

    closed = [x for x in rows if x.get("simulated_close_timestamp") and x.get("r_realistic") is not None]
    open_rows = [x for x in rows if x not in closed]
    results = {"schema": "liquidity-exit-management-results-v1", "protocol_sha256": protocol["protocol_sha256"], "control_reproduction": {"status": "PASS_CLOSED_POPULATION_RECONCILED_FROM_FROZEN_STATE", "fills": len(rows), "closed_fills": len(closed), "open_or_censored_fills": len(open_rows), "all_closed_rows_have_control_status_r_and_timestamp": all(x.get("status") and x.get("r_realistic") is not None and x.get("simulated_close_timestamp") for x in closed)}, "profiles": {"PROFILE_0_CURRENT": {"status": "AVAILABLE", "by_strategy": {k: summary(v) for k,v in by_strategy.items()}}, "PROFILE_1_FIXED_TARGET_HOLD": {"status": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"}, "PROFILE_2_BREAKEVEN": {"status": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"}, "PROFILE_3_TRAILING": {"status": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"}, "PROFILE_4_PARTIAL_RUNNER": {"status": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"}, "PROFILE_5_NO_TP_TIME_EXIT": {"status": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"}}}
    write_json(OUT / "liquidity_exit_management_results.json", results)
    write_json(OUT / "liquidity_exit_management_by_strategy.json", {"schema": "liquidity-exit-management-by-strategy-v1", "protocol_sha256": protocol["protocol_sha256"], "control": {k: summary(v) for k,v in by_strategy.items()}, "alternatives": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"})
    write_json(OUT / "liquidity_exit_management_pairwise.json", {"schema": "liquidity-exit-management-pairwise-v1", "protocol_sha256": protocol["protocol_sha256"], "status": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH", "control": "PROFILE_0_CURRENT", "comparison_population": "identical filled rows; no alternative exits scored"})
    write_json(OUT / "liquidity_exit_management_bootstrap.json", {"schema": "liquidity-exit-management-bootstrap-v1", "protocol_sha256": protocol["protocol_sha256"], "status": "NOT_RUN", "reason": "No alternative profile outcomes; bootstrap deltas would be fictional"})
    write_json(OUT / "liquidity_exit_management_capability_gap.json", {"schema": "liquidity-exit-management-capability-gap-v1", "protocol_sha256": protocol["protocol_sha256"], "minimum_additions": ["immutable M5 OHLC/bid-ask path from fill through 360 minutes", "path provenance/hash and data-age metadata", "record threshold first-hit and return timestamps", "persist lifecycle/outcome snapshot without frontend reconstruction", "separate research replay from broker execution"], "current_gaps": ["source state has only aggregate MFE/MAE and control terminal outcome", "no terminal outcome market snapshot", "no path after current 120-minute control", "broker management actions cannot be inferred from strategy/economic entries"], "engine_rewrite": False, "production_changes": False, "broker_writes": 0})
    print(json.dumps({"protocol_sha256": protocol["protocol_sha256"], "fills": len(rows), "strategies": {k: len(v) for k,v in by_strategy.items()}, "alternatives": "UNAVAILABLE_NO_POST_ENTRY_CANDLE_PATH"}, indent=2))


if __name__ == "__main__":
    main()
