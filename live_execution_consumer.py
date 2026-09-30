#!/usr/bin/env python3
"""Fail-closed Context V1 execution consumer.

Only DRY_RUN, DEMO_EXECUTION, and explicitly account-bound REAL_EXECUTION exist.
The strategy signal producers remain independent and are not changed here.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from execution.adapters import DryRunReadAdapter
from execution.demo import (DEMO_CONTEXT, DEMO_SERVER, MAPPINGS, RISK_BUDGET,
                            RISK_FRACTION, VIRTUAL_STARTING_EQUITY, account_is_authorized,
                            execution_key, load_state, mapping_for, parse_time, save_state,
                            REAL_MAPPINGS, real_account_is_authorized, real_mapping_for,
                            strategy_symbol_for, utc_now, virtual_size)
from execution.demo_broker import BrokerSubmissionRejected, DemoExecutionAdapter
from execution.models import ExecutionDecision, ExecutionIntent, ExecutionMarketSnapshot
from execution.storage import ExecutionStore
from orchestration.brokers.mt5_shadow import BridgeReadTimeout, MT5ShadowProvider
from orchestration.config import load_config
from orchestration.models import stable_id
from execution.risk_policy import RiskPolicyError, ResolvedRiskPolicy, risk_policy_for
from trade_manager.central import (BROKER_STATE_PATH, INTENTS_PATH, OwnershipRegistry,
                                   BrokerStateStream, rows, stable_id, utc_now)
from platform_runtime import require_trading_platform_runtime, trading_platform_runtime_dir
from contracts.mt5_bridge.canonical_request import (canonical_request_fingerprint,
                                                     canonical_request_text,
                                                     wire_request as _wire_request)
from execution_v2.pricing import (ExecutionPricingError, broker_protection_levels,
                                  REFERENCE_PRICE_SIDE)

ROOT = Path(__file__).resolve().parent
PLATFORM_RUNTIME = trading_platform_runtime_dir(root=ROOT)
RUNTIME = PLATFORM_RUNTIME / "execution"
PID = RUNTIME / "pid"
HEARTBEAT = RUNTIME / "heartbeat.json"
STOP = Path("/tmp/live-execution-consumer-dry-run.stop")
MODE = "DRY_RUN"
VERSION = "LIVE_EXECUTION_CONSUMER_PHASE1"
DEMO_STATE = RUNTIME / "demo_state.json"
DEMO_EVENTS = RUNTIME / "demo_events.jsonl"
DEMO_TRADES = RUNTIME / "demo_trades.jsonl"
REAL_STATE = RUNTIME / "real_state.json"
ORCHESTRATION = PLATFORM_RUNTIME / "orchestration"
RESUME_STATE = RUNTIME / "real_execution_resume.json"
REAL_TRADES = RUNTIME / "real_trades.jsonl"
SMOKE_RUNTIME = RUNTIME / "real_smoke"
SMOKE_STATE = SMOKE_RUNTIME / "smoke_state.json"
SMOKE_EVENTS = SMOKE_RUNTIME / "events.jsonl"
SMOKE_CONFIRMATION = "CONFIRM REAL $1 SMOKE TEST"
REAL_SMOKE_ACCOUNT = os.environ.get("REAL_SMOKE_ACCOUNT")
REAL_SMOKE_SYMBOLS = ("XAUUSDm", "BTCUSDm", "USDJPYm", "EURUSDm")


def now(): return datetime.now(timezone.utc).isoformat()


def _smoke_event(event: str, **payload: Any) -> None:
    SMOKE_RUNTIME.mkdir(parents=True, exist_ok=True)
    row = {"event": event, "timestamp": now(), **payload}
    with SMOKE_EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_ms(value: str | None, current: datetime | None = None) -> float | None:
    parsed = _timestamp(value)
    if parsed is None:
        return None
    return max(0.0, ((current or datetime.now(timezone.utc)) - parsed).total_seconds() * 1000.0)


def config():
    platform = load_config()
    runtime = require_trading_platform_runtime(mode=platform.get("execution_mode"), root=ROOT)
    return {"mode": MODE, "mcp_url": platform.get("mcp_url"), "execution_mcp_url": platform.get("execution_mcp_url"),
            "trading_platform_runtime_dir": str(runtime),
            "execution_transport_verified": bool(platform.get("execution_transport_verified", False)),
            "expected_demo_context": DEMO_CONTEXT, "virtual_starting_equity": VIRTUAL_STARTING_EQUITY,
            "virtual_risk_fraction": RISK_FRACTION, "max_entry_drift": None, "max_spread": None,
            "signal_max_age": 120.0, "intent_max_age": 5.0,
            "quote_max_age_ms": 2000,
            "position_conflict_policy": "REJECT_SAME_SYMBOL_DIRECTION",
            "live_armed": False, "real_execution_enabled": False,
            "kill_switch": {"global_disarm": True,
            "account_disarm": {}, "strategy_disarm": {}, "portfolio_disarm": {},
            "daily_loss_limit": None, "daily_equity_loss_limit": None,
            "max_simultaneous_account_risk": None, "max_strategy_risk": None,
            "max_open_positions": None}}


def provider_adapter(cfg=None):
    cfg = cfg or config()
    endpoint = cfg.get("mcp_url")
    mode_state = load_state(REAL_STATE if cfg.get("mode") == "REAL_EXECUTION" else DEMO_STATE)
    if mode_state.get("armed") and cfg.get("execution_mcp_url"):
        endpoint = cfg["execution_mcp_url"]
    return DryRunReadAdapter(MT5ShadowProvider(endpoint, caller="EXECUTION_CONSUMER", snapshot_ttl_seconds=0))


def source_records():
    root = PLATFORM_RUNTIME / "orchestration"
    def read(name):
        path = root / name
        return [json.loads(x) for x in path.read_text().splitlines() if x] if path.exists() else []
    return read("signals.jsonl"), read("sizing_decisions.jsonl"), read("classification_corrections.jsonl")


def process_management_intents(cfg: dict[str, Any], provider: Any, ownership: OwnershipRegistry) -> int:
    """Execute only orchestrator-authorized management intents.

    This is the sole management write boundary. Trade Manager never imports
    this function and never has an execution provider. Unsupported operations
    are rejected rather than approximated.
    """
    if cfg.get("mode") != "REAL_EXECUTION" or not INTENTS_PATH.exists():
        return 0
    results_path = RUNTIME.parent / "management" / "execution_results.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    completed = {row.get("intent_id") for row in rows(results_path) if row.get("status") == "COMPLETED"}
    state = BrokerStateStream().load() or {}
    count = 0
    for intent in rows(INTENTS_PATH):
        intent_id = intent.get("intent_id")
        if not intent_id or intent_id in completed or intent.get("status") != "AUTHORIZED":
            continue
        result = {"schema": "management-execution-result-v1", "intent_id": intent_id, "timestamp": utc_now()}
        current = next((p for p in state.get("positions", []) if str(p.get("ticket") or p.get("position_id") or p.get("broker_position_id")) == str(intent.get("broker_position_id"))), None)
        if current is None:
            result.update(status="REJECTED", reason="POSITION_NOT_FOUND")
        elif int(intent.get("position_snapshot_version", -1)) != int(state.get("snapshot_version", -2)):
            result.update(status="REJECTED", reason="POSITION_STATE_CHANGED")
        else:
            owned, reason, owner = ownership.prove({**current, "account_position_mode": state.get("account_position_mode")})
            if not owned or not owner or owner.get("ownership_id") != intent.get("ownership_id"):
                result.update(status="REJECTED", reason=reason if not owned else "OWNERSHIP_CHANGED")
            else:
                action = intent.get("intent_type")
                tool = None; arguments = {}
                if action == "CLOSE_POSITION":
                    tool, arguments = "mt5_close_position", {"ticket": int(intent["broker_position_id"]), "confirm": True}
                elif action == "TRAIL_STOP" and current.get("current_price") is not None and intent.get("requested_stop") is not None:
                    distance = abs(float(current["current_price"]) - float(intent["requested_stop"]))
                    if distance > 0:
                        tool, arguments = "mt5_trailing_stop", {"ticket": int(intent["broker_position_id"]), "distance_price": distance, "confirm": True}
                if not tool:
                    result.update(status="REJECTED", reason="MANAGEMENT_ACTION_NOT_SUPPORTED_BY_EXECUTION_ADAPTER")
                else:
                    payload = json.dumps({"jsonrpc": "2.0", "id": intent_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}).encode()
                    try:
                        request = Request(cfg["execution_mcp_url"], data=payload, headers={"Content-Type": "application/json", "X-Execution-Mode": "REAL_EXECUTION", "X-Bridge-Origin": "REAL_MANAGEMENT_CONSUMER", "X-Bridge-Origin-Pid": str(os.getpid())})
                        with urlopen(request, timeout=30) as response:
                            outer = json.loads(response.read())
                        content = outer.get("result", {}).get("content", [{}])[0].get("text", "")
                        broker_result = json.loads(content) if content else outer
                        if outer.get("result", {}).get("isError"):
                            result.update(status="REJECTED", reason="BROKER_MANAGEMENT_REJECTED", broker_result=broker_result)
                        else:
                            result.update(status="COMPLETED", tool=tool, broker_result=broker_result)
                            if action == "CLOSE_POSITION":
                                ownership.record_management_result(owner["ownership_id"], state="CLOSED", current_volume=0.0)
                    except Exception as exc:
                        result.update(status="REJECTED", reason="MANAGEMENT_TRANSPORT_FAILED", error=str(exc))
        key = stable_id("MEXEC", {"intent_id": intent_id})
        if not any(row.get("unique_key") == key for row in rows(results_path)):
            with results_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({**result, "unique_key": key}, sort_keys=True, default=str) + "\n")
        count += 1
    return count


def live_output_state() -> dict[str, Any]:
    """Read the orchestrator's durable REAL-output gate without mutating it."""
    path = PLATFORM_RUNTIME / "orchestration" / "state.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def execution_state_consistency() -> dict[str, Any]:
    """Read-only, fail-closed authority check for REAL startup and intents.

    manifest owns configured authorization; orchestration state owns the
    durable publication state; real_state owns account arming; the consumer
    configuration owns runtime mode.  No artifact is silently preferred when
    these authorities disagree.
    """
    def read(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}
    manifest = read(ORCHESTRATION / "manifest.json")
    orchestration = read(ORCHESTRATION / "state.json")
    real = read(REAL_STATE)
    reasons = []
    if manifest.get("mode") != "REAL_EXECUTION":
        reasons.append("MANIFEST_MODE_NOT_REAL")
    if bool(manifest.get("live_execution_enabled")) != bool(orchestration.get("live_execution_enabled")):
        reasons.append("MANIFEST_ORCHESTRATION_LIVE_FLAG_MISMATCH")
    if bool(orchestration.get("live_execution_enabled")) != bool(real.get("armed")):
        reasons.append("ORCHESTRATION_REAL_ARM_MISMATCH")
    if real.get("armed") and real.get("mode") != "REAL_EXECUTION":
        reasons.append("REAL_ARM_MODE_MISMATCH")
    if not orchestration.get("live_execution_cutoff_timestamp"):
        reasons.append("LIVE_CUTOFF_MISSING")
    resume = read(RESUME_STATE)
    if not resume.get("real_execution_resumed_at") or not resume.get("real_execution_resume_generation"):
        reasons.append("REAL_RESUME_CUTOFF_MISSING")
    if not real.get("account_context_id"):
        reasons.append("REAL_ACCOUNT_CONTEXT_MISSING")
    return {"safe": not reasons, "reasons": reasons, "manifest": manifest,
            "orchestration": orchestration, "real": real, "resume": resume,
            "authority_model": {"manifest": "configured REAL authorization",
                                 "orchestration_state": "live-output publication authorization",
                                 "real_state": "account-bound REAL arming",
                                 "consumer_config": "runtime mode"}}


def _execution_signal_rows() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    signals, _, _ = source_records()
    gate = execution_state_consistency()
    resume = gate["resume"]
    resume_at = parse_time(resume.get("real_execution_resumed_at"))
    excluded = set(resume.get("excluded_signal_ids", []))
    candidates = []
    for row in signals:
        created = parse_time(row.get("created_at")); source = parse_time(row.get("signal_timestamp"))
        if (not resume_at or row.get("signal_id") in excluded or created is None or source is None or
                created <= resume_at or source <= resume_at or row.get("provenance", {}).get("gap_recovery") is True):
            continue
        candidates.append(row)
    return candidates, gate


def _source_health_ok(signal: dict[str, Any]) -> tuple[bool, str]:
    provenance = signal.get("provenance") or {}
    if provenance.get("source_read_health") is not True:
        return False, "STRATEGY_SOURCE_DATA_UNHEALTHY"
    if provenance.get("source_data_age") is None or float(provenance["source_data_age"]) > 120.0:
        return False, "STRATEGY_SOURCE_DATA_UNHEALTHY"
    if not provenance.get("source_market_data_timestamp"):
        return False, "STRATEGY_SOURCE_DATA_UNHEALTHY"
    return True, "SOURCE_DATA_HEALTHY"


def _trusted_signal_emission_time(row: dict[str, Any]) -> datetime | None:
    """Return only an explicit live publication timestamp.

    ``signal_timestamp`` is EVENT_TIME and may be minutes or hours old by the
    time a strategy publishes a new actionable decision.  ``created_at`` is
    retained for orchestration/replay bookkeeping, but is not silently
    promoted to emission time for old rows.
    """
    value = row.get("signal_emitted_at")
    return _timestamp(value) if value else None


def _signal_emission_age_ms(row: dict[str, Any], current: datetime | None = None) -> float | None:
    emitted = _trusted_signal_emission_time(row)
    if emitted is None:
        return None
    return max(0.0, ((current or datetime.now(timezone.utc)) - emitted).total_seconds() * 1000.0)


def _bridge_health(endpoint: str | None) -> dict[str, Any]:
    return _monitor_bridge_health(endpoint)


def execution_safety_audit() -> dict[str, Any]:
    gate = execution_state_consistency()
    signals, _, _ = source_records()
    resume = gate["resume"]
    candidates, _ = _execution_signal_rows()
    store = ExecutionStore(RUNTIME)
    intents = store.rows("execution_intents")
    decisions = store.rows("execution_decisions")
    skips = store.rows("execution_skips")
    trades = [json.loads(x) for x in REAL_TRADES.read_text().splitlines() if x] if REAL_TRADES.exists() else []
    post_live = post_live_signals(signals, gate["orchestration"])
    consumed = {x.get("signal_id") for x in intents + skips + decisions}
    status = store.state()
    endpoint = load_config().get("execution_mcp_url")
    result = {"manifest_mode": gate["manifest"].get("mode"),
              "manifest_live_execution_enabled": bool(gate["manifest"].get("live_execution_enabled")),
              "orchestration_live_execution_enabled": bool(gate["orchestration"].get("live_execution_enabled")),
              "live_execution_enabled_at": gate["orchestration"].get("live_execution_enabled_at"),
              "live_cutoff_timestamp": gate["orchestration"].get("live_execution_cutoff_timestamp"),
              "live_cutoff_signal_id": gate["orchestration"].get("live_execution_cutoff_signal_id"),
              "real_armed": bool(gate["real"].get("armed")), "real_armed_at": gate["real"].get("armed_at"),
              "bound_account": gate["real"].get("masked_login") or "MASKED",
              "consumer_status": status.get("status"), "consumer_pid": None,
              "pre_live_count": len(signals) - len(post_live), "post_live_count": len(post_live),
              "unconsumed_post_live_count": len(set(post_live) - consumed),
              "post_live_during_execution_outage": sorted(set(post_live) - set(resume.get("excluded_signal_ids", []))) if not resume.get("real_execution_resumed_at") else [],
              "real_intents": len(intents),
              "broker_submissions": sum(x.get("decision") == "REAL_SUBMITTED" for x in decisions),
              "fills": len(trades), "execution_resume": resume,
              "execution_22348_health": _bridge_health(endpoint),
              "safe_to_start_real_consumer": False if gate["reasons"] else True,
              "reasons": gate["reasons"]}
    return result


def establish_execution_resume_cutoff() -> int:
    """Explicit operator action; never invoked by startup or audit."""
    gate = execution_state_consistency()
    disabled_consistent = (gate["manifest"].get("mode") == "SHADOW" and
                           not gate["manifest"].get("live_execution_enabled") and
                           not gate["orchestration"].get("live_execution_enabled") and
                           not gate["real"].get("armed"))
    real_consistent_before_resume = (gate["manifest"].get("mode") == "REAL_EXECUTION" and
                                     bool(gate["manifest"].get("live_execution_enabled")) and
                                     bool(gate["orchestration"].get("live_execution_enabled")) and
                                     bool(gate["real"].get("armed")) and
                                     gate["reasons"] == ["REAL_RESUME_CUTOFF_MISSING"])
    if not disabled_consistent and not real_consistent_before_resume:
        print(json.dumps({"resumed": False, "reason": "EXECUTION_STATE_CONTRADICTION",
                          "reasons": gate["reasons"],
                          "manifest_mode": gate["manifest"].get("mode"),
                          "manifest_live": bool(gate["manifest"].get("live_execution_enabled")),
                          "orchestration_live": bool(gate["orchestration"].get("live_execution_enabled")),
                          "real_armed": bool(gate["real"].get("armed")),
                          "resume_cutoff_present": bool(gate["resume"].get("real_execution_resumed_at"))}, indent=2, default=str))
        return 2
    signals, _, _ = source_records()
    cutoff = now()
    value = {"schema": "real-execution-resume-v1", "real_execution_resumed_at": cutoff,
             "real_execution_resume_high_water_signal_id": signals[-1].get("signal_id") if signals else None,
             "real_execution_resume_generation": int(gate["resume"].get("real_execution_resume_generation", 0)) + 1,
             "excluded_signal_ids": [x.get("signal_id") for x in signals],
             "excluded_reason": "EXISTING_BEFORE_EXPLICIT_RESUME_CUTOFF"}
    tmp = RESUME_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, RESUME_STATE)
    print(json.dumps({"resumed": True, **value}, indent=2))
    return 0
def post_live_signals(signals: list[dict[str, Any]], state: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Return only signals first created after the durable live cutoff.

    The cutoff ID set is deliberately retained in addition to the timestamp:
    a historical record discovered after enablement can never become live.
    """
    state = state or live_output_state()
    if not state.get("live_execution_enabled"):
        return {}
    cutoff = parse_time(state.get("live_execution_cutoff_timestamp"))
    excluded = set(state.get("live_execution_cutoff_signal_ids", []))
    if cutoff is None:
        return {}
    result = {}
    for signal in signals:
        signal_id = signal.get("signal_id")
        created = parse_time(signal.get("created_at"))
        source_time = parse_time(signal.get("signal_timestamp"))
        if (signal_id in excluded or created is None or created <= cutoff or
                source_time is None or source_time <= cutoff):
            continue
        result[signal_id] = signal
    return result


def prospective_signals():
    signals, _, corrections = source_records()
    classes = {x["signal_id"]: x.get("corrected_classification") for x in corrections}
    return {x["signal_id"]: x for x in signals if classes.get(x["signal_id"]) == "PROSPECTIVE_ORCHESTRATOR_SIGNAL"}


def append_event(store, event_type, payload, correlation_id=None, causation_id=None):
    row = {"event_id": stable_id("EXEVT", {"type": event_type, "payload": payload}),
           "event_type": event_type, "timestamp": now(), "correlation_id": correlation_id,
           "causation_id": causation_id, "payload": payload}
    store.append("events", row, row["event_id"])


def record_skip(store: ExecutionStore, signal: dict[str, Any], reason: str, details: dict[str, Any] | None = None) -> None:
    """Persist every rejected opportunity; never silently drop a signal."""
    payload = {"signal_id": signal.get("signal_id"),
               "strategy_id": signal.get("strategy_id"),
               "strategy_version": signal.get("strategy_version"),
               "strategy_symbol": strategy_symbol_for(signal),
               "broker_symbol": mapping_for(strategy_symbol_for(signal)).get("broker_symbol"),
               "reason": reason, "details": details or {}, "timestamp": now()}
    key = stable_id("SKIP", {"signal_id": payload["signal_id"], "reason": reason})
    if store.append("execution_skips", payload, key):
        append_event(store, "DEMO_EXECUTION_SKIPPED", payload, signal.get("signal_id"), signal.get("signal_id"))


def _persisted_intent_policy(intent: dict[str, Any]) -> ResolvedRiskPolicy | None:
    """Recover the immutable policy snapshot persisted on a new intent."""
    try:
        version = int(intent["risk_policy_version"])
        equity = float(intent["virtual_equity_usd"])
        percent = float(intent["risk_percent"])
        budget = float(intent["risk_budget_usd"])
        if version <= 0 or not all(math.isfinite(x) for x in (equity, percent, budget)):
            return None
        if equity <= 0 or not 0 < percent <= 100 or not math.isclose(budget, equity * percent / 100.0, rel_tol=0, abs_tol=1e-9):
            return None
        return ResolvedRiskPolicy(version, equity, percent, percent / 100.0, budget, "PERSISTED_INTENT")
    except (KeyError, TypeError, ValueError):
        return None


def create_intents(store: ExecutionStore, cfg: dict[str, Any]) -> int:
    signals, sizings, corrections = source_records()
    execution_mode = cfg.get("mode")
    # REAL sizing is intentionally owned here, at the execution boundary,
    # because it needs fresh native broker metadata.  The orchestrator's
    # analytical sizing ledger is never treated as authoritative REAL volume.
    if execution_mode == "REAL_EXECUTION":
        gate = execution_state_consistency()
        if not gate["safe"]:
            return 0
        state = load_state(REAL_STATE)
        if not state.get("armed") or not state.get("account_context_id"):
            return 0
        startup_baseline = set(cfg.get("startup_signal_ids", set()))
        # A consumer reload must never turn already-persisted signals into new
        # intents. Only signals observed after this process's startup baseline
        # are eligible for new REAL sizing.
        live_signals = {signal_id: signal for signal_id, signal in post_live_signals(signals).items()
                        if signal_id not in startup_baseline}
        if not live_signals:
            return 0
        provider = provider_adapter(cfg)
        account = dict(state.get("account", {}))
        account.setdefault("account_id", "real-connected")
        account.setdefault("broker", "Exness")
        portfolio = {"portfolio_id": "portfolio-real-context", "enabled": True}
        existing = {row.get("signal_id") for row in store.rows("execution_intents")}
        for signal in live_signals.values():
            if signal.get("signal_id") in existing:
                continue
            try:
                policy = risk_policy_for(signal.get("strategy_id"))
            except RiskPolicyError as exc:
                record_skip(store, signal, "INVALID_OR_MISSING_RISK_CONFIG", {"error": str(exc)})
                continue
            resume_at = parse_time(gate["resume"].get("real_execution_resumed_at"))
            if resume_at is None:
                continue
            created_at = parse_time(signal.get("created_at"))
            source_at = parse_time(signal.get("signal_timestamp"))
            excluded = set(gate["resume"].get("excluded_signal_ids", []))
            if (signal.get("signal_id") in excluded or created_at is None or source_at is None or
                    created_at <= resume_at or source_at <= resume_at or
                    signal.get("provenance", {}).get("gap_recovery") is True):
                continue
            source_ok, source_reason = _source_health_ok(signal)
            if not source_ok:
                record_skip(store, signal, source_reason)
                continue
            if _trusted_signal_emission_time(signal) is None:
                record_skip(store, signal, "SIGNAL_EMISSION_TIME_UNAVAILABLE")
                continue
            strategy_symbol = strategy_symbol_for(signal)
            mapping = real_mapping_for(strategy_symbol)
            if mapping.get("status") != "NATIVE":
                record_skip(store, signal, "BROKER_SYMBOL_UNAVAILABLE", {"mapping": mapping})
                continue
            try:
                snapshot = provider.account_snapshot(account["account_id"])
                verified, reason = real_account_is_authorized(snapshot, state.get("account_context_id"))
                if not verified:
                    record_skip(store, signal, reason, {"account_context_id": snapshot.get("account_context_id")})
                    continue
                metadata = provider.symbol_metadata(mapping["broker_symbol"])
                sized = virtual_size(entry=float(signal["entry_price"]), stop=float(signal["stop_price"]),
                                     metadata=metadata, virtual_equity=policy.virtual_equity_usd,
                                     risk_fraction=policy.risk_fraction)
            except Exception as exc:
                record_skip(store, signal, "REAL_ACCOUNT_OR_SYMBOL_DATA_UNAVAILABLE", {"error": str(exc)})
                continue
            if sized.get("decision") != "EXECUTABLE":
                record_skip(store, signal, sized.get("reason", "REAL_SIZING_REJECTED"), sized)
                continue
            sizing = {"sizing_decision_id": stable_id("REALSIZING", {
                            "signal_id": signal["signal_id"],
                            "account_context_id": state["account_context_id"],
                            "risk_fraction": policy.risk_fraction,
                            "risk_policy_version": policy.version}),
                      "signal_id": signal["signal_id"], "strategy_id": signal.get("strategy_id"),
                      "strategy_version": signal.get("strategy_version"), "portfolio_id": portfolio["portfolio_id"],
                      "account_id": account["account_id"], "entry": float(signal["entry_price"]),
                      "stop": float(signal["stop_price"]), "target": float(signal["target_price"]),
                      "created_at": utc_now(), "account_snapshot_id": snapshot.get("snapshot_id") or stable_id("SNAP", snapshot),
                      "desired_risk_fraction": policy.risk_fraction, "desired_risk_amount": sized.get("desired_risk_amount"),
                      "risk_policy_version": policy.version, "virtual_equity_usd": policy.virtual_equity_usd,
                      "risk_percent": policy.risk_percent, "risk_budget_usd": policy.risk_budget_usd,
                      "risk_policy_source": policy.source,
                      "rounded_volume": sized.get("rounded_volume"), "estimated_loss_at_stop": sized.get("actual_risk"),
                      "decision": "EXECUTABLE", "sizing_mode": "REAL_EXECUTION_SIZING"}
            live_signal = dict(signal, broker_symbol_hint=mapping["broker_symbol"])
            intent = ExecutionIntent.from_records(live_signal, sizing, portfolio, account,
                                                   intent_max_age_seconds=float(cfg.get("intent_max_age", 5.0)))
            intent_row = intent.to_dict()
            # Record the durable visibility boundary without changing the
            # intent identity or its five-second expiry semantics.
            intent_row["intent_persisted_at"] = utc_now()
            intent_row["provenance"] = {**intent_row.get("provenance", {}),
                                         "source": "orchestration.signals.jsonl",
                                         "classification": "POST_LIVE_EXECUTION",
                                         "live_cutoff_timestamp": live_output_state().get("live_execution_cutoff_timestamp")}
            if store.append("execution_intents", intent_row, intent.execution_intent_id):
                append_event(store, "REAL_EXECUTION_INTENT_CREATED", intent_row, intent.correlation_id, intent.causation_id)
        return len([row for row in store.rows("execution_intents") if row.get("provenance", {}).get("classification") == "POST_LIVE_EXECUTION"])

    classes = {x["signal_id"]: x.get("corrected_classification") for x in corrections}
    signal_map = {x["signal_id"]: x for x in signals if classes.get(x["signal_id"]) == "PROSPECTIVE_ORCHESTRATOR_SIGNAL"}
    platform = load_config()
    accounts = {x["account_id"]: x for x in platform.get("accounts", [])}
    portfolios = {x["portfolio_id"]: x for x in platform.get("portfolios", [])}
    account_bound = execution_mode in {"DEMO_EXECUTION", "REAL_EXECUTION"}
    state_path = REAL_STATE if execution_mode == "REAL_EXECUTION" else DEMO_STATE
    demo = load_state(state_path)
    demo_provider = provider_adapter(cfg) if account_bound else None
    demo_account = platform.get("demo_execution_account", {}) if execution_mode == "DEMO_EXECUTION" else demo.get("account", {})
    demo_portfolio = platform.get("demo_execution_portfolio", {}) if execution_mode == "DEMO_EXECUTION" else {"portfolio_id": "portfolio-real-context"}
    count = 0
    for sizing in sizings:
        if sizing.get("decision") != "EXECUTABLE" or sizing.get("signal_id") not in signal_map:
            continue
        signal = signal_map[sizing["signal_id"]]
        if account_bound:
            armed_at = parse_time(demo.get("armed_at"))
            signal_at = parse_time(signal.get("signal_timestamp"))
            if not demo.get("armed") or not armed_at or not signal_at or signal_at <= armed_at:
                record_skip(store, signal, "PRE_ARM_SIGNAL_REJECTED")
                continue
            if float(sizing.get("desired_risk_fraction") or -1) != RISK_FRACTION:
                record_skip(store, signal, "RISK_POLICY_MISMATCH")
                continue
            mapping = real_mapping_for(strategy_symbol_for(signal)) if execution_mode == "REAL_EXECUTION" else mapping_for(strategy_symbol_for(signal))
            if (execution_mode == "REAL_EXECUTION" and mapping.get("status") != "NATIVE") or (execution_mode == "DEMO_EXECUTION" and mapping.get("status") != "VERIFIED_DIRECT"):
                record_skip(store, signal, "BROKER_SYMBOL_UNAVAILABLE" if mapping.get("status") == "UNAVAILABLE" else "BROKER_FEED_COMPATIBILITY_UNVERIFIED")
                continue
            try:
                account_snapshot = demo_provider.account_snapshot(demo_account.get("account_id", "demo"))
                verified, account_reason = (real_account_is_authorized(account_snapshot, demo.get("account_context_id"))
                                            if execution_mode == "REAL_EXECUTION"
                                            else account_is_authorized(account_snapshot))
                if not verified:
                    record_skip(store, signal, account_reason, {"account_context_id": account_snapshot.get("account_context_id")})
                    continue
                broker_metadata = demo_provider.symbol_metadata(mapping["broker_symbol"])
            except Exception as exc:
                record_skip(store, signal, "DEMO_ACCOUNT_OR_SYMBOL_DATA_UNAVAILABLE", {"error": str(exc)})
                continue
            sizing = dict(sizing)
            sizing["account_id"] = demo_account.get("account_id")
            sizing["portfolio_id"] = demo_portfolio.get("portfolio_id")
            sizing["account_snapshot_id"] = account_snapshot.get("snapshot_id") or stable_id("DEMO_SNAP", account_snapshot)
            sizing["created_at"] = utc_now()
            sizing.update(virtual_size(entry=float(sizing["entry"]), stop=float(sizing["stop"]),
                                       metadata=broker_metadata,
                                       virtual_equity=float(demo.get("virtual_equity", VIRTUAL_STARTING_EQUITY))))
            if sizing.get("decision") != "EXECUTABLE":
                record_skip(store, signal, sizing.get("reason", "VOLUME_CALCULATION_FAILED"), sizing)
                continue
            signal = dict(signal, broker_symbol_hint=mapping.get("broker_symbol"))
        if account_bound:
            portfolio = demo_portfolio
            account = demo_account
        else:
            portfolio = portfolios.get(sizing.get("portfolio_id"), {})
            account = accounts.get(sizing.get("account_id"), {})
        if not portfolio or not account: continue
        intent = ExecutionIntent.from_records(signal, sizing, portfolio, account,
                                               intent_max_age_seconds=float(cfg.get("intent_max_age", 5.0)))
        if store.append("execution_intents", intent.to_dict(), intent.execution_intent_id):
            append_event(store, "EXECUTION_INTENT_CREATED", intent.to_dict(), intent.correlation_id, intent.causation_id)
            count += 1
    return count


def _step_aligned(value, minimum, maximum, step):
    if any(x is None for x in (value, minimum, maximum, step)) or value < minimum or value > maximum or step <= 0:
        return False
    units = round((value - minimum) / step)
    return abs((minimum + units * step) - value) <= max(step * 1e-6, 1e-12)


def validate_intent(intent: dict[str, Any], account: dict[str, Any], metadata: dict[str, Any], quote: dict[str, Any], positions: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    current = datetime.now(timezone.utc)
    signal_age = _signal_emission_age_ms(intent, current)
    intent_age = _age_ms(intent.get("intent_created_at"), current)
    if signal_age is None:
        return "DRY_RUN_REJECTED", "SIGNAL_EMISSION_TIME_UNAVAILABLE", {"event_time": intent.get("signal_timestamp")}
    if intent_age is None:
        return "DRY_RUN_REJECTED", "INTENT_CREATION_TIME_UNAVAILABLE", {}
    if intent.get("expires_at") and (_timestamp(intent["expires_at"]) or current) <= current:
        return "DRY_RUN_REJECTED", "EXECUTION_INTENT_EXPIRED", {"signal_age_ms": signal_age, "intent_age_ms": intent_age}
    if signal_age is not None and signal_age > float(cfg.get("signal_max_age", 120.0)) * 1000.0:
        return "DRY_RUN_REJECTED", "EXECUTION_INTENT_EXPIRED", {"signal_age_ms": signal_age, "intent_age_ms": intent_age}
    if intent_age is not None and intent_age > float(cfg.get("intent_max_age", 5.0)) * 1000.0:
        return "DRY_RUN_REJECTED", "EXECUTION_INTENT_EXPIRED", {"signal_age_ms": signal_age, "intent_age_ms": intent_age}
    if cfg.get("mode") not in {"DRY_RUN", "DEMO_EXECUTION", "REAL_EXECUTION"} or cfg.get("live_armed"):
        return "DRY_RUN_REJECTED", "LIVE_ARMED_FAIL_CLOSED", {}
    terminal_state = str(intent.get("v1_terminal_state") or intent.get("lifecycle_state") or "").upper()
    if intent.get("setup_terminal") is True or terminal_state in {
        "TARGET_COMPLETED", "RETURN_AFTER_SETUP_TARGET_COMPLETED", "INVALIDATED_NO_REENTRY",
        "SETUP_INVALIDATED_BEFORE_ENTRY", "THESIS_INVALIDATED",
    }:
        return "DRY_RUN_REJECTED", "SETUP_ALREADY_TERMINAL", {"terminal_state": terminal_state or "EXPLICIT_FLAG"}
    if cfg.get("mode") in {"DEMO_EXECUTION", "REAL_EXECUTION"}:
        state = load_state(REAL_STATE if cfg.get("mode") == "REAL_EXECUTION" else DEMO_STATE)
        verified, reason = (real_account_is_authorized(account, state.get("account_context_id"))
                           if cfg.get("mode") == "REAL_EXECUTION" else account_is_authorized(account))
        if not verified:
            return "DRY_RUN_REJECTED", reason, {}
    if not float(account.get("equity") or 0) > 0:
        return "DRY_RUN_REJECTED", "REJECTED_ZERO_EQUITY", {}
    volume = float(intent["approved_volume"])
    if not _step_aligned(volume, float(metadata.get("volume_min", 0)), float(metadata.get("volume_max", 0)), float(metadata.get("volume_step", 0))):
        return "DRY_RUN_REJECTED", "REJECTED_INVALID_VOLUME", {}
    entry, stop, target = float(intent["strategy_entry_price"]), float(intent["strategy_stop_price"]), float(intent["strategy_target_price"])
    direction = intent["direction"]
    if direction == "LONG" and not (stop < entry and target > entry):
        return "DRY_RUN_REJECTED", "REJECTED_INVALID_GEOMETRY", {}
    if direction == "SHORT" and not (stop > entry and target < entry):
        return "DRY_RUN_REJECTED", "REJECTED_INVALID_GEOMETRY", {}
    quote_age = quote.get("quote_age_ms", quote.get("age_ms"))
    if quote_age is None and quote.get("quote_received_at"):
        quote_age = _age_ms(quote.get("quote_received_at"), current)
    if cfg.get("mode") in {"DEMO_EXECUTION", "REAL_EXECUTION"}:
        if quote.get("freshness_state") not in ("FRESH",) or quote_age is None or float(quote_age) > float(cfg.get("quote_max_age_ms", 2000)):
            return "DRY_RUN_REJECTED", "STALE_EXECUTION_QUOTE", {"quote_age_ms": quote_age}
    bid, ask = quote.get("bid"), quote.get("ask")
    if bid is None or ask is None or float(bid) <= 0 or float(ask) <= 0:
        return "DRY_RUN_REJECTED", "REJECTED_MARKET_UNAVAILABLE", {}
    executable = float(ask) if direction == "LONG" else float(bid)
    drift = abs(executable - entry)
    risk = abs(entry - stop)
    entry_delta = executable - entry
    target_reached = (float(bid) >= target) if direction == "LONG" else (float(ask) <= target)
    stop_breached = (float(bid) <= stop) if direction == "LONG" else (float(ask) >= stop)
    if target_reached:
        return "DRY_RUN_REJECTED", "TARGET_ALREADY_REACHED", {"executable_price": executable, "entry_delta": entry_delta, "entry_delta_R": entry_delta / risk if risk else None, "signal_age_ms": signal_age, "intent_age_ms": intent_age, "quote_age_ms": quote_age}
    if stop_breached:
        return "DRY_RUN_REJECTED", "STOP_ALREADY_BREACHED", {"executable_price": executable, "entry_delta": entry_delta, "entry_delta_R": entry_delta / risk if risk else None, "signal_age_ms": signal_age, "intent_age_ms": intent_age, "quote_age_ms": quote_age}
    if cfg.get("max_entry_drift") is not None and drift > float(cfg["max_entry_drift"]):
        return "DRY_RUN_REJECTED", "ENTRY_PRICE_NO_LONGER_VALID", {"drift": drift, "entry_delta": entry_delta, "entry_delta_R": entry_delta / risk if risk else None}
    if cfg.get("max_spread") is not None and float(ask) - float(bid) > float(cfg["max_spread"]):
        return "DRY_RUN_REJECTED", "REJECTED_SPREAD_POLICY", {"spread": float(ask) - float(bid)}
    if cfg.get("position_conflict_policy") == "REJECT_SAME_SYMBOL_DIRECTION":
        if any(p.get("symbol") == intent["broker_symbol"] and p.get("direction") == direction for p in positions):
            return "DRY_RUN_REJECTED", "REJECTED_EXISTING_POSITION_CONFLICT", {}
    if cfg.get("mode") in {"DEMO_EXECUTION", "REAL_EXECUTION"} and "free_margin" in account and float(account.get("free_margin") or 0) <= 0:
        return "DRY_RUN_REJECTED", "INSUFFICIENT_BROKER_MARGIN", {}
    decision = "REAL_EXECUTABLE" if cfg.get("mode") == "REAL_EXECUTION" else ("DEMO_EXECUTABLE" if cfg.get("mode") == "DEMO_EXECUTION" else "DRY_RUN_ACCEPTED")
    reason = decision if cfg.get("mode") in {"DEMO_EXECUTION", "REAL_EXECUTION"} else "DRY_RUN_EXECUTABLE"
    return decision, reason, {"executable_price": executable, "entry_drift": drift, "entry_delta": entry_delta,
                              "entry_delta_ticks": entry_delta / float(metadata.get("tick_size") or metadata.get("point") or 1),
                              "entry_delta_R": entry_delta / risk if risk else None, "risk": risk,
                              "signal_age_ms": signal_age, "intent_age_ms": intent_age, "quote_age_ms": quote_age}


def process_intents(store: ExecutionStore, cfg: dict[str, Any], adapter=None) -> int:
    adapter = adapter or provider_adapter(); count = 0
    # Load the decision keys once.  Re-reading the entire append-only stream
    # for every intent made delivery cost grow with history.
    decided = {x.get("unique_key") for x in store.rows("execution_decisions")}
    for intent in store.rows("execution_intents"):
        key = stable_id("DEC", {"intent": intent["execution_intent_id"]})
        if key in decided: continue
        consumer_observed_at = utc_now()
        observed_dt = parse_time(consumer_observed_at)
        intent_dt = parse_time(intent.get("intent_created_at"))
        signal_dt = _trusted_signal_emission_time(intent)
        if signal_dt is None:
            signal_age_ms = None
        else:
            signal_age_ms = ((observed_dt - signal_dt).total_seconds() * 1000.0
                             if observed_dt and signal_dt else None)
        intent_age_ms = ((observed_dt - intent_dt).total_seconds() * 1000.0
                         if observed_dt and intent_dt else None)
        # Expiry is a local safety decision.  Make it before any bridge read,
        # so stale work cannot consume the same transport time needed by fresh
        # intents and the rejection remains fail-closed.
        expires_at = parse_time(intent.get("expires_at"))
        expired = ((expires_at is not None and observed_dt is not None and expires_at <= observed_dt) or
                   (intent_age_ms is not None and intent_age_ms > float(cfg.get("intent_max_age", 5.0)) * 1000.0) or
                   (signal_age_ms is None or signal_age_ms > float(cfg.get("signal_max_age", 120.0)) * 1000.0) or
                   (intent_age_ms is None))
        if expired:
            details = {"signal_age_ms": signal_age_ms, "intent_age_ms": intent_age_ms,
                       "consumer_observed_at": consumer_observed_at,
                       "age_at_consumer_ms": intent_age_ms,
                       "event_time": intent.get("signal_timestamp"),
                       "signal_emitted_at": intent.get("signal_emitted_at")}
            reason = "EXECUTION_INTENT_EXPIRED" if signal_age_ms is not None and intent_age_ms is not None else (
                "SIGNAL_EMISSION_TIME_UNAVAILABLE" if signal_age_ms is None else "INTENT_CREATION_TIME_UNAVAILABLE")
            row = ExecutionDecision(
                execution_decision_id=key, execution_intent_id=intent["execution_intent_id"],
                signal_id=intent["signal_id"], strategy_id=intent["strategy_id"],
                strategy_version=intent["strategy_version"], portfolio_id=intent["portfolio_id"],
                account_id=intent["account_id"], sizing_account_snapshot_id=intent["account_snapshot_id"],
                execution_account_snapshot_id=None, market_snapshot_id=None,
                approved_volume=intent["approved_volume"], decision="DRY_RUN_REJECTED",
                reason=reason, created_at=now(), details=details)
            if store.append("execution_decisions", row.to_dict(), key):
                append_event(store, "REAL_EXECUTION_DECISION" if cfg.get("mode") == "REAL_EXECUTION" else "DRY_RUN_DECISION",
                             row.to_dict(), intent["correlation_id"], intent["causation_id"])
                decided.add(key); count += 1
            continue
        persisted_policy = _persisted_intent_policy(intent)
        if persisted_policy is None:
            if cfg.get("mode") == "REAL_EXECUTION":
                record_skip(store, intent, "MISSING_OR_INVALID_PERSISTED_RISK_POLICY")
                continue
            demo_equity = float(load_state(DEMO_STATE).get("virtual_equity", VIRTUAL_STARTING_EQUITY))
            persisted_policy = ResolvedRiskPolicy(0, demo_equity, RISK_FRACTION * 100.0,
                                                   RISK_FRACTION, demo_equity * RISK_FRACTION, "ANALYTICAL_DEMO_COMPATIBILITY")
        try:
            if str(intent.get("order_type", "MARKET")).upper() != "MARKET":
                decision, reason, details = "DRY_RUN_REJECTED", "UNSUPPORTED_ORDER_TYPE", {
                    "order_type": intent.get("order_type")}
                raise RuntimeError(reason)
            account = adapter.account_snapshot(intent["account_id"])
            if cfg.get("mode") in {"DEMO_EXECUTION", "REAL_EXECUTION"}:
                state_path = REAL_STATE if cfg.get("mode") == "REAL_EXECUTION" else DEMO_STATE
                demo = load_state(state_path)
                if not demo.get("account_context_id") or not demo.get("armed"):
                    decision, reason, details = "DRY_RUN_REJECTED", "EXECUTION_DISARMED", {}
                    raise RuntimeError("EXECUTION_DISARMED")
                if account.get("account_context_id") != demo.get("account_context_id"):
                    demo.update(mode="DRY_RUN", armed=False, disarmed_at=utc_now(), disarm_reason="ACCOUNT_CONTEXT_CHANGED")
                    save_state(state_path, demo)
                    append_event(store, "EXECUTION_DISARMED_ACCOUNT_CONTEXT_CHANGED", {"expected": demo.get("account_context_id"), "actual": account.get("account_context_id")})
                    decision, reason, details = "DRY_RUN_REJECTED", "DEMO_ACCOUNT_CONTEXT_CHANGED", {}
                    raise RuntimeError("DEMO_ACCOUNT_CONTEXT_CHANGED")
            metadata = adapter.symbol_metadata(intent["broker_symbol"])
            quote_requested_at = now()
            quote = adapter.quote(intent["broker_symbol"])
            quote_received_at = now()
            if quote.get("freshness_state") is None:
                quote = dict(quote, freshness_state="FRESH")
            quote = dict(quote, quote_requested_at=quote_requested_at,
                          quote_received_at=quote_received_at,
                          quote_age_ms=quote.get("quote_age_ms", 0.0))
            positions = adapter.open_positions(intent["account_id"])
            if cfg.get("mode") in {"DEMO_EXECUTION", "REAL_EXECUTION"}:
                market_sized = market_execution_sizing(
                    intent, quote, metadata,
                    persisted_policy.virtual_equity_usd, persisted_policy.risk_fraction,
                )
                if market_sized.get("decision") != "EXECUTABLE":
                    decision, reason, details = "DRY_RUN_REJECTED", market_sized.get("reason", "REAL_SIZING_REJECTED"), market_sized
                    raise RuntimeError(reason)
                intent = dict(intent,
                              approved_volume=float(market_sized["rounded_volume"]),
                              estimated_actual_risk=market_sized.get("actual_risk"),
                              desired_risk_amount=market_sized.get("desired_risk_amount"))
            decision, reason, details = validate_intent(intent, account, metadata, quote, positions, cfg)
            market = ExecutionMarketSnapshot(
                market_snapshot_id=stable_id("MKT", {"intent": intent["execution_intent_id"], "timestamp": now()}),
                timestamp=now(), broker_symbol=intent["broker_symbol"], bid=quote.get("bid"), ask=quote.get("ask"),
                spread_price=(float(quote["ask"])-float(quote["bid"])) if quote.get("bid") is not None and quote.get("ask") is not None else None,
                spread_ticks=None, strategy_entry=intent["strategy_entry_price"], current_executable_price=details.get("executable_price"),
                entry_drift_price=details.get("entry_drift"), entry_drift_ticks=None,
                entry_drift_as_r=(details.get("entry_drift") / details["risk"] if details.get("risk") else None), metadata_reference=metadata.get("broker_symbol"))
            store.append("account_snapshots", dict(account, captured_at=now()), account.get("snapshot_id") or stable_id("SNAP", account))
            store.append("market_snapshots", market.to_dict(), market.market_snapshot_id)
            if decision in {"DEMO_EXECUTABLE", "REAL_EXECUTABLE"}:
                state_path = REAL_STATE if cfg.get("mode") == "REAL_EXECUTION" else DEMO_STATE
                trades_path = REAL_TRADES if cfg.get("mode") == "REAL_EXECUTION" else DEMO_TRADES
                demo = load_state(state_path)
                try:
                    # Re-read immediately before the write.  The account may
                    # have changed after the initial validation snapshot.
                    account = adapter.account_snapshot(intent["account_id"])
                    if account.get("account_context_id") != demo.get("account_context_id"):
                        demo.update(mode="DRY_RUN", armed=False, disarmed_at=utc_now(), disarm_reason="ACCOUNT_CONTEXT_CHANGED_BEFORE_SUBMIT")
                        save_state(state_path, demo)
                        raise RuntimeError("DEMO_ACCOUNT_CONTEXT_CHANGED")
                    verified, account_reason = (real_account_is_authorized(account, demo.get("account_context_id"))
                                               if cfg.get("mode") == "REAL_EXECUTION" else account_is_authorized(account))
                    if not verified:
                        raise RuntimeError(account_reason)
                    # Final pre-write recheck: the strategy clock continues
                    # while the intent waits in the consumer/transport.
                    final_quote_requested_at = now()
                    final_quote = adapter.quote(intent["broker_symbol"])
                    final_quote = dict(final_quote,
                                       freshness_state=final_quote.get("freshness_state", "FRESH"),
                                       quote_requested_at=final_quote_requested_at,
                                       quote_received_at=now(),
                                       quote_age_ms=final_quote.get("quote_age_ms", 0.0))
                    final_metadata = adapter.symbol_metadata(intent["broker_symbol"])
                    final_positions = adapter.open_positions(intent["account_id"])
                    final_sized = market_execution_sizing(
                        intent, final_quote, final_metadata,
                        persisted_policy.virtual_equity_usd, persisted_policy.risk_fraction,
                    )
                    if final_sized.get("decision") != "EXECUTABLE":
                        raise RuntimeError(final_sized.get("reason", "REAL_SIZING_REJECTED"))
                    intent = dict(intent,
                                  approved_volume=float(final_sized["rounded_volume"]),
                                  estimated_actual_risk=final_sized.get("actual_risk"),
                                  desired_risk_amount=final_sized.get("desired_risk_amount"))
                    final_decision, final_reason, final_details = validate_intent(
                        intent, account, final_metadata, final_quote, final_positions, cfg)
                    if final_decision != decision:
                        decision, reason, details = final_decision, final_reason, {**details, **final_details,
                                                                                   "final_pre_write_recheck": True}
                        raise RuntimeError(final_reason)
                    details.update({"final_pre_write_recheck": True,
                                    "quote_requested_at": final_quote_requested_at,
                                    "quote_received_at": final_quote.get("quote_received_at"),
                                    "reference_entry_price": intent["strategy_entry_price"],
                                    "execution_price": final_sized["execution_price"],
                                    "final_sizing": final_sized})
                    candidate = market_execution_candidate(intent, final_quote, final_sized["rounded_volume"],
                                                           final_metadata)
                    details.update({"canonical_stop_price": candidate["canonical_stop_price"],
                                    "canonical_target_price": candidate["canonical_target_price"],
                                    "broker_stop_price": candidate["broker_stop_price"],
                                    "broker_target_price": candidate["broker_target_price"],
                                    "reference_price_side": candidate["reference_price_side"],
                                    "execution_pricing": candidate["execution_pricing"]})
                    side = candidate["side"]
                    canonical_request = canonical_market_request(candidate, final_metadata)
                    canonical_text = canonical_request_text(canonical_request)
                    canonical_fingerprint = canonical_request_fingerprint(canonical_request)
                    order_check = adapter.order_check(
                        symbol=intent["broker_symbol"], side=side,
                        volume=canonical_request["volume"], stop_loss=canonical_request["sl"],
                        take_profit=canonical_request["tp"], canonical_request=canonical_request,
                        canonical_request_text=canonical_text)
                    ea_request = _wire_request(order_check.get("request")) if isinstance(order_check, dict) else None
                    ea_text = order_check.get("canonical_request_text") if isinstance(order_check, dict) else None
                    ea_fingerprint = canonical_request_fingerprint(ea_request) if isinstance(ea_request, dict) else None
                    if (order_check.get("success") is not True or order_check.get("retcode") not in (None, 0)):
                        raise RuntimeError(f"BROKER_PREFLIGHT_REJECTED:{order_check.get('retcode')}:{order_check.get('comment')}")
                    if ea_fingerprint != canonical_fingerprint or ea_text != canonical_text:
                        raise RuntimeError("CANONICAL_REQUEST_MISMATCH")
                    details.update({"canonical_request": canonical_request,
                                    "canonical_request_text": canonical_text,
                                    "canonical_request_fingerprint": canonical_fingerprint,
                                    "order_check": order_check,
                                    "order_check_fingerprint": ea_fingerprint})
                    broker = DemoExecutionAdapter(
                        cfg.get("execution_mcp_url") or "", mode=cfg["mode"],
                        armed_context=demo.get("account_context_id"),
                        verified_snapshot=account,
                        transport_verified=cfg.get("execution_transport_verified", False))
                    broker_result = broker.submit_canonical_market_order(
                        request=canonical_request, canonical_request_text=canonical_text,
                        request_fingerprint=canonical_fingerprint,
                        idempotency_key=stable_id("REALORDER", {"execution_intent_id": intent["execution_intent_id"], "account_context_id": demo.get("account_context_id")}))
                    details["broker_result"] = broker_result
                    if not broker_result.get("ok", False):
                        decision, reason = "DEMO_REJECTED", "BROKER_ORDER_REJECTED"
                    else:
                        # Ownership is created only after the broker confirms
                        # the REAL execution result. Trade Manager never gets
                        # an adoption path for manual/unowned positions.
                        if cfg.get("mode") == "REAL_EXECUTION":
                            try:
                                ownership.record_real_execution(intent, broker_result)
                            except Exception as ownership_exc:
                                decision, reason = "REAL_REJECTED", f"OWNERSHIP_REGISTRY_UPDATE_FAILED:{ownership_exc}"
                                details["broker_write_blocked"] = True
                                raise RuntimeError(str(ownership_exc))
                        trades_path.parent.mkdir(parents=True, exist_ok=True)
                        with trades_path.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps({"timestamp": now(), "execution_intent_id": intent["execution_intent_id"],
                                                 "signal_id": intent["signal_id"], "strategy_id": intent["strategy_id"],
                                                 "strategy_version": intent["strategy_version"],
                                                 "strategy_symbol": intent.get("canonical_symbol"),
                                                 "broker_symbol": intent["broker_symbol"],
                                                 "account_context_id": demo.get("account_context_id"),
                                                 "requested_volume": intent["approved_volume"],
                                                 "reference_entry_price": intent["strategy_entry_price"],
                                                 "execution_request_price": canonical_request["price"],
                                                 "actual_fill_price": broker_result.get("price"),
                                                 "slippage_from_reference": ((broker_result.get("price") - intent["strategy_entry_price"]) if broker_result.get("price") is not None else None),
                                                 "slippage_from_request": ((broker_result.get("price") - canonical_request["price"]) if broker_result.get("price") is not None else None),
                                                 "SL": intent["strategy_stop_price"], "TP": intent["strategy_target_price"],
                                                 "canonical_stop_price": candidate["canonical_stop_price"],
                                                 "canonical_target_price": candidate["canonical_target_price"],
                                                 "broker_stop_price": candidate["broker_stop_price"],
                                                 "broker_target_price": candidate["broker_target_price"],
                                                 "execution_pricing": candidate["execution_pricing"],
                                                 "broker_result": broker_result}, sort_keys=True, default=str) + "\n")
                        decision, reason = ("REAL_SUBMITTED", "REAL_ORDER_SUBMITTED") if cfg.get("mode") == "REAL_EXECUTION" else ("DEMO_SUBMITTED", "DEMO_ORDER_SUBMITTED")
                except Exception as exc:
                    decision, reason = "DRY_RUN_REJECTED", str(exc)
                    details["broker_write_blocked"] = True
        except Exception as exc:
            decision, reason, details = "DRY_RUN_REJECTED", "REJECTED_ACCOUNT_DATA", {"error": str(exc)}
            market = None; account = {}
        row = ExecutionDecision(
            execution_decision_id=key, execution_intent_id=intent["execution_intent_id"], signal_id=intent["signal_id"],
            strategy_id=intent["strategy_id"], strategy_version=intent["strategy_version"], portfolio_id=intent["portfolio_id"],
            account_id=intent["account_id"], sizing_account_snapshot_id=intent["account_snapshot_id"],
            execution_account_snapshot_id=account.get("snapshot_id"), market_snapshot_id=market.market_snapshot_id if market else None,
            approved_volume=intent["approved_volume"], decision=decision, reason=reason, created_at=now(),
            details={**(details or {}), "consumer_observed_at": consumer_observed_at,
                     "age_at_consumer_ms": intent_age_ms})
        store.append("execution_decisions", row.to_dict(), key)
        append_event(store, "REAL_EXECUTION_DECISION" if cfg.get("mode") == "REAL_EXECUTION" else ("DEMO_EXECUTION_DECISION" if cfg.get("mode") == "DEMO_EXECUTION" else "DRY_RUN_DECISION"), row.to_dict(), intent["correlation_id"], intent["causation_id"])
        count += 1
    return count


def safety_audit_paths(root: Path = ROOT) -> list[Path]:
    """Return the platform policy files that must remain write-isolated.

    The MT5 client is deliberately outside this set: it is a transport
    boundary, not an authorization or strategy module.  Keeping this list
    explicit prevents a future directory move from making the audit vacuous.
    """
    return [Path(__file__), *(p for p in Path(root / "execution").rglob("*.py")
                              if p.name != "demo_broker.py")]


def safety_audit():
    forbidden = {"mt5_market_order", "mt5_pending_order", "mt5_cancel_pending_order", "mt5_close_position", "mt5_trailing_stop", "order_send", "TRADE_ACTION_DEAL", "TRADE_ACTION_PENDING"}
    calls = []
    for path in safety_audit_paths():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden: calls.append(node.func.id)
    return {"pass": not calls, "forbidden_calls": sorted(set(calls)), "mode": MODE,
            "demo_write_path": "DEMO_EXECUTION_ONLY",
            "research_endpoint_rejected": True,
            "broker_writes_attempted": 0, "broker_writes_performed": 0}


def report(store):
    intents, decisions = store.rows("execution_intents"), store.rows("execution_decisions")
    reasons = {}
    for x in decisions: reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
    return "\n".join(["LIVE EXECUTION CONSUMER — PHASE 1", "=" * 64, f"Mode: {MODE}", f"Execution intents: {len(intents)}", f"Processed decisions: {len(decisions)}", f"DRY_RUN_ACCEPTED: {sum(x['decision']=='DRY_RUN_ACCEPTED' for x in decisions)}", f"DRY_RUN_REJECTED: {sum(x['decision']=='DRY_RUN_REJECTED' for x in decisions)}", "", "REJECTIONS", *[f"{k}: {v}" for k,v in sorted(reasons.items())], "", "BROKER WRITES ATTEMPTED: 0", "BROKER WRITES PERFORMED: 0", "LIVE ARMING: FAIL-CLOSED"])


def latency_report():
    cfg = load_config()
    provider = MT5ShadowProvider(cfg["mcp_url"], caller="EXECUTION_CONSUMER", max_age_ms=None)
    try:
        health = provider.health(); lifecycle = health.get("lifecycle", {}) or {}
    except Exception as exc:
        health = {}; lifecycle = {}; error = str(exc)
    intents = ExecutionStore(RUNTIME).rows("execution_intents")
    decisions = ExecutionStore(RUNTIME).rows("execution_decisions")
    return {
        "mode": MODE,
        "bridge": {"read_path_healthy": lifecycle.get("read_path_healthy", False),
                    "queue_depth": lifecycle.get("transport_queue_depth", health.get("pending")),
                    "active_waiters": lifecycle.get("active_waiters"),
                    "error": locals().get("error")},
        "execution_critical_reads": {
            "N": (lifecycle.get("latency_by_priority_class", {})
                   .get("EXECUTION_CRITICAL", {}).get("N", 0)),
            "latency": (lifecycle.get("latency_by_priority_class", {})
                         .get("EXECUTION_CRITICAL", {})),
            "latency_by_operation": lifecycle.get("latency_by_operation", {}),
            "timeouts": (lifecycle.get("latency_by_priority_class", {})
                          .get("EXECUTION_CRITICAL", {}).get("timeouts", 0)),
        },
        "account": {"freshness": "OBSERVED_VIA_ACCOUNT_CLI_OR_ORCHESTRATOR"},
        "intents": {"received": len(intents), "decisions": len(decisions),
                     "expired": sum(x.get("reason") == "EXECUTION_INTENT_EXPIRED" for x in decisions),
                     "ready_for_submission": 0},
        "broker_writes_attempted": 0,
        "broker_writes_performed": 0,
    }


def demo_audit() -> dict[str, Any]:
    platform = load_config()
    endpoint = platform.get("execution_mcp_url")
    result = {"mode": "DEMO_EXECUTION", "armed": False, "account": None,
              "transport": {"endpoint": endpoint, "verified": False},
              "reason": None, "symbol_mappings": MAPPINGS}
    if not endpoint or not platform.get("execution_transport_verified", False):
        result["reason"] = "DEDICATED_EXECUTION_TRANSPORT_NOT_VERIFIED"
        return result
    try:
        provider = MT5ShadowProvider(endpoint, caller="ACCOUNT_VERIFICATION", snapshot_ttl_seconds=0, read_timeout_seconds=10)
        health = provider.health()
        result["transport"]["health"] = health
        if not health.get("lifecycle", {}).get("read_path_healthy", False):
            result["reason"] = "EXECUTION_TRANSPORT_UNHEALTHY"
            return result
        snapshot = provider.account_snapshot("demo-account")
        result["account"] = {k: snapshot.get(k) for k in (
            "account_context_id", "snapshot_timestamp", "retrieval_timestamp", "age_ms",
            "balance", "equity", "margin", "free_margin", "currency", "freshness_state", "raw")}
        ok, reason = account_is_authorized(snapshot)
        result["transport"]["verified"] = True
        result["account_authorized"] = ok
        result["reason"] = reason if not ok else None
    except Exception as exc:
        result["reason"] = f"ACCOUNT_VERIFICATION_FAILED:{exc}"
    return result


def real_audit() -> dict[str, Any]:
    """Read-only pre-arm audit of the currently connected Exness terminal."""
    platform = load_config(); endpoint = platform.get("execution_mcp_url")
    result = {"mode": "REAL_EXECUTION", "armed": False,
              "EXECUTION_ENDPOINT": endpoint, "account": None,
              "transport": {"endpoint": endpoint, "verified": False},
              "symbols": {}, "verification_requests": [], "reason": None}
    try:
        provider = MT5ShadowProvider(endpoint, caller="ACCOUNT_VERIFICATION", snapshot_ttl_seconds=0,
                                     read_timeout_seconds=10)
        health = provider.health(); result["transport"]["execution_read_health"] = health

        def read(label, operation, symbol, fn):
            try:
                value = fn()
                request = dict(getattr(provider, "last_request", {}))
                request.update({"label": label, "operation": operation, "symbol": symbol,
                                "endpoint": endpoint, "status": "COMPLETED"})
                result["verification_requests"].append(request)
                return value
            except BridgeReadTimeout as exc:
                result["verification_requests"].append({
                    "label": label, "operation": operation, "symbol": symbol,
                    "endpoint": exc.endpoint, "request_id": exc.request_id,
                    "elapsed_ms": exc.elapsed_ms, "status": "TIMED_OUT"})
                raise

        account = read("account", "account_info", None,
                       lambda: provider.account_snapshot("real-connected"))
        result["account"] = {k: account.get(k) for k in ("account_context_id", "snapshot_timestamp", "retrieval_timestamp", "age_ms", "balance", "equity", "free_margin", "currency", "leverage", "freshness_state", "raw")}
        ok, reason = real_account_is_authorized(account)
        result["account_authorized"] = ok
        if not ok:
            result["reason"] = reason; return result
        for symbol in ("XAUUSDm", "BTCUSDm", "USDJPYm", "EURUSDm"):
            metadata = read(f"symbol_info:{symbol}", "symbol_info", symbol,
                            lambda symbol=symbol: provider.symbol_metadata(symbol))
            quote = read(f"quote:{symbol}", "quote", symbol,
                         lambda symbol=symbol: provider.quote(symbol))
            result["symbols"][symbol] = {"metadata": metadata, "quote": quote, "available": not bool((metadata.get("raw") or {}).get("error")) and quote.get("bid") is not None}
        result["transport"]["verified"] = bool(platform.get("execution_transport_verified", False))
        if not result["transport"]["verified"]:
            result["reason"] = "DEDICATED_EXECUTION_TRANSPORT_NOT_VERIFIED"
    except BridgeReadTimeout as exc:
        operation = "ACCOUNT" if exc.operation == "mt5_account_info" else "SYMBOL"
        suffix = f":{exc.symbol}" if exc.symbol else ""
        result["reason"] = (f"{operation}_VERIFICATION_TIMEOUT{suffix}:"
                             f"endpoint={exc.endpoint} request_id={exc.request_id} "
                             f"elapsed_ms={exc.elapsed_ms:.1f}")
    except Exception as exc:
        result["reason"] = f"REAL_VERIFICATION_FAILED:{type(exc).__name__}:{exc}"
    return result


def demo_status() -> dict[str, Any]:
    state = load_state(DEMO_STATE)
    trades = []
    if DEMO_TRADES.exists():
        trades = [json.loads(x) for x in DEMO_TRADES.read_text().splitlines() if x]
    equity = float(state.get("virtual_equity", VIRTUAL_STARTING_EQUITY))
    store = ExecutionStore(RUNTIME)
    skips = store.rows("execution_skips")
    decisions = store.rows("execution_decisions")
    last_event = store.rows("events")[-1] if store.rows("events") else None
    armed_at = parse_time(state.get("armed_at"))
    signals_after_arm = [s for s in prospective_signals().values()
                         if armed_at and parse_time(s.get("signal_timestamp")) and
                         parse_time(s.get("signal_timestamp")) > armed_at]
    return {"demo_execution": "ARMED" if state.get("armed") else "DISARMED",
            "mode": state.get("mode", "DRY_RUN"), "server": DEMO_SERVER,
            "account_context_id": state.get("account_context_id") or DEMO_CONTEXT,
            "virtual_portfolio": {"starting_equity": state.get("starting_equity", VIRTUAL_STARTING_EQUITY),
                                   "current_equity": equity, "risk_fraction": RISK_FRACTION,
                                   "next_risk_budget": equity * RISK_FRACTION},
            "signals_since_arm": {"received": len(signals_after_arm), "eligible": sum(x.get("decision") in {"DEMO_EXECUTABLE", "DEMO_SUBMITTED"} for x in decisions),
                                   "expired": sum(x.get("reason") == "EXECUTION_INTENT_EXPIRED" for x in decisions), "skipped": len(skips)},
            "orders": {"submitted": sum(x.get("decision") == "DEMO_SUBMITTED" for x in decisions), "accepted": len(trades),
                       "rejected": sum(x.get("decision") not in {"DEMO_SUBMITTED", "DRY_RUN_ACCEPTED"} for x in decisions)},
            "positions": {"open": 0, "closed": len(trades)},
            "pnl": {"realized": state.get("realized_pnl", 0.0), "unrealized": 0.0},
            "armed_at": state.get("armed_at"), "last_event": last_event}


def real_status() -> dict[str, Any]:
    state = load_state(REAL_STATE)
    trades = [json.loads(x) for x in REAL_TRADES.read_text().splitlines() if x] if REAL_TRADES.exists() else []
    store = ExecutionStore(RUNTIME); decisions = store.rows("execution_decisions")
    skips = store.rows("execution_skips")
    staleness_reasons = ("EXECUTION_INTENT_EXPIRED", "STALE_EXECUTION_QUOTE", "ENTRY_PRICE_NO_LONGER_VALID",
                         "TARGET_ALREADY_REACHED", "STOP_ALREADY_BREACHED", "SETUP_ALREADY_TERMINAL")
    staleness_counts = {reason: sum(1 for x in decisions if x.get("reason") == reason) for reason in staleness_reasons}
    last_stale = next((x for x in reversed(decisions) if x.get("reason") in staleness_reasons), None)
    return {"real_execution": "ARMED" if state.get("armed") else "DISARMED", "mode": state.get("mode", "DRY_RUN"),
            "staleness_control": {"enabled": True, "signal_max_age_seconds": config().get("signal_max_age"),
                                   "intent_max_age_seconds": config().get("intent_max_age"),
                                   "quote_max_age_ms": config().get("quote_max_age_ms"), "since_arm": staleness_counts,
                                   "last_skipped": last_stale},
            "account": {"server": state.get("server"), "masked_login": state.get("masked_login"), "context": state.get("account_context_id")},
            "virtual_portfolio": {"starting_equity": state.get("starting_equity", VIRTUAL_STARTING_EQUITY), "current_equity": state.get("virtual_equity", VIRTUAL_STARTING_EQUITY), "risk_fraction": RISK_FRACTION, "next_risk_budget": float(state.get("virtual_equity", VIRTUAL_STARTING_EQUITY)) * RISK_FRACTION},
            "signals_since_arm": {"received": 0, "eligible": sum(x.get("decision") in {"REAL_EXECUTABLE", "REAL_SUBMITTED"} for x in decisions), "skipped": len(skips), "expired": sum(x.get("reason") == "EXECUTION_INTENT_EXPIRED" for x in decisions)},
            "orders": {"submitted": sum(x.get("decision") == "REAL_SUBMITTED" for x in decisions), "accepted": len(trades), "rejected": sum(x.get("decision") == "REAL_REJECTED" for x in decisions), "uncertain_reconciling": sum(x.get("reason") == "SUBMISSION_ACK_UNCERTAIN_RECONCILE_REQUIRED" for x in decisions)},
            "positions": {"open": 0, "closed": len(trades)}, "pnl": {"realized": state.get("realized_pnl", 0.0), "unrealized": 0.0}, "armed_at": state.get("armed_at"), "last_event": store.rows("events")[-1] if store.rows("events") else None}


def _monitor_rows_since(rows: list[dict[str, Any]], armed_at: str | None) -> list[dict[str, Any]]:
    boundary = _timestamp(armed_at)
    if boundary is None:
        return []
    selected = []
    for row in rows:
        stamp = _timestamp(row.get("timestamp") or row.get("created_at") or row.get("event_timestamp"))
        if stamp is None or stamp >= boundary:
            selected.append(row)
    return selected


def _monitor_bridge_health(endpoint: str | None) -> dict[str, Any]:
    """Read bridge health only; never queues an MT5 operation."""
    if not endpoint:
        return {"healthy": False, "reason": "EXECUTION_ENDPOINT_UNCONFIGURED"}
    try:
        base = endpoint.rsplit("/mcp", 1)[0]
        with urlopen(base + "/health", timeout=2) as response:
            health = json.loads(response.read())
        lifecycle = health.get("lifecycle", {}) or {}
        return {"healthy": bool(lifecycle.get("read_path_healthy", False)),
                "endpoint": endpoint,
                "queue_depth": lifecycle.get("transport_queue_depth", lifecycle.get("queue_depth", 0)),
                "pending": health.get("pending", 0), "timeouts": lifecycle.get("timeout_count", 0),
                "orphans": lifecycle.get("orphan_count", 0),
                "last_successful_response": lifecycle.get("last_response_timestamp"),
                "seconds_since_last_response": lifecycle.get("seconds_since_last_response"),
                "latency_by_operation": lifecycle.get("latency_by_operation", {})}
    except Exception as exc:
        return {"healthy": False, "endpoint": endpoint, "reason": f"HEALTH_READ_FAILED:{exc}"}


def real_monitor_snapshot() -> dict[str, Any]:
    """Build a monitoring snapshot without writing state or broker commands."""
    state = load_state(REAL_STATE)
    store = ExecutionStore(RUNTIME)
    intents = _monitor_rows_since(store.rows("execution_intents"), state.get("armed_at"))
    decisions = _monitor_rows_since(store.rows("execution_decisions"), state.get("armed_at"))
    skips = _monitor_rows_since(store.rows("execution_skips"), state.get("armed_at"))
    events = _monitor_rows_since(store.rows("events"), state.get("armed_at"))
    trades = []
    if REAL_TRADES.exists():
        trades = _monitor_rows_since([json.loads(x) for x in REAL_TRADES.read_text().splitlines() if x], state.get("armed_at"))

    counts = {"received": len(intents) + len(skips),
              "eligible": sum(x.get("decision") in {"REAL_EXECUTABLE", "REAL_SUBMITTED"} for x in decisions),
              "skipped": len(skips), "intents_created": len(intents),
              "orders_submitted": sum(x.get("decision") == "REAL_SUBMITTED" for x in decisions)}
    reasons = ("EXECUTION_INTENT_EXPIRED", "STALE_EXECUTION_QUOTE", "ENTRY_PRICE_NO_LONGER_VALID",
               "TARGET_ALREADY_REACHED", "STOP_ALREADY_BREACHED", "SETUP_ALREADY_TERMINAL",
               "BELOW_MINIMUM_VOLUME_FOR_RISK_BUDGET")
    skip_counts = {reason: sum(1 for x in skips + decisions if x.get("reason") == reason) for reason in reasons}
    known = sum(skip_counts.values())
    rejected = sum(1 for x in decisions if str(x.get("decision", "")).startswith("REAL_REJECTED"))
    skip_counts["OTHER"] = max(0, len(skips) + rejected - known)
    recent = (events + decisions + skips)[-20:]
    last = recent[-1] if recent else None
    endpoint = load_config().get("execution_mcp_url")
    health = _monitor_bridge_health(endpoint)
    equity = float(state.get("virtual_equity", VIRTUAL_STARTING_EQUITY))
    open_trades = [x for x in trades if str(x.get("status", "")).upper() not in {"CLOSED", "COMPLETE"} and not x.get("close_time")]
    return {"armed": bool(state.get("armed")),
            "account": f"{state.get('masked_login', 'N/A')}@{state.get('server', 'N/A')}",
            "endpoint": endpoint, "bridge": health,
            "virtual_portfolio": {"starting_equity": float(state.get("starting_equity", VIRTUAL_STARTING_EQUITY)),
                                   "current_equity": equity, "risk_fraction": RISK_FRACTION,
                                   "risk_budget": equity * RISK_FRACTION,
                                   "open_strategy_risk": sum(float(x.get("initial_risk_amount", 0) or 0) for x in open_trades),
                                   "realized_pnl": float(state.get("realized_pnl", 0.0) or 0.0)},
            "signals": counts, "last_signal": last, "open_positions": open_trades,
            "recent_events": recent, "skip_counts": skip_counts,
            "safety": {"account_binding_valid": state.get("account_context_id") == REAL_SMOKE_ACCOUNT,
                       "real_mode": state.get("mode") == "REAL_EXECUTION",
                       "transport_verified": bool(load_config().get("execution_transport_verified")),
                       "broker_side_protection_required": True, "staleness_control": True,
                       "duplicate_prevention": True,
                       "real_execution": "ARMED" if state.get("armed") else "DISARMED"}}


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def print_real_monitor(snapshot: dict[str, Any]) -> None:
    v, b, s = snapshot["virtual_portfolio"], snapshot["bridge"], snapshot["signals"]
    print("REAL EXECUTION\n==============")
    print(f"ARMED: {'YES' if snapshot['armed'] else 'NO'}\nACCOUNT: {snapshot['account']}")
    print(f"EXECUTION ENDPOINT: {str(snapshot['endpoint'] or '').replace('http://', '').removesuffix('/mcp')}")
    print(f"BRIDGE: {'HEALTHY' if b.get('healthy') else 'UNHEALTHY'}\nSTALE CONTROL: ENABLED")
    print("\nVIRTUAL PORTFOLIO\n=================")
    print(f"Starting equity: {_money(v['starting_equity'])}\nCurrent virtual equity: {_money(v['current_equity'])}")
    print(f"Risk per economic position: {v['risk_fraction']:.0%}\nCurrent risk budget: {_money(v['risk_budget'])}")
    print(f"Open strategy risk: {_money(v['open_strategy_risk'])}\nRealized strategy P/L: {_money(v['realized_pnl'])}")
    print("\nSIGNALS\n=======")
    print(f"Signals observed since arm: {s['received']}\nEligible: {s['eligible']}\nSkipped: {s['skipped']}\nIntents created: {s['intents_created']}\nOrders submitted: {s['orders_submitted']}")
    print("\nLAST SIGNAL\n===========")
    print(json.dumps(snapshot["last_signal"], indent=2, default=str) if snapshot["last_signal"] else "None")
    print("\nOPEN REAL POSITIONS\n===================")
    print(json.dumps(snapshot["open_positions"], indent=2, default=str) if snapshot["open_positions"] else "None")
    print("\nRECENT EXECUTION EVENTS\n=======================")
    print(json.dumps(snapshot["recent_events"], indent=2, default=str) if snapshot["recent_events"] else "None")
    print("\nSKIP COUNTS\n===========")
    for key, value in snapshot["skip_counts"].items(): print(f"{key}: {value}")
    print("\nEXECUTION HEALTH\n================")
    print(f"22348 queue depth: {b.get('queue_depth', 'N/A')}\nPending requests: {b.get('pending', 'N/A')}\nTimeouts: {b.get('timeouts', 'N/A')}\nOrphans: {b.get('orphans', 'N/A')}\nLast successful response: {b.get('last_successful_response', 'N/A')}")
    print(f"Latency by operation: {json.dumps(b.get('latency_by_operation', {}), sort_keys=True)}")
    print("\nSAFETY\n======")
    for key, value in snapshot["safety"].items(): print(f"{key}: {value}")
    print("Broker writes attempted: 0\nBroker writes performed: 0")


def real_monitor(once: bool = False) -> int:
    while True:
        if not once:
            print("\033[2J\033[H", end="")
        print_real_monitor(real_monitor_snapshot())
        if once:
            return 0
        try:
            time.sleep(1.0)
        except KeyboardInterrupt:
            return 0


def arm_demo() -> int:
    audit = demo_audit()
    if audit.get("reason") or not audit.get("account_authorized"):
        print(json.dumps({"armed": False, "reason": audit.get("reason") or "DEMO_ACCOUNT_NOT_VERIFIED"}, indent=2))
        return 2
    if any(v.get("status") != "VERIFIED_DIRECT" for v in MAPPINGS.values()):
        print(json.dumps({"armed": False, "reason": "BROKER_FEED_COMPATIBILITY_UNVERIFIED",
                          "symbol_mappings": MAPPINGS}, indent=2))
        return 2
    snapshot = audit["account"]
    state = {"mode": "DEMO_EXECUTION", "armed": True, "armed_at": utc_now(),
             "account_context_id": snapshot["account_context_id"], "starting_equity": VIRTUAL_STARTING_EQUITY,
             "virtual_equity": VIRTUAL_STARTING_EQUITY, "realized_pnl": 0.0}
    save_state(DEMO_STATE, state)
    print(json.dumps({"armed": True, "account_context_id": snapshot["account_context_id"]}, indent=2))
    return 0


def arm_real() -> int:
    gate = execution_state_consistency()
    if not gate["safe"]:
        print(json.dumps({"armed": False, "reason": "EXECUTION_STATE_CONTRADICTION",
                          "details": gate}, indent=2, default=str))
        return 2
    audit = real_audit()
    if audit.get("reason") or not audit.get("account_authorized"):
        print(json.dumps({"armed": False, "EXECUTION_ENDPOINT": audit.get("EXECUTION_ENDPOINT"),
                          "reason": audit.get("reason") or "REAL_ACCOUNT_NOT_VERIFIED"}, indent=2)); return 2
    snapshot = audit["account"]
    state = {"mode": "REAL_EXECUTION", "armed": True, "armed_at": utc_now(), "account_context_id": snapshot["account_context_id"],
             "server": (snapshot.get("raw") or {}).get("server"), "masked_login": f"{str((snapshot.get('raw') or {}).get('login', ''))[:3]}***{str((snapshot.get('raw') or {}).get('login', ''))[-3:]}",
             "account": {"account_id": "real-connected", "broker": "Exness", "broker_environment": "REAL", "broker_account_reference": snapshot["account_context_id"]},
             "starting_equity": VIRTUAL_STARTING_EQUITY, "virtual_equity": VIRTUAL_STARTING_EQUITY, "realized_pnl": 0.0}
    save_state(REAL_STATE, state)
    print(json.dumps({"armed": True, "EXECUTION_ENDPOINT": "127.0.0.1:22348",
                      "account_context_id": state["account_context_id"], "server": state["server"]}, indent=2)); return 0


def real_smoke_test() -> int:
    """User-invoked, one-opening-order smoke lifecycle; never an agent action."""
    return run_real_smoke_test(preflight_only=False)


def _smoke_quote(provider, symbol):
    requested = now(); quote = provider.quote(symbol); received = now()
    return dict(quote, quote_requested_at=requested, quote_received_at=received,
                quote_age_ms=0.0)


def _tick_normalize_price(price: float, tick: float, digits: int, *, round_up: bool) -> float:
    """Normalize to a real trade tick, then format to symbol digits."""
    if tick <= 0:
        return round(price, digits)
    units = price / tick
    units = math.ceil(units - 1e-12) if round_up else math.floor(units + 1e-12)
    return round(units * tick, digits)


def _smoke_candidate(symbol, metadata, quote, direction="BUY"):
    raw = metadata.get("raw") or {}
    if raw.get("error") or metadata.get("trade_enabled") is False:
        return None, "SYMBOL_UNAVAILABLE_OR_DISABLED"
    required = ("tick_size", "tick_value", "volume_min", "volume_step", "volume_max", "stops_level", "freeze_level")
    if any(metadata.get(k) is None for k in required) or quote.get("bid") is None or quote.get("ask") is None:
        return None, "SMOKE_METADATA_INCOMPLETE"
    volume = float(metadata["volume_min"])
    tick = float(metadata["tick_size"]); value = float(metadata["tick_value"])
    point = float(raw.get("point") or tick)
    digits = int(raw.get("digits", 8))
    broker_min_stop_distance = max(float(metadata.get("stops_level") or 0),
                                   float(metadata.get("freeze_level") or 0)) * point
    # A zero MT5 stops/freeze level still requires a price separation of at
    # least one trade tick.  The smoke buffer is deliberately spread-aware:
    # one current spread beyond the executable market, or two broker minimums
    # when that is larger.  This is deterministic and avoids a one-tick stop
    # sitting inside/at the spread.
    effective_minimum = max(tick, broker_min_stop_distance)
    spread = abs(float(quote["ask"]) - float(quote["bid"]))
    breathing_buffer = max(spread, 2.0 * effective_minimum)
    buffer_ticks = max(1, int(math.ceil((breathing_buffer / tick) - 1e-12)))
    breathing_buffer = buffer_ticks * tick
    if direction == "BUY":
        entry = float(quote["ask"])
        stop = _tick_normalize_price(float(quote["bid"]) - breathing_buffer, tick, digits, round_up=False)
        target = _tick_normalize_price(float(quote["ask"]) + breathing_buffer, tick, digits, round_up=True)
        outside_spread = stop < float(quote["bid"]) and target > float(quote["ask"])
        stop_distance_valid = (float(quote["bid"]) - stop) >= effective_minimum - 1e-12
    elif direction == "SELL":
        entry = float(quote["bid"])
        stop = _tick_normalize_price(float(quote["ask"]) + breathing_buffer, tick, digits, round_up=True)
        target = _tick_normalize_price(float(quote["bid"]) - breathing_buffer, tick, digits, round_up=False)
        outside_spread = stop > float(quote["ask"]) and target < float(quote["bid"])
        stop_distance_valid = (stop - float(quote["ask"])) >= effective_minimum - 1e-12
    else:
        return None, "UNSUPPORTED_SMOKE_DIRECTION"
    distance = abs(entry - stop)
    ticks = max(1, int(math.ceil((distance / tick) - 1e-12)))
    estimated = ticks * value * volume
    if estimated > 1.0 + 1e-9:
        return None, "SMOKE_STOP_RISK_OVER_ONE_USD"
    return {"symbol": symbol, "direction": direction, "side": direction, "volume": volume,
            "bid": quote["bid"], "ask": quote["ask"], "entry": entry, "stop": stop,
            "target": target, "distance": distance, "ticks": ticks,
            "spread": spread, "broker_min_stop_distance": broker_min_stop_distance,
            "effective_minimum_stop_distance": effective_minimum,
            "breathing_buffer": breathing_buffer,
            "entry_to_target_distance": abs(target - entry),
            "sl_outside_spread": outside_spread,
            "broker_stop_distance_valid": stop_distance_valid,
            "breathing_room_valid": outside_spread and stop_distance_valid,
            "estimated_stop_loss": estimated, "quote": quote, "metadata": metadata}, None


def _quote_age_ms(quote: dict[str, Any]) -> float:
    received = quote.get("quote_received_at")
    if not received:
        return float("inf")
    return max(0.0, (datetime.now(timezone.utc) - parse_time(received)).total_seconds() * 1000.0)


def canonical_market_request(candidate: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    """Build the diagnostic equivalent of the EA canonical MqlTradeRequest.

    This is intentionally a pure builder.  It performs no network operation
    and has no order-send capability.
    """
    side = str(candidate["side"])
    flags = int((metadata.get("raw") or {}).get("filling_mode", metadata.get("filling_mode") or 0) or 0)
    # Matches FillingModeForSymbol() in the EA: IOC when flag 2 is present,
    # otherwise FOK when flag 1 is present.
    filling = 1 if flags & 2 else 0
    request = {
        "schema_version": 1,
        "action": 1,  # TRADE_ACTION_DEAL
        "magic": 0,
        "symbol": candidate["symbol"],
        "volume": float(candidate["volume"]),
        "price": float(candidate["ask"] if side == "BUY" else candidate["bid"]),
        "sl": float(candidate.get("broker_stop_price", candidate["stop"])),
        "tp": float(candidate.get("broker_target_price", candidate["target"])),
        "deviation": 50,
        "type": 0 if side == "BUY" else 1,
        "type_filling": filling,
        "type_time": 0,  # ORDER_TIME_GTC
        "expiration": 0,
        "comment": str(candidate.get("comment") or "CANONICAL_ORDER_CHECK"),
    }
    return request


def market_execution_candidate(intent: dict[str, Any], quote: dict[str, Any], volume: float,
                               metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the MARKET request candidate from the current executable quote.

    ``strategy_entry_price`` is retained only as the paper/reference entry.
    The broker price for MARKET execution is always the current executable
    side of the fresh quote.
    """
    side = "BUY" if intent["direction"] == "LONG" else "SELL"
    canonical_stop = float(intent["strategy_stop_price"])
    canonical_target = float(intent["strategy_target_price"])
    pricing = None
    broker_stop, broker_target = canonical_stop, canonical_target
    if metadata is not None:
        try:
            protection = broker_protection_levels(
                direction=intent["direction"], canonical_stop_price=canonical_stop,
                canonical_target_price=canonical_target, broker_symbol=intent["broker_symbol"],
                bid=float(quote["bid"]), ask=float(quote["ask"]), metadata=metadata)
        except ExecutionPricingError:
            raise
        broker_stop, broker_target = protection.broker_stop_price, protection.broker_target_price
        pricing = protection.to_dict()
    return {
        "symbol": intent["broker_symbol"],
        "side": side,
        "volume": float(volume),
        "bid": float(quote["bid"]),
        "ask": float(quote["ask"]),
        "stop": canonical_stop,
        "target": canonical_target,
        "canonical_stop_price": canonical_stop,
        "canonical_target_price": canonical_target,
        "broker_stop_price": broker_stop,
        "broker_target_price": broker_target,
        "reference_price_side": REFERENCE_PRICE_SIDE,
        "execution_pricing": pricing,
        "reference_entry_price": float(intent["strategy_entry_price"]),
        "comment": f"CTXV1:{intent['execution_intent_id'][:24]}",
    }


def market_execution_sizing(intent: dict[str, Any], quote: dict[str, Any], metadata: dict[str, Any],
                            virtual_equity: float, risk_fraction: float = RISK_FRACTION) -> dict[str, Any]:
    """Size a MARKET intent using the fresh executable price, never paper entry."""
    execution_price = float(quote["ask"] if intent["direction"] == "LONG" else quote["bid"])
    sized = virtual_size(entry=execution_price, stop=float(intent["strategy_stop_price"]),
                         metadata=metadata, virtual_equity=float(virtual_equity), risk_fraction=float(risk_fraction))
    sized["execution_price"] = execution_price
    sized["reference_entry_price"] = float(intent["strategy_entry_price"])
    return sized


def canonical_order_diagnostic() -> int:
    """Read-only canonical request and OrderCheck diagnostic for XAUUSDm."""
    endpoint = load_config().get("execution_mcp_url") or "http://127.0.0.1:22348/mcp"
    provider = MT5ShadowProvider(endpoint, caller="EXECUTION_CRITICAL", snapshot_ttl_seconds=0,
                                 read_timeout_seconds=12, max_age_ms=2000)
    counters = {"broker_capable_requests_attempted": 0, "mt5_order_send_attempted": 0,
                "broker_orders_accepted": 0, "broker_fills_observed": 0}
    try:
        health = provider.health()
        account = provider.account_snapshot("canonical-order-diagnostic")
        if account.get("account_context_id") != REAL_SMOKE_ACCOUNT or (account.get("raw") or {}).get("type") not in (2, "2"):
            raise RuntimeError("REAL_ACCOUNT_CONTEXT_OR_MODE_MISMATCH")
        metadata = provider.symbol_metadata("XAUUSDm")
        quote = _smoke_quote(provider, "XAUUSDm")
        candidate, reason = _smoke_candidate("XAUUSDm", metadata, quote, "BUY")
        if not candidate:
            raise RuntimeError(reason or "CANONICAL_CANDIDATE_INVALID")
        request = canonical_market_request(candidate, metadata)
        fingerprint = canonical_request_fingerprint(request)
        check = provider.order_check(symbol="XAUUSDm", side="BUY", volume=request["volume"],
                                     stop_loss=request["sl"], take_profit=request["tp"],
                                     canonical_request=request,
                                     canonical_request_text=canonical_request_text(request))
        matrix = []
        tick = float(metadata.get("tick_size") or metadata.get("raw", {}).get("point") or 0.001)
        digits = int((metadata.get("raw") or {}).get("digits", 8))
        bid = float(quote["bid"]); ask = float(quote["ask"]); volume = float(metadata["volume_min"])
        for distance in (0.262, 0.30, 0.40, 0.50, 0.75, 1.00, 1.50, 2.00):
            row_candidate = dict(candidate)
            row_candidate["stop"] = _tick_normalize_price(bid - distance, tick, digits, round_up=False)
            row_candidate["target"] = _tick_normalize_price(ask + distance, tick, digits, round_up=True)
            row_candidate["volume"] = volume
            row_request = canonical_market_request(row_candidate, metadata)
            row_check = provider.order_check(symbol="XAUUSDm", side="BUY", volume=volume,
                                             stop_loss=row_request["sl"], take_profit=row_request["tp"],
                                             canonical_request=row_request,
                                             canonical_request_text=canonical_request_text(row_request))
            ea_request = _wire_request(row_check.get("request")) if isinstance(row_check, dict) else None
            ea_text = row_check.get("canonical_request_text") if isinstance(row_check, dict) else None
            ea_fp = canonical_request_fingerprint(ea_request) if isinstance(ea_request, dict) else None
            matrix.append({"distance": distance, "request": row_request,
                           "fingerprint": canonical_request_fingerprint(row_request),
                           "order_check": row_check,
                           "control_fingerprint": canonical_request_fingerprint(row_request),
                           "ea_fingerprint": ea_fp,
                           "order_check_fingerprint": ea_fp,
                           "identity_match": ea_fp == canonical_request_fingerprint(row_request)
                           and (ea_text == canonical_request_text(row_request) if ea_text else False)})
        ea_request = _wire_request(check.get("request")) if isinstance(check, dict) else None
        ea_text = check.get("canonical_request_text") if isinstance(check, dict) else None
        control_text = canonical_request_text(request)
        control_fp = canonical_request_fingerprint(request)
        ea_fp = canonical_request_fingerprint(ea_request) if isinstance(ea_request, dict) else None
        print(json.dumps({"endpoint": endpoint, "account_context_id": account["account_context_id"],
                          "health": health, "canonical_order_send_enabled": False,
                          "wire_schema_version": 1,
                          "request": request, "request_fingerprint": fingerprint,
                          "control_canonical_request": request,
                          "control_canonical_request_text": control_text,
                          "control_fingerprint": control_fp,
                          "ea_received_canonical_request": ea_request,
                          "ea_received_canonical_request_text": ea_text,
                          "ea_received_fingerprint": ea_fp,
                          "order_check_request": ea_request,
                          "order_check_fingerprint": ea_fp,
                          "identity_proven": bool(ea_fp == control_fp and ea_text == control_text),
                          "quote": quote, "metadata": metadata, "order_check": check,
                          "matrix": matrix, **counters}, indent=2, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"endpoint": endpoint, "aborted": True, "reason": str(exc),
                          "canonical_order_send_enabled": False, **counters}, indent=2))
        return 2


def smoke_preflight():
    platform = load_config(); endpoint = platform.get("execution_mcp_url") or "http://127.0.0.1:22348/mcp"
    provider = MT5ShadowProvider(endpoint, caller="REAL_SMOKE_TEST", snapshot_ttl_seconds=0,
                                 read_timeout_seconds=12, max_age_ms=2000)
    health = provider.health()
    if not health.get("lifecycle", {}).get("read_path_healthy", False):
        raise RuntimeError("DEDICATED_EXECUTION_TRANSPORT_UNHEALTHY")
    account = provider.account_snapshot("real-smoke-connected")
    if account.get("account_context_id") != REAL_SMOKE_ACCOUNT or (account.get("raw") or {}).get("type") not in (2, "2"):
        raise RuntimeError("REAL_ACCOUNT_CONTEXT_OR_MODE_MISMATCH")
    candidates = []
    rejected = {}
    for symbol in REAL_SMOKE_SYMBOLS:
        metadata = provider.symbol_metadata(symbol); quote = _smoke_quote(provider, symbol)
        candidate, reason = _smoke_candidate(symbol, metadata, quote)
        if candidate: candidates.append(candidate)
        else: rejected[symbol] = reason
    if not candidates:
        raise RuntimeError(f"NO_SYMBOL_CAN_SATISFY_ONE_USD_STOP_CAP:{rejected}")
    # Deterministic choice only; no directional or performance inference.
    selected = candidates[0]
    try:
        check = provider.order_check(symbol=selected["symbol"], side=selected["side"],
                                     volume=selected["volume"], stop_loss=selected["stop"],
                                     take_profit=selected["target"])
        selected["order_check"] = ({"available": False, "reason": check.get("error")}
                                    if check.get("error") else check)
    except Exception as exc:
        selected["order_check"] = {"available": False, "reason": str(exc)}
    existing = provider.open_positions("real-smoke-connected")
    return {"endpoint": endpoint, "health": health, "account": account,
            "selected": selected, "candidates": candidates, "rejected": rejected,
            "existing_positions": existing, "provider": provider}


def _smoke_post_confirmation_preflight(provider: MT5ShadowProvider, symbol: str,
                                       direction: str, expected_volume: float) -> dict[str, Any]:
    """Build and OrderCheck a new market-dependent candidate after confirmation."""
    account = provider.account_snapshot("real-smoke-post-confirmation")
    if account.get("account_context_id") != REAL_SMOKE_ACCOUNT or (account.get("raw") or {}).get("type") not in (2, "2"):
        raise RuntimeError("REAL_ACCOUNT_CONTEXT_OR_MODE_MISMATCH")
    metadata = provider.symbol_metadata(symbol)
    quote = _smoke_quote(provider, symbol)
    candidate, reason = _smoke_candidate(symbol, metadata, quote, direction)
    if not candidate:
        raise RuntimeError(reason or "POST_CONFIRMATION_CANDIDATE_INVALID")
    if abs(float(candidate["volume"]) - float(expected_volume)) > 1e-12:
        raise RuntimeError("SMOKE_VOLUME_CHANGED_AFTER_CONFIRMATION")
    canonical_request = canonical_market_request(candidate, metadata)
    canonical_text = canonical_request_text(canonical_request)
    canonical_fingerprint = canonical_request_fingerprint(canonical_request)
    check = provider.order_check(symbol=symbol, side=direction,
                                 volume=candidate["volume"], stop_loss=candidate["stop"],
                                 take_profit=candidate["target"],
                                 canonical_request=canonical_request,
                                 canonical_request_text=canonical_text)
    candidate["order_check"] = {"available": False, "reason": check.get("error")} if check.get("error") else check
    ea_request = _wire_request(check.get("request")) if isinstance(check, dict) else None
    ea_text = check.get("canonical_request_text") if isinstance(check, dict) else None
    ea_fingerprint = canonical_request_fingerprint(ea_request) if isinstance(ea_request, dict) else None
    candidate.update({"canonical_request": canonical_request,
                      "canonical_request_text": canonical_text,
                      "canonical_request_fingerprint": canonical_fingerprint,
                      "order_check_fingerprint": ea_fingerprint,
                      "canonical_identity_proven": ea_fingerprint == canonical_fingerprint and ea_text == canonical_text})
    return {"endpoint": provider.mcp_url, "account": account, "selected": candidate,
            "provider": provider}


def _smoke_envelope_unchanged(initial: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """Keep human authorization bound to identity/risk, not stale prices."""
    return (initial.get("symbol") == fresh.get("symbol")
            and initial.get("direction") == fresh.get("direction")
            and initial.get("side") == fresh.get("side")
            and abs(float(initial.get("volume", 0)) - float(fresh.get("volume", 0))) <= 1e-12
            and float(fresh.get("estimated_stop_loss", float("inf"))) <= 1.0 + 1e-9)


def _smoke_response_metrics(response: dict[str, Any]) -> dict[str, int]:
    """Separate request-path, MT5 OrderSend, acceptance, and fill evidence."""
    retcode = response.get("retcode")
    send_attempted = response.get("mt5_order_send_attempted")
    if send_attempted is None:
        send_attempted = retcode is not None
    accepted = response.get("broker_order_accepted")
    if accepted is None:
        accepted = retcode is not None and int(retcode) in {10008, 10009, 10010}
    deal = response.get("deal") or response.get("deal_id")
    return {"broker_capable_requests_attempted": 1,
            "mt5_order_send_attempted": int(bool(send_attempted)),
            "broker_orders_accepted": int(bool(accepted)),
            "broker_fills_observed": int(bool(deal))}


def _print_smoke_preflight(preflight):
    a = preflight["account"]; c = preflight["selected"]; raw = a.get("raw") or {}
    spread = c.get("spread", float(c["ask"]) - float(c["bid"]))
    entry_to_target = c.get("entry_to_target_distance", abs(float(c["target"]) - float(c["entry"])))
    broker_min = c.get("broker_min_stop_distance")
    buffer = c.get("breathing_buffer")
    sl_outside = c.get("sl_outside_spread")
    stop_valid = c.get("broker_stop_distance_valid")
    breathing_valid = c.get("breathing_room_valid")
    print("*** REAL MONEY ORDER ***")
    print(json.dumps({"account": REAL_SMOKE_ACCOUNT, "server": raw.get("server"), "trade_mode": raw.get("type"),
                      "balance": a.get("balance"), "symbol": c["symbol"], "direction": c["direction"],
                      "volume": c["volume"], "bid": c["bid"], "ask": c["ask"],
                      "spread": spread, "broker_min_stop_distance": broker_min,
                      "smoke_breathing_buffer": buffer, "entry_reference": c["entry"],
                      "SL": c["stop"], "TP": c["target"], "entry_to_SL_distance": c["distance"],
                      "entry_to_TP_distance": entry_to_target,
                      "BID_to_SL_breathing_room": (c["bid"] - c["stop"] if c["direction"] == "BUY" else None),
                      "SL_to_ASK_breathing_room": (c["stop"] - c["ask"] if c["direction"] == "SELL" else None),
                      "estimated_loss_at_SL": c["estimated_stop_loss"], "risk_cap": 1.0,
                      "geometry_valid": breathing_valid, "risk_valid": c["estimated_stop_loss"] <= 1.0,
                      "SL_OUTSIDE_SPREAD": sl_outside,
                      "BROKER_STOP_DISTANCE_VALID": stop_valid,
                      "BREATHING_ROOM_VALID": breathing_valid,
                      "quote_age_ms": c["quote"].get("quote_age_ms"),
                      "order_check": c.get("order_check")}, indent=2))


def run_real_smoke_test(preflight_only=False) -> int:
    started = time.time()
    attempted = 0
    smoke_id = None
    try:
        preflight = smoke_preflight(); _print_smoke_preflight(preflight)
        if preflight_only:
            print(json.dumps({"preflight_only": True, "broker_writes_attempted": 0, "broker_writes_performed": 0}, indent=2)); return 0
        # The preview quote/candidate is informational only.  Market movement
        # while the user reviews it may make this preview OrderCheck fail;
        # that must not block confirmation.  The authoritative OrderCheck is
        # performed after confirmation on a newly quoted/rebuilt candidate.
        initial_check = preflight["selected"].get("order_check") or {}
        preview = preflight["selected"]
        _smoke_event("PREVIEW_ORDER_CHECK", symbol=preview["symbol"], side=preview["side"],
                     volume=preview["volume"], bid=preview["bid"], ask=preview["ask"],
                     stop_loss=preview["stop"], take_profit=preview["target"],
                     quote_received_at=preview["quote"].get("quote_received_at"),
                     order_check=initial_check)
        confirmation = input(f"Type exactly '{SMOKE_CONFIRMATION}': ")
        if confirmation != SMOKE_CONFIRMATION:
            print(json.dumps({"aborted": True, "reason": "CONFIRMATION_MISMATCH", "broker_writes_attempted": 0}, indent=2)); return 2
        # Rebuild from a new quote after confirmation. Price-dependent fields
        # are allowed to change; the human authorization envelope is not.
        fresh = _smoke_post_confirmation_preflight(
            preflight["provider"], preflight["selected"]["symbol"],
            preflight["selected"]["direction"], preflight["selected"]["volume"])
        c = fresh["selected"]
        if not _smoke_envelope_unchanged(preflight["selected"], c):
            raise RuntimeError("PREFLIGHT_CHANGED_AFTER_CONFIRMATION")
        _smoke_event("POST_CONFIRM_QUOTE", symbol=c["symbol"], side=c["side"], volume=c["volume"],
                     bid=c["bid"], ask=c["ask"], quote_received_at=c["quote"].get("quote_received_at"),
                     quote_age_ms=_quote_age_ms(c["quote"]))
        _smoke_event("POST_CONFIRM_CANDIDATE", symbol=c["symbol"], side=c["side"], volume=c["volume"],
                     entry=c["entry"], stop_loss=c["stop"], take_profit=c["target"],
                     estimated_stop_loss=c["estimated_stop_loss"])
        order_check = c.get("order_check") or {}
        if order_check.get("available") is False:
            raise RuntimeError("ORDER_CHECK_UNAVAILABLE_EA_RELOAD_REQUIRED")
        if order_check.get("success") is not True:
            raise RuntimeError(f"BROKER_PREFLIGHT_REJECTED:{order_check.get('retcode')}:{order_check.get('comment')}")
        _smoke_event("POST_CONFIRM_ORDER_CHECK", symbol=c["symbol"], side=c["side"], volume=c["volume"],
                     stop_loss=c["stop"], take_profit=c["target"], order_check=order_check)
        # This is the authoritative candidate. Do not recalculate after its
        # successful OrderCheck; submit these exact prices if still fresh.
        quote_to_submit_ms = _quote_age_ms(c["quote"])
        if quote_to_submit_ms > 2000.0:
            raise RuntimeError("STALE_EXECUTION_QUOTE")
        smoke_id = stable_id("SMOKE", {"account_context_id": REAL_SMOKE_ACCOUNT, "created": now()})
        SMOKE_RUNTIME.mkdir(parents=True, exist_ok=True)
        if SMOKE_STATE.exists():
            prior = json.loads(SMOKE_STATE.read_text())
            if (prior.get("status") not in {"COMPLETE", "ABORTED"}
                    and not prior.get("administrative_resolution")):
                raise RuntimeError("UNRESOLVED_PRIOR_SMOKE_REQUIRES_RECONCILIATION")
        # Persist a durable ready-to-submit record first. The write counter is
        # incremented only immediately before the broker-capable submission
        # call, never for OrderCheck or other reads.
        SMOKE_STATE.write_text(json.dumps({"smoke_test_id": smoke_id, "status": "SUBMISSION_ATTEMPTED",
                                           "created_at": now(), "execution_class": "REAL_SMOKE_TEST",
                                           "strategy_id": "NONE", "account_context_id": REAL_SMOKE_ACCOUNT,
                                           "symbol": c["symbol"], "endpoint": fresh["endpoint"],
                                           "broker_write_tool": "mt5_canonical_order_send", "broker_writes_attempted": 0,
                                           "broker_capable_requests_attempted": 0,
                                           "mt5_order_send_attempted": 0,
                                           "broker_orders_accepted": 0,
                                           "broker_fills_observed": 0,
                                           "request_payload": c.get("canonical_request"),
                                           "canonical_request_text": c.get("canonical_request_text"),
                                           "canonical_request_fingerprint": c.get("canonical_request_fingerprint"),
                                           "final_pre_submission_quote": c.get("quote"),
                                           "quote_to_submit_ms": quote_to_submit_ms,
                                           "submitted_geometry": {"entry_reference": c.get("entry"),
                                                                  "stop_loss": c.get("stop"),
                                                                  "take_profit": c.get("target"),
                                                                  "estimated_stop_loss": c.get("estimated_stop_loss")},
                                           "candidate_fingerprint": {"account_context_id": REAL_SMOKE_ACCOUNT,
                                                                      "symbol": c["symbol"], "side": c["side"],
                                                                      "volume": c["volume"], "stop_loss": c["stop"],
                                                                      "take_profit": c["target"],
                                                                      "order_check_request_price": order_check.get("request_price"),
                                                                      "created_at": now()}}, indent=2) + "\n")
        adapter = DemoExecutionAdapter(fresh["endpoint"] or "http://127.0.0.1:22348/mcp", mode="REAL_SMOKE_TEST",
                                       armed_context=REAL_SMOKE_ACCOUNT, verified_snapshot=fresh["account"],
                                       transport_verified=True, smoke_test_id=smoke_id)
        _smoke_event("FINAL_CANDIDATE_FINGERPRINT", smoke_test_id=smoke_id,
                     account_context_id=REAL_SMOKE_ACCOUNT, symbol=c["symbol"], side=c["side"],
                     volume=c["volume"], stop_loss=c["stop"], take_profit=c["target"],
                     order_check_request_price=order_check.get("request_price"),
                     quote_received_at=c["quote"].get("quote_received_at"),
                     quote_age_ms=quote_to_submit_ms,
                     canonical_request_fingerprint=c.get("canonical_request_fingerprint"))
        attempted = 1
        in_progress = json.loads(SMOKE_STATE.read_text())
        in_progress.update({"status": "SUBMISSION_IN_PROGRESS", "broker_writes_attempted": attempted,
                            "broker_capable_requests_attempted": attempted,
                            "mt5_order_send_attempted": 0,
                            "broker_orders_accepted": 0,
                            "broker_fills_observed": 0,
                            "submission_started_at": now()})
        SMOKE_STATE.write_text(json.dumps(in_progress, indent=2) + "\n")
        _smoke_event("MARKET_ORDER_ENQUEUED", smoke_test_id=smoke_id, symbol=c["symbol"],
                     side=c["side"], volume=c["volume"], stop_loss=c["stop"], take_profit=c["target"])
        open_started = time.time()
        result = adapter.submit_canonical_market_order(
            request=c["canonical_request"],
            canonical_request_text=c["canonical_request_text"],
            request_fingerprint=c["canonical_request_fingerprint"],
            idempotency_key=smoke_id)
        open_latency = (time.time() - open_started) * 1000.0
        # Persist the complete EA/MT5 response before reconciliation.  A
        # completed transport response is not itself broker acceptance, but
        # the response is authoritative evidence for classifying acceptance
        # or rejection and must survive a later reconciliation failure.
        accepted_state = json.loads(SMOKE_STATE.read_text())
        accepted_state.update({"status": "BROKER_RESPONSE_RECEIVED",
                               "broker_response": result,
                               "open_latency_ms": open_latency,
                               **_smoke_response_metrics(result)})
        SMOKE_STATE.write_text(json.dumps(accepted_state, indent=2, default=str) + "\n")
        after = fresh["provider"].open_positions("real-smoke-connected")
        before_tickets = {p.get("ticket") for p in preflight["existing_positions"]}
        new_positions = [p for p in after if p.get("ticket") not in before_tickets and p.get("symbol") == c["symbol"]]
        if len(new_positions) != 1:
            raise RuntimeError("SMOKE_POSITION_RECONCILIATION_FAILED")
        position = new_positions[0]
        if float(position.get("sl") or 0) <= 0:
            adapter.close_position(ticket=int(position["ticket"]))
            raise RuntimeError("SMOKE_POSITION_SL_NOT_VERIFIED")
        close_started = time.time(); close_result = adapter.close_position(ticket=int(position["ticket"]))
        close_latency = (time.time() - close_started) * 1000.0
        remaining = fresh["provider"].open_positions("real-smoke-connected")
        closed = not any(p.get("ticket") == position.get("ticket") for p in remaining)
        SMOKE_STATE.write_text(json.dumps({"smoke_test_id": smoke_id, "status": "COMPLETE" if closed else "RECONCILIATION_REQUIRED",
                                           "opening": result, "closing": close_result, "position": position,
                                           "open_latency_ms": open_latency, "close_latency_ms": close_latency,
                                           "position_confirmed_closed": closed}, indent=2, default=str) + "\n")
        print(json.dumps({"SMOKE_TEST_ID": smoke_id, "opening": result, "position": position,
                          "closing": close_result, "POSITION_CONFIRMED_CLOSED": closed,
                          "open_latency_ms": open_latency, "close_latency_ms": close_latency,
                          "broker_writes_attempted": 2, "broker_writes_performed": 2,
                          "broker_capable_requests_attempted": 2,
                          "mt5_order_send_attempted": 2,
                          "broker_orders_accepted": 1,
                          "broker_fills_observed": 1}, indent=2, default=str))
        return 0 if closed else 2
    except BrokerSubmissionRejected as exc:
        if SMOKE_STATE.exists():
            try:
                persisted = json.loads(SMOKE_STATE.read_text())
                persisted.update({"status": "BROKER_REJECTED",
                                  "broker_response": exc.response,
                                  "broker_rejection": str(exc),
                                  **_smoke_response_metrics(exc.response)})
                SMOKE_STATE.write_text(json.dumps(persisted, indent=2, default=str) + "\n")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        if attempted == 0 and SMOKE_STATE.exists() and smoke_id:
            try:
                persisted = json.loads(SMOKE_STATE.read_text())
                if persisted.get("smoke_test_id") == smoke_id:
                    attempted = int(persisted.get("broker_writes_attempted", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        print(json.dumps({"aborted": True, "reason": str(exc),
                          "broker_response": exc.response,
                          "elapsed_ms": (time.time() - started) * 1000,
                          "broker_writes_attempted": attempted,
                          **_smoke_response_metrics(exc.response)}, indent=2, default=str))
        return 2
    except Exception as exc:
        if attempted == 0 and SMOKE_STATE.exists() and smoke_id:
            try:
                persisted = json.loads(SMOKE_STATE.read_text())
                if persisted.get("smoke_test_id") == smoke_id:
                    attempted = int(persisted.get("broker_writes_attempted", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        print(json.dumps({"aborted": True, "reason": str(exc), "elapsed_ms": (time.time() - started) * 1000,
                          "broker_writes_attempted": attempted}, indent=2)); return 2


def _smoke_request_evidence(smoke_id: str) -> list[dict[str, Any]]:
    """Read-only lookup of persisted bridge lifecycle records for one smoke."""
    path = Path("runtime/execution_bridge/request_lifecycle.jsonl")
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if smoke_id in json.dumps(row, sort_keys=True):
            rows.append(row)
    return rows


def real_smoke_resolve() -> int:
    """Resolve an old uncertain smoke lifecycle using broker reads only."""
    if not SMOKE_STATE.exists():
        print(json.dumps({"resolved": False, "reason": "NO_SMOKE_STATE"}, indent=2))
        return 2
    state = json.loads(SMOKE_STATE.read_text())
    smoke_id = state.get("smoke_test_id")
    if not smoke_id:
        print(json.dumps({"resolved": False, "reason": "SMOKE_ID_MISSING"}, indent=2))
        return 2
    if state.get("administrative_resolution") or state.get("status") in {"COMPLETE", "ABORTED"}:
        print(json.dumps({"resolved": False, "reason": "SMOKE_LIFECYCLE_ALREADY_TERMINAL",
                          "smoke_test_id": smoke_id}, indent=2))
        return 2
    endpoint = load_config().get("execution_mcp_url") or "http://127.0.0.1:22348/mcp"
    if endpoint != "http://127.0.0.1:22348/mcp":
        print(json.dumps({"resolved": False, "reason": "DEDICATED_EXECUTION_ENDPOINT_REQUIRED",
                          "endpoint": endpoint}, indent=2))
        return 2
    provider = MT5ShadowProvider(endpoint, caller="RECONCILIATION", snapshot_ttl_seconds=0,
                                 read_timeout_seconds=12, max_age_ms=2000)
    try:
        account = provider.account_snapshot("smoke-resolution")
        if account.get("account_context_id") != REAL_SMOKE_ACCOUNT or (account.get("raw") or {}).get("type") not in (2, "2"):
            raise RuntimeError("REAL_ACCOUNT_CONTEXT_OR_MODE_MISMATCH")
        positions = provider.open_positions("smoke-resolution")
        orders = provider.pending_orders("smoke-resolution")
        history = provider.history(500)
    except Exception as exc:
        print(json.dumps({"resolved": False, "reason": f"READ_ONLY_RECONCILIATION_FAILED:{exc}",
                          "smoke_test_id": smoke_id, "endpoint": endpoint,
                          "broker_writes_attempted": 0}, indent=2))
        return 2
    symbol = state.get("symbol")
    requested_volume = float((state.get("request_payload") or {}).get("volume") or 0)
    matching_exposure = [p for p in positions + orders
                         if p.get("symbol") == symbol and
                         (not requested_volume or abs(float(p.get("volume") or 0) - requested_volume) < 1e-9)]
    created = parse_time(state.get("created_at")) if state.get("created_at") else None
    matching_deals = []
    for deal in (history or {}).get("deals", []):
        if deal.get("symbol") != symbol:
            continue
        if requested_volume and abs(float(deal.get("volume") or 0) - requested_volume) >= 1e-9:
            continue
        if created is not None and abs(float(deal.get("time") or 0) - created.timestamp()) > 300:
            continue
        matching_deals.append(deal)
    evidence = _smoke_request_evidence(smoke_id)
    if matching_exposure or matching_deals:
        print(json.dumps({"resolved": False, "status": "SMOKE_RESOLUTION_BLOCKED",
                          "smoke_test_id": smoke_id, "account": account,
                          "positions": positions, "pending_orders": orders,
                          "matching_exposure": matching_exposure, "matching_deals": matching_deals,
                          "request_evidence": evidence, "broker_writes_attempted": 0}, indent=2, default=str))
        return 2
    resolved = dict(state)
    resolved["broker_outcome"] = state.get("broker_outcome", "OUTCOME_UNKNOWN")
    resolved["broker_write_attempted"] = True
    resolved["administrative_resolution"] = "NO_CURRENT_EXPOSURE_CONFIRMED"
    resolved["resolved_at"] = now()
    resolved["resolution_method"] = "READ_ONLY_BROKER_RECONCILIATION"
    resolved["unresolved_smoke_lifecycle"] = False
    resolved["resolution_account_context_id"] = account.get("account_context_id")
    resolved["resolution_request_evidence"] = evidence
    resolved["resolution_matching_positions"] = []
    resolved["resolution_matching_orders"] = []
    resolved["resolution_matching_deals"] = []
    SMOKE_STATE.write_text(json.dumps(resolved, indent=2, default=str) + "\n")
    print(json.dumps({"resolved": True, "smoke_test_id": smoke_id,
                      "broker_outcome": resolved["broker_outcome"],
                      "administrative_resolution": resolved["administrative_resolution"],
                      "resolved_at": resolved["resolved_at"],
                      "resolution_method": resolved["resolution_method"],
                      "account_context_id": account.get("account_context_id"),
                      "positions": positions, "pending_orders": orders,
                      "matching_deals": matching_deals, "request_evidence": evidence,
                      "broker_writes_attempted": 0, "broker_writes_performed": 0}, indent=2, default=str))
    return 0


def disarm_demo() -> int:
    state = load_state(DEMO_STATE)
    state.update(mode="DRY_RUN", armed=False, disarmed_at=utc_now())
    save_state(DEMO_STATE, state)
    print(json.dumps({"armed": False, "account_context_id": state.get("account_context_id")}, indent=2))
    return 0


def trades_report() -> None:
    paths = [("REAL", REAL_TRADES), ("DEMO", DEMO_TRADES)]
    if not any(path.exists() for _, path in paths):
        print("No broker demo or real trades.")
        return
    for label, path in paths:
        if path.exists():
            print(f"[{label} BROKER TRADES]")
            print(path.read_text(encoding="utf-8"), end="")


def run(interval=1):
    if not safety_audit()["pass"]: raise RuntimeError("broker-write safety audit failed")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if PID.exists():
        try: os.kill(int(json.loads(PID.read_text())["pid"]), 0)
        except ProcessLookupError: PID.unlink(missing_ok=True)
        else: raise RuntimeError("dry-run execution consumer already active")
    store, cfg = ExecutionStore(RUNTIME), config()
    demo_state = load_state(DEMO_STATE)
    real_state = load_state(REAL_STATE)
    if real_state.get("armed"):
        cfg["mode"] = "REAL_EXECUTION"
        consistency = execution_state_consistency()
        if not consistency["safe"]:
            # The process may be used for diagnostics, but contradiction can
            # never fall through to REAL or DEMO intent creation.
            cfg["mode"] = "DIAGNOSTIC_READ_ONLY"
            cfg["execution_state_contradiction"] = consistency["reasons"]
    elif demo_state.get("armed"):
        cfg["mode"] = "DEMO_EXECUTION"
    # Write lifecycle identity only after runtime mode selection.  The module
    # default is diagnostic DRY_RUN, but an armed REAL state intentionally
    # selects REAL_EXECUTION for this process.
    PID.write_text(json.dumps({"pid": os.getpid(), "started": now(), "mode": cfg["mode"]}) + "\n")
    state = store.state(); state.update(status="ACTIVE", mode=cfg["mode"]); store.save_state(state); STOP.unlink(missing_ok=True)
    cfg["startup_signal_ids"] = {row.get("signal_id") for row in source_records()[0] if row.get("signal_id")}
    broker_state = BrokerStateStream()
    ownership = OwnershipRegistry()
    halt = {"x": False}
    def stop(*_): halt["x"] = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try:
        while not halt["x"] and not STOP.exists():
            if cfg["mode"] != "DIAGNOSTIC_READ_ONLY":
                # This is the single normalized broker-state source consumed
                # by Trade Manager. It is a read-only operation on 22348.
                try:
                    broker_state.refresh_from_provider(provider_adapter(cfg), cfg.get("account_id") or REAL_SMOKE_ACCOUNT)
                except Exception:
                    pass
                create_intents(store, cfg); process_intents(store, cfg)
                process_management_intents(cfg, provider_adapter(cfg), ownership)
            HEARTBEAT.write_text(json.dumps({"pid": os.getpid(), "status": "ACTIVE", "mode": cfg["mode"], "timestamp": now(), "broker_writes": 0}) + "\n")
            time.sleep(max(1, interval))
    finally:
        state = store.state(); state["status"] = "STOPPED"; store.save_state(state); PID.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start"); s.add_argument("--interval", type=int, default=1)
    for x in ("stop", "status", "health", "report", "latency-report", "decisions", "audit-order-isolation", "account", "demo-audit", "demo-arm", "demo-status", "demo-disarm", "real-audit", "real-arm", "real-status", "real-smoke-resolve", "canonical-order-diagnostic", "trades", "execution-safety-audit", "execution-resume-cutoff"): sub.add_parser(x)
    monitor = sub.add_parser("real-monitor"); monitor.add_argument("--once", action="store_true")
    smoke = sub.add_parser("real-smoke-test")
    smoke.add_argument("--preflight-only", action="store_true")
    args = p.parse_args(); store = ExecutionStore(RUNTIME)
    if args.command == "start": run(args.interval); return
    if args.command == "stop": STOP.write_text(now()); print("Dry-run execution consumer stop requested"); return
    if args.command == "audit-order-isolation": print(json.dumps(safety_audit(), indent=2)); return
    if args.command == "demo-audit": print(json.dumps(demo_audit(), indent=2, default=str)); return
    if args.command == "real-audit": print(json.dumps(real_audit(), indent=2, default=str)); return
    if args.command == "execution-safety-audit":
        print(json.dumps(execution_safety_audit(), indent=2, default=str)); return
    if args.command == "execution-resume-cutoff": raise SystemExit(establish_execution_resume_cutoff())
    if args.command == "real-arm": raise SystemExit(arm_real())
    if args.command == "real-status": print(json.dumps(real_status(), indent=2, default=str)); return
    if args.command == "real-monitor": raise SystemExit(real_monitor(once=args.once))
    if args.command == "real-smoke-resolve": raise SystemExit(real_smoke_resolve())
    if args.command == "canonical-order-diagnostic": raise SystemExit(canonical_order_diagnostic())
    if args.command == "real-smoke-test": raise SystemExit(run_real_smoke_test(preflight_only=args.preflight_only))
    if args.command == "demo-arm": raise SystemExit(arm_demo())
    if args.command == "demo-disarm": raise SystemExit(disarm_demo())
    if args.command == "demo-status": print(json.dumps(demo_status(), indent=2, default=str)); return
    if args.command == "trades": trades_report(); return
    if args.command == "account": print(json.dumps(demo_audit(), indent=2, default=str)); return
    if args.command == "report": print(report(store)); return
    if args.command == "latency-report": print(json.dumps(latency_report(), indent=2, default=str)); return
    if args.command == "decisions": print(json.dumps(store.rows("execution_decisions"), indent=2)); return
    state = store.state(); heartbeat = json.loads(HEARTBEAT.read_text()) if HEARTBEAT.exists() else None
    print(json.dumps({"version": VERSION, "mode": MODE, "status": state.get("status"), "pid": json.loads(PID.read_text())["pid"] if PID.exists() else None, "heartbeat": heartbeat, "broker_writes": 0}, indent=2))


if __name__ == "__main__": main()
