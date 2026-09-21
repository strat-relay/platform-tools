#!/usr/bin/env python3
"""Read-only observer for all four enabled Liquidity live instances."""
from __future__ import annotations

import argparse
import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "artifacts/live/liquidity_live_all_observation.json"
SIGNALS = ROOT / "runtime/orchestration/signals.jsonl"
INTENTS = ROOT / "runtime/execution/execution_intents.jsonl"
DECISIONS = ROOT / "runtime/execution/execution_decisions.jsonl"
SKIPS = ROOT / "runtime/execution/execution_skips.jsonl"
ORCH_PID = ROOT / "runtime/orchestration/pid"
ORCH_HEARTBEAT = ROOT / "runtime/orchestration/heartbeat.json"
CONSUMER_PID = ROOT / "runtime/execution/pid"
CONSUMER_HEARTBEAT = ROOT / "runtime/execution/heartbeat.json"
CONTEXT_HEARTBEAT = ROOT / "context_structure_retrace_forward.heartbeat.json"

INSTANCES = {
    "BASE": {
        "strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
        "instance_id": "liquidity-xau-base",
        "symbol": "XAUUSDm",
        "state": "liquidity_displacement_forward_state.json",
        "baseline": "runtime/orchestration/liquidity-xau-base-startup-baseline.json",
    },
    "XAU33": {
        "strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1",
        "instance_id": "liquidity-xau33",
        "symbol": "XAUUSDm",
        "state": "liquidity_displacement_xau33_state.json",
        "baseline": "runtime/orchestration/liquidity-xau33-startup-baseline.json",
    },
    "BTC25": {
        "strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1",
        "instance_id": "liquidity-btc25",
        "symbol": "BTCUSDm",
        "state": "liquidity_displacement_btc25_state.json",
        "baseline": "runtime/orchestration/liquidity-btc25-startup-baseline.json",
    },
    "USDJPY25": {
        "strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1",
        "instance_id": "liquidity-usdjpy25",
        "symbol": "USDJPYm",
        "state": "liquidity_displacement_usdjpy25_state.json",
        "baseline": "runtime/orchestration/liquidity-usdjpy25-startup-baseline.json",
    },
}


def iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def read(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def lines(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        out.append(row)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def pid(path: Path) -> int | None:
    value = read(path)
    if isinstance(value, dict):
        value = value.get("pid")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def alive(value: int | None) -> bool:
    if not value:
        return False
    try:
        os.kill(value, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def health() -> dict[str, Any]:
    orch = pid(ORCH_PID)
    consumer = pid(CONSUMER_PID)
    bridge = False
    try:
        with socket.create_connection(("127.0.0.1", 22348), timeout=.5):
            bridge = True
    except OSError:
        pass
    return {
        "orchestrator_pid": orch,
        "orchestrator_alive": alive(orch),
        "orchestrator_heartbeat": read(ORCH_HEARTBEAT),
        "consumer_pid": consumer,
        "consumer_alive": alive(consumer),
        "consumer_heartbeat": read(CONSUMER_HEARTBEAT),
        "context_heartbeat": read(CONTEXT_HEARTBEAT),
        "execution_bridge_22348_reachable": bridge,
        "research_22350_touched": False,
    }


def rows_for(name: str) -> list[dict[str, Any]]:
    info = INSTANCES[name]
    state = read(ROOT / info["state"], {}) or {}
    return [row for row in (state.get("signals") or {}).values() if isinstance(row, dict)]


def baseline(name: str) -> dict[str, Any]:
    return read(ROOT / INSTANCES[name]["baseline"], {}) or {}


def event_time(row: dict[str, Any]) -> datetime | None:
    return dt(row.get("simulated_fill_timestamp") or row.get("fill_timestamp") or row.get("detected_at"))


def find_new(name: str) -> dict[str, Any] | None:
    base = baseline(name)
    cutoff = dt(base.get("baseline_event_timestamp"))
    setup_id = base.get("baseline_setup_id")
    candidates = []
    for row in rows_for(name):
        timestamp = event_time(row)
        if timestamp and cutoff and timestamp > cutoff and row.get("setup_id") != setup_id:
            candidates.append((timestamp, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def match(rows: list[dict[str, Any]], field: str, value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    found = [row for row in rows if row.get(field) == value]
    return max(found, key=lambda row: str(row.get("created_at") or row.get("timestamp") or "")) if found else None


def trace(name: str, event: dict[str, Any]) -> dict[str, Any]:
    info = INSTANCES[name]
    strategy = info["strategy_id"]
    signals = [row for row in lines(SIGNALS) if row.get("strategy_id") == strategy and row.get("symbol") == info["symbol"]]
    signal = match(signals, "setup_id", event.get("setup_id"))
    signal_id = signal.get("signal_id") if signal else None
    intents = lines(INTENTS)
    intent = match(intents, "signal_id", signal_id)
    intent_id = intent.get("execution_intent_id") if intent else None
    decisions = lines(DECISIONS)
    decision = match(decisions, "execution_intent_id", intent_id)
    skips = [row for row in lines(SKIPS) if row.get("strategy_id") == strategy and row.get("signal_id") == signal_id]
    details = (decision or {}).get("details") or {}
    received = dt(details.get("consumer_observed_at") or (decision or {}).get("created_at"))
    event_at = event_time(event)
    signal_at = dt((signal or {}).get("signal_timestamp"))
    emitted = dt((signal or {}).get("signal_emitted_at"))
    intent_at = dt((intent or {}).get("intent_created_at"))
    return {
        "strategy_id": strategy,
        "instance_id": info["instance_id"],
        "publisher_id": f"{info['instance_id']}-publisher",
        "setup_id": event.get("setup_id"),
        "source_event_id": (signal or {}).get("source_event_id"),
        "symbol": info["symbol"],
        "direction": event.get("direction"),
        "event": event,
        "signal": signal,
        "intent": intent,
        "decision": decision,
        "skips": skips,
        "explicit_signal_emitted_at": bool(emitted),
        "ages": {
            "event_age_seconds": (received - event_at).total_seconds() if received and event_at else None,
            "signal_age_seconds": (received - emitted).total_seconds() if received and emitted else None,
            "intent_age_seconds": (received - intent_at).total_seconds() if received and intent_at else None,
        },
        "first_blocking_reason": (decision or {}).get("reason") or (skips[-1].get("reason") if skips else None) or ("PUBLISHER_NOT_YET_EMITTED" if not signal else None),
        "duplicate_counts": {
            "signals": sum(1 for row in signals if row.get("setup_id") == event.get("setup_id")),
            "intents": sum(1 for row in intents if signal_id and row.get("signal_id") == signal_id),
            "decisions": sum(1 for row in decisions if intent_id and row.get("execution_intent_id") == intent_id),
        },
    }


def snapshot(previous: dict[str, Any]) -> dict[str, Any]:
    output = {
        "schema": "liquidity-live-all-observation-v1",
        "observer_pid": os.getpid(),
        "observer_started_at": previous.get("observer_started_at") or iso(),
        "last_observed_at": iso(),
        "single_canary_mode": False,
        "baseline_protection_active": True,
        "health": health(),
        "broker_writes_by_observer": 0,
        "production_ledgers_written_by_observer": False,
        "instances": previous.get("instances", {}),
    }
    for name, info in INSTANCES.items():
        old = output["instances"].get(name, {})
        item = {
            "strategy_id": info["strategy_id"],
            "instance_id": info["instance_id"],
            "symbol": info["symbol"],
            "baseline": baseline(name),
            "state_row_count": len(rows_for(name)),
            "new_event_confirmed": old.get("new_event_confirmed", False),
            "status": old.get("status", "AWAITING_NEW_EVENT"),
            "traces": old.get("traces", []),
        }
        event = find_new(name)
        seen = {trace.get("setup_id") for trace in item["traces"] if isinstance(trace, dict)}
        if event and event.get("setup_id") not in seen:
            item["traces"].append(trace(name, event))
            item["new_event_confirmed"] = True
            current = item["traces"][-1]
            if not current.get("signal"):
                item["status"] = "BLOCKED_AT_PUBLISHER"
            elif not current.get("intent"):
                item["status"] = "BLOCKED_AT_ORCHESTRATOR"
            elif current.get("decision") and (current["decision"].get("decision") or "").endswith("REJECTED"):
                reason = current["decision"].get("reason", "")
                item["status"] = "POSITION_CONFLICT" if "POSITION" in reason or "SYMBOL_DIRECTION" in reason else ("BLOCKED_AT_RISK" if "RISK" in reason or "VOLUME" in reason else "BLOCKED_AT_CONSUMER")
            elif current.get("decision"):
                item["status"] = "LIVE_PATH_VERIFIED"
        item["duplicate_suppression_pass"] = all(
            (tr.get("duplicate_counts") or {}).get("signals", 0) <= 1 and
            (tr.get("duplicate_counts") or {}).get("intents", 0) <= 1 and
            (tr.get("duplicate_counts") or {}).get("decisions", 0) <= 1
            for tr in item["traces"]
        )
        output["instances"][name] = item
    return output


def persist(payload: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".liquidity_live_all.", suffix=".tmp", dir=str(ARTIFACT.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, ARTIFACT)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--max-seconds", type=float, default=172800.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    started = time.monotonic()
    previous = read(ARTIFACT, {}) or {}
    while True:
        payload = snapshot(previous if isinstance(previous, dict) else {})
        persist(payload)
        previous = payload
        if args.once:
            return 0
        if time.monotonic() - started >= args.max_seconds:
            payload["completed_at"] = iso()
            persist(payload)
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
