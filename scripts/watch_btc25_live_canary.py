#!/usr/bin/env python3
"""Read-only observer for the enabled BTC25 live canary.

The observer never imports or calls the production orchestration writer.  It
only reads state/ledger files and process health, and writes its own durable
diagnostic artifact.  It deliberately treats the persisted BTC25 startup
baseline as historical and will not trace anything at or before it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
import statistics
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "liquidity_displacement_btc25_state.json"
BASELINE = ROOT / "runtime/orchestration/liquidity-btc25-startup-baseline.json"
SIGNALS = ROOT / "runtime/orchestration/signals.jsonl"
INTENTS = ROOT / "runtime/execution/execution_intents.jsonl"
DECISIONS = ROOT / "runtime/execution/execution_decisions.jsonl"
SKIPS = ROOT / "runtime/execution/execution_skips.jsonl"
ARTIFACT = ROOT / "artifacts/live/btc25_canary_observation.json"
HEARTBEAT = ROOT / "liquidity_displacement_btc25.heartbeat.json"
ORCH_HEARTBEAT = ROOT / "runtime/orchestration/heartbeat.json"
CONSUMER_HEARTBEAT = ROOT / "runtime/execution/heartbeat.json"
CONTEXT_HEARTBEAT = ROOT / "context_structure_retrace_forward.heartbeat.json"
EXPORT_PID = ROOT / "runtime/bridge-22350/pid"

STRATEGY = "LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1"
SYMBOL = "BTCUSDm"
BASELINE_ID = "LDS-btc25-1789815600-1789816800-LONG"
BASELINE_TIME = "2026-09-19T11:25:00+00:00"
EXPECTED_PIDS = {
    "btc25_runner": 21477,
    "orchestrator": 16941,
    "real_consumer": 77231,
    "context_phase6": 75418,
    "execution_bridge": 15989,
    "execution_mt5": 90513,
    "research_export": 18240,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat()


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default


def read_pid(path: Path, default: int | None = None) -> int | None:
    value = read_json(path, None)
    if isinstance(value, dict):
        value = value.get("pid")
    if value is None:
        try:
            value = path.read_text().strip()
        except (FileNotFoundError, OSError):
            return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def file_age(path: Path) -> float | None:
    try:
        return max(0.0, (time.time() - path.stat().st_mtime))
    except OSError:
        return None


def jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
                except json.JSONDecodeError:
                    continue
    except (FileNotFoundError, OSError):
        pass
    return rows


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {key: None for key in ("median", "mean", "p25", "p75", "p90", "maximum")}
    values = sorted(values)
    def q(p: float) -> float:
        if len(values) == 1:
            return values[0]
        position = (len(values) - 1) * p
        low, high = math.floor(position), math.ceil(position)
        if low == high:
            return values[low]
        return values[low] + (values[high] - values[low]) * (position - low)
    return {
        "median": q(.50), "mean": statistics.mean(values), "p25": q(.25),
        "p75": q(.75), "p90": q(.90), "maximum": max(values),
    }


def cadence() -> dict[str, Any]:
    state = read_json(STATE, {}) or {}
    rows = list((state.get("signals") or {}).values()) if isinstance(state, dict) else []
    events = []
    for row in rows:
        event_time = parse_time(row.get("simulated_fill_timestamp") or row.get("fill_timestamp"))
        if event_time:
            events.append((event_time, row))
    events.sort(key=lambda item: item[0])
    intervals = [(b[0] - a[0]).total_seconds() / 60.0 for a, b in zip(events, events[1:])]
    last = events[-1][0] if events else None
    return {
        "total_actionable_events": len(events),
        "first_event_timestamp": iso(events[0][0]) if events else None,
        "last_event_timestamp": iso(last) if last else None,
        "inter_event_minutes": percentiles(intervals),
        "events_per_24h_at_last_event": sum(last - item[0] <= timedelta(hours=24) for item in events) if last else 0,
        "events_over_7d_at_last_event": sum(last - item[0] <= timedelta(days=7) for item in events) if last else 0,
        "was_90_second_window_meaningful": bool(intervals and statistics.median(intervals) <= 1.5 and len(intervals) >= 3),
    }


def health() -> dict[str, Any]:
    pids = dict(EXPECTED_PIDS)
    for key, path in (("orchestrator", ROOT / "runtime/orchestration/pid"),
                      ("real_consumer", ROOT / "runtime/execution/pid")):
        discovered = read_pid(path)
        if discovered:
            pids[key] = discovered
    ages = {
        "btc25_runner": file_age(HEARTBEAT),
        "orchestrator": file_age(ORCH_HEARTBEAT),
        "real_consumer": file_age(CONSUMER_HEARTBEAT),
        "context_phase6": file_age(CONTEXT_HEARTBEAT),
    }
    bridge_reachable = False
    try:
        with socket.create_connection(("127.0.0.1", 22348), timeout=0.5):
            bridge_reachable = True
    except OSError:
        pass
    return {
        "pids": pids,
        "process_alive": {key: alive(pid) for key, pid in pids.items()},
        "heartbeat_age_seconds": ages,
        "execution_bridge_tcp_reachable": bridge_reachable,
        "research_export_pid": read_pid(EXPORT_PID, EXPECTED_PIDS["research_export"]),
        "research_export_alive": alive(read_pid(EXPORT_PID, EXPECTED_PIDS["research_export"])),
    }


def state_rows() -> list[dict[str, Any]]:
    state = read_json(STATE, {}) or {}
    rows = list((state.get("signals") or {}).values()) if isinstance(state, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def new_event(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    baseline = parse_time(BASELINE_TIME)
    candidates = []
    for row in rows:
        event = parse_time(row.get("simulated_fill_timestamp") or row.get("fill_timestamp"))
        setup_id = str(row.get("setup_id") or "")
        if event and baseline and event > baseline and setup_id != BASELINE_ID:
            candidates.append((event, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def first_match(rows: list[dict[str, Any]], field: str, value: Any) -> dict[str, Any] | None:
    matches = [row for row in rows if row.get(field) == value]
    return max(matches, key=lambda row: str(row.get("created_at") or row.get("timestamp") or "")) if matches else None


def trace_event(event: dict[str, Any]) -> dict[str, Any]:
    setup_id = event.get("setup_id")
    source_event_id = event.get("source_event_id")
    signals = [row for row in jsonl(SIGNALS) if row.get("strategy_id") == STRATEGY and row.get("symbol") == SYMBOL]
    signal = first_match(signals, "setup_id", setup_id)
    if signal is None and source_event_id:
        signal = first_match(signals, "source_event_id", source_event_id)
    signal_id = signal.get("signal_id") if signal else None
    intents = jsonl(INTENTS)
    intent = first_match(intents, "signal_id", signal_id) if signal_id else None
    intent_id = intent.get("execution_intent_id") if intent else None
    decisions = jsonl(DECISIONS)
    decision = first_match(decisions, "execution_intent_id", intent_id) if intent_id else None
    skips = [row for row in jsonl(SKIPS) if row.get("strategy_id") == STRATEGY and (row.get("signal_id") == signal_id or not signal_id)]
    decision_details = (decision or {}).get("details") or {}
    consumer_received = parse_time(decision_details.get("consumer_observed_at") or (decision or {}).get("created_at"))
    signal_timestamp = parse_time((signal or {}).get("signal_timestamp"))
    emitted = parse_time((signal or {}).get("signal_emitted_at") or (signal or {}).get("created_at"))
    intent_created = parse_time((intent or {}).get("intent_created_at") or (intent or {}).get("execution_intent_created_at"))
    ages = {
        "event_age_seconds": (consumer_received - parse_time(event.get("simulated_fill_timestamp") or event.get("fill_timestamp"))).total_seconds() if consumer_received and parse_time(event.get("simulated_fill_timestamp") or event.get("fill_timestamp")) else None,
        "signal_age_seconds": (consumer_received - emitted).total_seconds() if consumer_received and emitted else None,
        "intent_age_seconds": (consumer_received - intent_created).total_seconds() if consumer_received and intent_created else None,
    }
    return {
        "setup_id": setup_id, "source_event_id": source_event_id,
        "event": event,
        "signal": signal,
        "intent": intent,
        "decision": decision,
        "skips": skips,
        "ages": ages,
        "explicit_signal_emitted_at": bool(signal and signal.get("signal_emitted_at")),
        "first_blocking_reason": (decision or {}).get("reason") or (skips[-1].get("reason") if skips else None) or ("PUBLISHER_NOT_YET_EMITTED" if signal is None else None),
        "duplicate_counts": {
            "signals": sum(1 for row in signals if row.get("setup_id") == setup_id or row.get("source_event_id") == source_event_id),
            "intents": sum(1 for row in intents if signal_id and row.get("signal_id") == signal_id),
            "decisions": sum(1 for row in decisions if intent_id and row.get("execution_intent_id") == intent_id),
        },
    }


def update(previous: dict[str, Any] | None, once: bool = False) -> dict[str, Any]:
    previous = previous or {}
    h = health()
    result = {
        "schema": "btc25-canary-observation-v1",
        "observer_pid": os.getpid(),
        "observer_started_at": previous.get("observer_started_at") or iso(),
        "last_observed_at": iso(),
        "baseline": {"setup_id": BASELINE_ID, "timestamp": BASELINE_TIME, "protection_active": True},
        "cadence": cadence(),
        "health": h,
        "broker_writes_by_observer": 0,
        "production_ledgers_written_by_observer": False,
        "events": previous.get("events", []),
        "status": previous.get("status", "ACTIVE_AWAITING_SIGNAL"),
    }
    event = new_event(state_rows())
    seen = {item.get("setup_id") for item in result["events"] if isinstance(item, dict)}
    if event and event.get("setup_id") not in seen:
        trace = trace_event(event)
        trace["observed_at"] = iso()
        result["events"].append(trace)
        if trace.get("signal") is None:
            result["status"] = "BLOCKED_AT_PUBLISHER"
        elif trace.get("intent") is None:
            result["status"] = "BLOCKED_AT_ORCHESTRATOR"
        elif trace.get("decision") and (trace["decision"].get("decision") or "").endswith("REJECTED"):
            reason = trace["decision"].get("reason", "")
            result["status"] = "BLOCKED_AT_RISK" if "RISK" in reason or "VOLUME" in reason else "BLOCKED_AT_CONSUMER"
        elif trace.get("decision"):
            result["status"] = "LIVE_PATH_VERIFIED"
    result["btc25_new_event_confirmed"] = bool(result["events"])
    result["duplicate_suppression_pass"] = all(
        (item.get("duplicate_counts") or {}).get("signals", 0) <= 1 and
        (item.get("duplicate_counts") or {}).get("intents", 0) <= 1 and
        (item.get("duplicate_counts") or {}).get("decisions", 0) <= 1
        for item in result["events"]
    )
    return result


def persist(payload: dict[str, Any]) -> None:
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".btc25_canary.", suffix=".tmp", dir=str(ARTIFACT.parent))
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--max-seconds", type=float, default=86400.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    previous = read_json(ARTIFACT, {})
    while True:
        payload = update(previous if isinstance(previous, dict) else {})
        persist(payload)
        previous = payload
        if args.once or payload.get("status") not in {"ACTIVE_AWAITING_SIGNAL"}:
            return 0
        if time.monotonic() - started >= args.max_seconds:
            payload["status"] = "ACTIVE_NO_SIGNAL_IN_24H"
            payload["completed_at"] = iso()
            persist(payload)
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
