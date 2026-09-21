#!/usr/bin/env python3
"""Passive Phase 7 observer for frozen CONTEXT_STRUCTURE_RETRACE_V1.

Reads Phase 6 state and read-only MT5 market data. Writes only Phase 7 files.
It contains no broker-write primitive and never changes a V1 decision.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paper_runner import call_bridge
from trade_manager.fanout import SharedObservationPublisher, market_envelope, position_event

ROOT = Path(__file__).resolve().parent
VERSION = "CONTEXT_STRUCTURE_RETRACE_V1_PHASE7_OBSERVER"
SCHEMA = "context-structure-retrace-phase7-v1"
PHASE6_STATE = ROOT / "context_structure_retrace_forward_state.json"
PHASE6_MANIFEST = ROOT / "context_structure_retrace_forward_manifest.json"
PHASE7_STATE = ROOT / "context_structure_retrace_phase7_state.json"
PHASE7_EVENTS = ROOT / "context_structure_retrace_phase7.jsonl"
PHASE7_HEARTBEAT = ROOT / "context_structure_retrace_phase7.heartbeat.json"
PHASE7_PID = ROOT / "context_structure_retrace_phase7.pid"
PHASE7_MANIFEST = ROOT / "context_structure_retrace_phase7_manifest.json"
PHASE7_STOP = Path("/tmp/context-structure-retrace-phase7.stop")
READ_ONLY_TOOLS = frozenset({"mt5_symbol_info", "mt5_quote", "mt5_rates"})
SHARED_OBSERVATION_PUBLISHER = SharedObservationPublisher()
HORIZON_MINUTES = 180
TIME_CHECKPOINTS = (5, 10, 15, 30, 60, 90, 120, 180)
FAVORABLE = {"+0.25R": .25, "+0.50R": .50, "+0.75R": .75, "+1.00R": 1.0, "+1.50R": 1.5, "+2.00R": 2.0, "+3.00R": 3.0}
ADVERSE = {"-0.25R": .25, "-0.50R": .50, "-0.75R": .75, "-1.00R": 1.0}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso(ts: int | float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def epoch(value: str | int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())


def atomic_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def phase7_source_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def phase6_identity() -> dict[str, Any]:
    m = json.loads(PHASE6_MANIFEST.read_text(encoding="utf-8"))
    return {"source_hash": m.get("code_hash"), "config_hash": m.get("configuration_hash"),
            "decision_fingerprint": "70dba71d28fe8a5c09f9033b80eeb4c27a733c6c342537e03c631f41e2a1cdda",
            "phase2_hash": m.get("phase2_representation_hash"), "freeze_timestamp": m.get("freeze_timestamp")}


def config() -> dict[str, Any]:
    return {"version": VERSION, "horizon_minutes": HORIZON_MINUTES,
            "time_checkpoints_minutes": list(TIME_CHECKPOINTS),
            "favorable_thresholds_R": FAVORABLE, "adverse_thresholds_R": ADVERSE,
            "entry_source": "Phase 6 economic-position metadata; never recomputed",
            "price_source": "M5 bid OHLC; ask side is bid plus observed bar spread for short observations",
            "commission": "UNKNOWN", "slippage": "OBSERVE_ONLY",
            "hypotheses": {"V1_STRUCTURAL_TARGET": "BASELINE_REFERENCE",
                           "ORIGINAL_STOP_NO_V1_TARGET": "EXECUTABLE_RESEARCH_OBSERVATION",
                           "FIXED_THRESHOLDS": "EXECUTABLE_RESEARCH_OBSERVATION",
                           "BREAKEVEN_AFTER": "EXECUTABLE_RESEARCH_OBSERVATION",
                           "LOWER_TF_STRUCTURE_TRAIL": "DEFINED_BUT_NOT_EXECUTABLE",
                           "EMA_STRUCTURE_MANAGEMENT": "DEFINED_BUT_NOT_EXECUTABLE",
                           "OPPOSITE_PRICE_ACTION": "DEFINED_BUT_NOT_EXECUTABLE"},
            "two_leg": {"total_R": 1.0, "leg_a_R": 0.5, "leg_b_R": 0.5}}


def config_hash() -> str:
    return hashlib.sha256(json.dumps(config(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def empty_state(freeze_timestamp: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "observer_version": VERSION, "freeze_timestamp": freeze_timestamp,
            "created_at": now_iso(), "status": "STOPPED", "positions": {}, "processed_events": [],
            "counters": {"registered": 0, "prospective": 0, "reference": 0, "threshold_events": 0,
                          "checkpoints": 0, "gaps": 0}}


def load_state() -> dict[str, Any]:
    if PHASE7_STATE.exists():
        return json.loads(PHASE7_STATE.read_text(encoding="utf-8"))
    return empty_state(json.loads(PHASE7_MANIFEST.read_text(encoding="utf-8"))["freeze_timestamp"])


def append_event(state: dict[str, Any], event: dict[str, Any]) -> bool:
    key = event.get("event_id") or hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode()).hexdigest()
    if key in state.setdefault("processed_events", []):
        return False
    row = {"schema": SCHEMA, "observer_version": VERSION, "event_id": key, "event_time": now_iso(), **event}
    with PHASE7_EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True, default=str) + "\n")
    state["processed_events"].append(key)
    state["counters"]["threshold_events"] += int(event.get("type") == "PHASE7_THRESHOLD_REACHED")
    state["counters"]["checkpoints"] += int(event.get("type") == "PHASE7_TIME_CHECKPOINT")
    state["counters"]["gaps"] += int(event.get("type") == "PHASE7_DATA_GAP")
    return True


def v1_positions() -> list[dict[str, Any]]:
    source = json.loads(PHASE6_STATE.read_text(encoding="utf-8"))
    result = []
    for setup in source.get("setups", {}).values():
        for position in setup.get("opportunities", []):
            row = dict(position)
            for key in ("symbol", "setup_id", "direction", "market_event_id", "pattern"):
                row.setdefault(key, setup.get(key))
            result.append(row)
    return result


def _risk_r(direction: str, price: float, entry: float, risk: float) -> float:
    """Signed executable-price R; positive is favorable for either direction."""
    return ((price - entry) if direction == "LONG" else (entry - price)) / risk


def _bar_prices(bar: dict[str, Any], contract: dict[str, Any]) -> tuple[float, float, float, float]:
    point = float(contract.get("point", contract.get("tick_size", 0)) or 0)
    spread = float(bar.get("spread", 0) or 0) * point
    bid_high, bid_low = float(bar["high"]), float(bar["low"])
    return bid_high, bid_low, bid_high + spread, bid_low + spread


def two_leg_combined_R(leg_a_R: float, leg_b_R: float) -> float:
    return 0.5 * leg_a_R + 0.5 * leg_b_R


def register_position(state: dict[str, Any], position: dict[str, Any], prospective: bool) -> dict[str, Any]:
    pid = position["economic_position_id"]
    if pid in state["positions"]:
        return state["positions"][pid]
    geometry = position.get("geometry") or {}
    row = {"economic_position_id": pid, "symbol": position.get("symbol"), "direction": position.get("direction"),
           "setup_id": position.get("setup_id"), "market_event_id": position.get("market_event_id"),
           "entry_opportunity_id": position.get("entry_opportunity_id"), "entry_attempt_id": position.get("entry_attempt_id"),
           "setup_timestamp": position.get("setup_timestamp"), "fill_timestamp": position.get("fill_timestamp"),
           "fill_timestamp_iso": position.get("fill_timestamp_iso"), "executable_entry": position.get("executable_paper_entry"),
           "theoretical_entry": position.get("theoretical_entry"), "initial_stop": position.get("stop"),
           "risk_distance": float(geometry.get("stop_distance") or 0), "v1_target": position.get("target"),
           "v1_target_R": geometry.get("target_R"), "spread_at_entry": position.get("spread_at_fill"),
           "entry_mechanisms": position.get("entry_mechanisms", []), "reentry_type": position.get("reentry_type"),
           "prospective_status": "PROSPECTIVE" if prospective else "PRE_PHASE7_EXPOSED_REFERENCE",
           "left_truncated": (not prospective and position.get("status") == "OPEN"), "v1_status": position.get("status"),
           "v1_exit_timestamp": position.get("exit_timestamp"), "v1_exit_reason": position.get("exit_reason"),
           "v1_realized_R": position.get("realized_R"), "v1_window_mfe_price": 0.0, "v1_window_mae_price": 0.0,
           "shadow_mfe_R": 0.0, "shadow_mae_R": 0.0, "post_exit_shadow_mfe_R": None, "post_exit_shadow_mae_R": None,
           "thresholds": {k: {"reached": False, "first_timestamp": None} for k in {**FAVORABLE, **ADVERSE}},
           "time_checkpoints": {}, "observation_complete": False, "v1_window": "entry_to_actual_v1_exit",
           "phase7_shadow_window": "entry_to_180m_horizon"}
    state["positions"][pid] = row
    state["counters"]["registered"] += 1
    state["counters"]["prospective"] += int(prospective)
    state["counters"]["reference"] += int(not prospective)
    append_event(state, {"type": "PHASE7_POSITION_REGISTERED", "economic_position_id": pid,
                         "symbol": row["symbol"], "setup_id": row["setup_id"],
                         "prospective_status": row["prospective_status"], "left_truncated": row["left_truncated"],
                         "event_id": "register|" + pid})
    return row


def refresh_v1_metadata(row: dict[str, Any], position: dict[str, Any]) -> None:
    for key in ("status", "exit_timestamp", "exit_reason", "realized_R"):
        row["v1_" + key] = position.get(key)
    if position.get("exit_timestamp"):
        row["v1_exit_timestamp"] = position["exit_timestamp"]


def observe_position(state: dict[str, Any], row: dict[str, Any], position: dict[str, Any],
                     bars: list[dict[str, Any]], contract: dict[str, Any], freeze_epoch: int) -> None:
    entry_ts = int(row["fill_timestamp"])
    start = max(entry_ts, freeze_epoch) if row["left_truncated"] else entry_ts
    entry, risk, direction = float(row["executable_entry"]), float(row["risk_distance"] or 0), row["direction"]
    if not risk:
        return
    exit_ts = epoch(row.get("v1_exit_timestamp"))
    for bar in sorted(bars[:-1], key=lambda x: int(x["time"])):
        ts = int(bar["time"])
        if ts <= start:
            continue
        bid_high, bid_low, ask_high, ask_low = _bar_prices(bar, contract)
        favorable = (bid_high - entry) if direction == "LONG" else (entry - ask_low)
        adverse = (entry - bid_low) if direction == "LONG" else (ask_high - entry)
        favorable, adverse = max(0.0, favorable), max(0.0, adverse)
        in_v1_window = exit_ts is None or ts <= exit_ts
        after_exit = bool(exit_ts is not None and ts > exit_ts)
        if in_v1_window:
            row["v1_window_mfe_price"] = max(float(row["v1_window_mfe_price"]), favorable)
            row["v1_window_mae_price"] = max(float(row["v1_window_mae_price"]), adverse)
        row["shadow_mfe_R"] = max(float(row["shadow_mfe_R"]), favorable / risk)
        row["shadow_mae_R"] = max(float(row["shadow_mae_R"]), adverse / risk)
        if after_exit:
            row["post_exit_shadow_mfe_R"] = max(float(row.get("post_exit_shadow_mfe_R") or 0), favorable / risk)
            row["post_exit_shadow_mae_R"] = max(float(row.get("post_exit_shadow_mae_R") or 0), adverse / risk)
        for name, threshold in FAVORABLE.items():
            if not row["thresholds"][name]["reached"] and favorable / risk >= threshold:
                row["thresholds"][name] = {"reached": True, "first_timestamp": iso(ts),
                    "price_side": "BID_HIGH" if direction == "LONG" else "ASK_LOW", "post_v1_exit": after_exit}
                append_event(state, {"type": "PHASE7_THRESHOLD_REACHED", "economic_position_id": row["economic_position_id"],
                    "threshold": name, "timestamp_used": iso(ts), "post_v1_exit": after_exit,
                    "event_id": f"threshold|{row['economic_position_id']}|{name}"})
        for name, threshold in ADVERSE.items():
            if not row["thresholds"][name]["reached"] and adverse / risk >= threshold:
                row["thresholds"][name] = {"reached": True, "first_timestamp": iso(ts),
                    "price_side": "BID_LOW" if direction == "LONG" else "ASK_HIGH", "post_v1_exit": after_exit}
                append_event(state, {"type": "PHASE7_THRESHOLD_REACHED", "economic_position_id": row["economic_position_id"],
                    "threshold": name, "timestamp_used": iso(ts), "post_v1_exit": after_exit,
                    "event_id": f"threshold|{row['economic_position_id']}|{name}"})
        elapsed = (ts - entry_ts) / 60
        for minutes in TIME_CHECKPOINTS:
            key = f"R@{minutes}m"
            if key not in row["time_checkpoints"] and elapsed >= minutes:
                close = float(bar["close"])
                close_exec = close if direction == "LONG" else close + (ask_high - bid_high)
                row["time_checkpoints"][key] = {"R": _risk_r(direction, close_exec, entry, risk),
                    "timestamp_used": iso(ts), "observation_delay_from_requested_time_minutes": elapsed - minutes}
                append_event(state, {"type": "PHASE7_TIME_CHECKPOINT", "economic_position_id": row["economic_position_id"],
                    "checkpoint": key, **row["time_checkpoints"][key], "event_id": f"checkpoint|{row['economic_position_id']}|{minutes}"})
        if elapsed >= HORIZON_MINUTES:
            row["observation_complete"] = True
            append_event(state, {"type": "PHASE7_OBSERVATION_COMPLETE", "economic_position_id": row["economic_position_id"],
                                 "event_id": f"complete|{row['economic_position_id']}"})
            break
    row["v1_window_mfe_R"] = float(row["v1_window_mfe_price"]) / risk
    row["v1_window_mae_R"] = float(row["v1_window_mae_price"]) / risk


def poll(state: dict[str, Any], mcp_url: str, limit: int) -> None:
    freeze_epoch = epoch(state["freeze_timestamp"]) or 0
    poll_key = now_iso()
    for position in v1_positions():
        prospective = int(position.get("fill_timestamp") or 0) >= freeze_epoch
        row = register_position(state, position, prospective)
        refresh_v1_metadata(row, position)
        if row["prospective_status"] == "PRE_PHASE7_EXPOSED_REFERENCE" and not row["left_truncated"]:
            continue
        try:
            contract = call_bridge(mcp_url, "mt5_symbol_info", {"symbol": row["symbol"]})
            data = call_bridge(mcp_url, "mt5_rates", {"symbol": row["symbol"], "timeframe": "M5", "limit": limit})
            rates = data.get("rates", [])
            latest = rates[-1] if rates else None
            if latest:
                point = float(contract.get("point", contract.get("tick_size", 0)) or 0)
                spread = float(latest.get("spread", 0) or 0) * point
                candle = {"time": latest.get("time"), "open": latest.get("open"), "high": latest.get("high"),
                          "low": latest.get("low"), "close": latest.get("close"), "spread": latest.get("spread"),
                          "complete": False}
                envelope = market_envelope(row["symbol"], observed_at=now_iso(), source_timestamp=iso(int(latest["time"])),
                                           bid=None, ask=None, spread=spread, m1=None, m5=candle,
                                           context={}, economic_position_id=row["economic_position_id"],
                                           setup_id=row.get("setup_id"), strategy_id=position.get("strategy_id", "CONTEXT_STRUCTURE_RETRACE_V1"),
                                           m5_history=rates[-205:])
            lifecycle = position_event("POSITION_UPDATED", row, observed_at=now_iso())
            if not SHARED_OBSERVATION_PUBLISHER.publish(lifecycle):
                append_event(state, {"type": "PHASE7_SHARED_OBSERVATION_PUBLISH_FAILED",
                    "economic_position_id": row["economic_position_id"], "symbol": row["symbol"],
                    "reason": "publisher returned false", "event_id": f"shared-publish-failed|{row['economic_position_id']}|{poll_key}"})
            if latest:
                if not SHARED_OBSERVATION_PUBLISHER.publish(envelope):
                    append_event(state, {"type": "PHASE7_SHARED_OBSERVATION_PUBLISH_FAILED",
                        "economic_position_id": row["economic_position_id"], "symbol": row["symbol"],
                        "reason": "publisher returned false", "event_id": f"shared-publish-failed-market|{row['economic_position_id']}|{poll_key}"})
            observe_position(state, row, position, data.get("rates", []), contract, freeze_epoch)
        except Exception as exc:
            append_event(state, {"type": "PHASE7_DATA_GAP", "economic_position_id": row["economic_position_id"],
                "symbol": row["symbol"], "gap_start": poll_key, "gap_end": now_iso(), "reason": str(exc),
                "event_id": f"gap|{row['economic_position_id']}|{poll_key}"})
    state["last_poll_at"] = now_iso()
    atomic_json(PHASE7_STATE, state)
    atomic_json(PHASE7_HEARTBEAT, {"observer_pid": os.getpid(), "timestamp": now_iso(), "status": "ACTIVE",
                                   "prospective_positions": state["counters"]["prospective"]})


def freeze() -> dict[str, Any]:
    if PHASE7_MANIFEST.exists():
        return json.loads(PHASE7_MANIFEST.read_text(encoding="utf-8"))
    manifest = {"observer_version": VERSION, "freeze_timestamp": now_iso(), "source_hash": phase7_source_hash(),
        "configuration_hash": config_hash(), "schema": SCHEMA, "phase6_identity": phase6_identity(),
        "execution_isolation": {"mode": "PASSIVE_READ_ONLY", "allowed_bridge_tools": sorted(READ_ONLY_TOOLS), "broker_write": False},
        "observation_horizon_minutes": HORIZON_MINUTES}
    atomic_json(PHASE7_MANIFEST, manifest)
    atomic_json(PHASE7_STATE, empty_state(manifest["freeze_timestamp"]))
    return manifest


def acquire_lock() -> None:
    if PHASE7_PID.exists():
        try:
            os.kill(int(json.loads(PHASE7_PID.read_text())["pid"]), 0)
        except ProcessLookupError:
            PHASE7_PID.unlink(missing_ok=True)
        else:
            raise RuntimeError("Phase 7 observer already active")
    atomic_json(PHASE7_PID, {"pid": os.getpid(), "started": now_iso(), "observer_version": VERSION})


def release_lock() -> None:
    PHASE7_PID.unlink(missing_ok=True)


def order_isolation_audit() -> dict[str, Any]:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    bridge_tools = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "call_bridge":
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                bridge_tools.append(node.args[1].value)
    disallowed_calls = sorted(set(bridge_tools) - READ_ONLY_TOOLS)
    return {"pass": bool(bridge_tools) and not disallowed_calls,
            "disallowed_bridge_tools": disallowed_calls, "bridge_tools": sorted(set(bridge_tools)),
            "allowed_bridge_tools": sorted(READ_ONLY_TOOLS), "call_bridge_call_count": len(bridge_tools),
            "broker_order_primitives_imported": False}


def report_data() -> dict[str, Any]:
    state = load_state()
    rows = list(state.get("positions", {}).values())
    prospective = [r for r in rows if r.get("prospective_status") == "PROSPECTIVE"]
    closed = [r for r in prospective if r.get("v1_status") in ("STOPPED", "TARGET_HIT")]
    return {"manifest": json.loads(PHASE7_MANIFEST.read_text()), "status": state.get("status"), "N": len(prospective),
            "reference_N": sum(r.get("prospective_status") == "PRE_PHASE7_EXPOSED_REFERENCE" for r in rows),
            "thresholds_actually_reached": {name: sum(bool(r.get("thresholds", {}).get(name, {}).get("reached")) for r in prospective)
                                             for name in {**FAVORABLE, **ADVERSE}},
            "positions": prospective, "closed_N": len(closed), "open_N": sum(r.get("v1_status") == "OPEN" for r in prospective),
            "scope": {"v1_window": "entry_to_actual_v1_exit", "shadow_window": "entry_to_180m_horizon",
                      "post_exit": "separate from V1 MFE/MAE"}, "hypotheses": config()["hypotheses"]}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[len(s) // 2]


def build_shadow_report() -> dict[str, Any]:
    """Standard shadow-entity observability report. This entity has its own
    independent freeze/hash/pid/heartbeat lifecycle, separate from the V1
    runner it observes — it is never folded into CONTEXT_STRUCTURE_RETRACE_V1's
    own standard report. No writes; maps report_data()'s existing output plus
    this observer's own heartbeat/pid files (report_data() doesn't thread
    those through) into the shared shadow-report shape.
    """
    d = report_data()
    now = datetime.now(timezone.utc)
    hb = json.loads(PHASE7_HEARTBEAT.read_text()) if PHASE7_HEARTBEAT.exists() else {}
    pid = json.loads(PHASE7_PID.read_text()).get("pid") if PHASE7_PID.exists() else None
    heartbeat_ts = hb.get("timestamp")

    def _age_seconds(iso_ts: Any) -> float | None:
        if not iso_ts:
            return None
        try:
            ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
            return (now - ts).total_seconds()
        except (TypeError, ValueError):
            return None

    post_exit_mfe = [p["post_exit_shadow_mfe_R"] for p in d["positions"] if p.get("post_exit_shadow_mfe_R") is not None]
    post_exit_mae = [p["post_exit_shadow_mae_R"] for p in d["positions"] if p.get("post_exit_shadow_mae_R") is not None]

    return {
        "shadow_entity_id": VERSION,
        "references_strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
        "status": d["status"],
        "runner_pid": pid,
        "last_runner_heartbeat": heartbeat_ts,
        "runner_heartbeat_age": _age_seconds(heartbeat_ts),
        "observed_at": now.isoformat(),
        "sample": {"prospective_N": d["N"], "reference_N": d["reference_N"]},
        "thresholds_reached": d["thresholds_actually_reached"],
        "mfe_mae_summary": {
            "median_post_exit_mfe_R": _median(post_exit_mfe),
            "median_post_exit_mae_R": _median(post_exit_mae),
            "max_post_exit_mfe_R": max(post_exit_mfe) if post_exit_mfe else None,
            "max_post_exit_mae_R": max(post_exit_mae) if post_exit_mae else None,
            "scope": "post_exit_shadow_continuation",
        },
        "v1_window": {"closed_N": d["closed_N"], "open_N": d["open_N"]},
        "scope": d["scope"],
        "hypotheses": d["hypotheses"],
        "manifest": d["manifest"],
    }


def print_report() -> None:
    d = report_data()
    print(f"{VERSION}\n{'=' * 72}")
    print(f"Freeze: {d['manifest']['freeze_timestamp']} | Status: {d['status']} | Prospective N: {d['N']} | Reference N: {d['reference_N']}")
    print("\nTHRESHOLDS ACTUALLY REACHED — POST-ENTRY OBSERVATIONS ONLY")
    print(" | ".join(f"{k}: {v}" for k, v in d["thresholds_actually_reached"].items()))
    print(f"\nV1 WINDOW: closed={d['closed_N']} open={d['open_N']}; MFE/MAE are separate from shadow continuation")
    print("\nSHADOW HYPOTHESES — OBSERVATION ONLY")
    for name, status in d["hypotheses"].items():
        print(f"{name}: {status}")
    if d["N"] < 20:
        print("\nPROSPECTIVE SAMPLE TOO SMALL FOR STRATEGY SELECTION (N < 20)")


def run(args: argparse.Namespace) -> None:
    if not PHASE7_MANIFEST.exists():
        freeze()
    acquire_lock()
    state = load_state()
    state["status"] = "ACTIVE"
    atomic_json(PHASE7_STATE, state)
    stop = {"x": False}
    def handler(signum: int, frame: Any) -> None:
        stop["x"] = True
    signal.signal(signal.SIGINT, handler); signal.signal(signal.SIGTERM, handler)
    PHASE7_STOP.unlink(missing_ok=True)
    try:
        while not stop["x"] and not PHASE7_STOP.exists():
            poll(state, args.mcp_url, args.limit)
            time.sleep(max(1, args.interval))
    finally:
        state["status"] = "STOPPED"
        atomic_json(PHASE7_STATE, state)
        atomic_json(PHASE7_HEARTBEAT, {"observer_pid": os.getpid(), "timestamp": now_iso(), "status": "STOPPED"})
        release_lock()


def main() -> None:
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("freeze"); sub.add_parser("report"); sub.add_parser("status"); sub.add_parser("health"); sub.add_parser("audit-order-isolation")
    start = sub.add_parser("start"); start.add_argument("--interval", type=int, default=15); start.add_argument("--limit", type=int, default=320); start.add_argument("--mcp-url", default="http://127.0.0.1:22347/mcp")
    sub.add_parser("stop")
    args = parser.parse_args()
    if args.command == "freeze": print(json.dumps(freeze(), indent=2)); return
    if args.command == "report": print_report(); return
    if args.command == "audit-order-isolation": print(json.dumps(order_isolation_audit(), indent=2)); return
    if args.command in ("status", "health"):
        state = load_state(); print(json.dumps({"observer": VERSION, "status": state.get("status"),
            "pid": json.loads(PHASE7_PID.read_text())["pid"] if PHASE7_PID.exists() else None,
            "heartbeat": json.loads(PHASE7_HEARTBEAT.read_text()) if PHASE7_HEARTBEAT.exists() else None,
            "prospective_positions": state["counters"]["prospective"]}, indent=2, default=str)); return
    if args.command == "stop": PHASE7_STOP.write_text(now_iso()); print("Phase 7 stop requested"); return
    run(args)


if __name__ == "__main__":
    main()
