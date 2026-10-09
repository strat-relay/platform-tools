#!/usr/bin/env python3
"""Multi-strategy, multi-account signal orchestration.

The orchestrator owns canonicalization, routing, and sizing disposition. It
never owns broker execution; the execution consumer owns that boundary.
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

from orchestration.adapters.context_structure_retrace import ContextStructureRetraceAdapter
from orchestration.canonical_signal_publisher import CanonicalSignalPublisher
from orchestration.adapters.liquidity_displacement import LiquidityDisplacementAdapter
from orchestration.brokers.mt5_shadow import READ_ONLY_BRIDGE_TOOLS, MT5ShadowProvider
from orchestration.config import load_config, refresh_lifecycle
from orchestration.liquidity_instances import DEFINITIONS_BY_ID, DEFINITIONS_BY_INSTANCE_ID, LiquidityInstanceAdapter
from orchestration.models import AccountSnapshot, StrategySignal, stable_id
from orchestration.registry import PortfolioRegistry, StrategyRegistry
from orchestration.risk import RiskSizingEngine
from orchestration.storage import OrchestrationStore
from migration.flags import (ExecutionAuthorityMode, SignalAuthorityFlags,
                             SignalAuthorityMode, execution_authority_mode_from_env)
from postgres.config import PostgresConfig
from postgres.db import connect
from trade_manager.central import authorize_pending_proposals
from orchestration.tradeability import evaluate as evaluate_tradeability, load_policy
from orchestration.replay_guard import EPOCH_PATH, eligibility, load_epoch, records_by_strategy
from platform_runtime import trading_platform_runtime_dir
from observability.strategy_audit import audit, configure_strategy_audit_logging
from execution_v2.trace import emit as trace_emit

ROOT = Path(__file__).resolve().parent
PLATFORM_RUNTIME = trading_platform_runtime_dir(root=ROOT)
RUNTIME = PLATFORM_RUNTIME / "orchestration"
MANIFEST = RUNTIME / "manifest.json"
HEARTBEAT = RUNTIME / "heartbeat.json"
PID = RUNTIME / "pid"
STOP = Path("/tmp/signal-orchestrator-shadow.stop")
REAL_STOP = Path("/tmp/signal-orchestrator-real.stop")
PRIMARY_STOP = Path("/tmp/signal-orchestrator-primary.stop")
VERSION = "SIGNAL_ORCHESTRATOR_SHADOW_V1"
LIVE_OUTPUT_VERSION = "SIGNAL_ORCHESTRATOR_LIVE_OUTPUT_V1"
SCHEMA = "signal-orchestration-v1"
# Real account binding is deployment configuration, never source data.
REAL_CONTEXT = os.environ.get("REAL_ACCOUNT_CONTEXT")
EXECUTION_ENDPOINT = "http://10.10.10.100:22348/mcp"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_hash() -> str:
    files = [Path(__file__), *Path(ROOT / "orchestration").rglob("*.py")]
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(str(path.relative_to(ROOT)).encode()); h.update(path.read_bytes())
    return h.hexdigest()


def config_hash(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def classify_source_timestamp(source_timestamp: str | None, freeze_timestamp: str) -> str:
    return ("PROSPECTIVE_ORCHESTRATOR_SIGNAL" if source_timestamp and source_timestamp >= freeze_timestamp
            else "PRE_ORCHESTRATOR_REFERENCE")


def live_classification(signal: dict[str, Any], state: dict[str, Any]) -> str:
    """Classify live output using receipt and source-event provenance."""
    if not state.get("live_execution_enabled"):
        return "PRE_LIVE_EXECUTION"
    cutoff = state.get("live_execution_cutoff_timestamp")
    if signal.get("signal_id") in set(state.get("live_execution_cutoff_signal_ids", [])):
        return "PRE_LIVE_EXECUTION"
    # Receipt time alone is insufficient: a late discovery of an old strategy
    # event must remain permanently outside the REAL stream.
    if (not cutoff or not signal.get("created_at") or signal["created_at"] <= cutoff or
            not signal.get("signal_timestamp") or signal["signal_timestamp"] <= cutoff):
        return "PRE_LIVE_EXECUTION"
    return "POST_LIVE_EXECUTION"


def canonical_schema_hash() -> str:
    return hashlib.sha256(json.dumps({"model": "StrategySignal", "schema": "strategy-signal-v1",
                                      "immutable": True}, sort_keys=True).encode()).hexdigest()


def atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def identity() -> dict[str, Any]:
    state = json.loads((ROOT / "context_structure_retrace_forward_manifest.json").read_text())
    return {"phase6_source": state.get("code_hash"), "phase6_config": state.get("configuration_hash"),
            "phase6_decision_fingerprint": "6dda2523e15edbc0e2d123878367f21ffaec70219272aa409193c2fc45b7c9bc",
            "phase2_hash": state.get("phase2_representation_hash"),
            "phase7_manifest": str(ROOT / "context_structure_retrace_phase7_manifest.json")}


def manifest(config: dict[str, Any]) -> dict[str, Any]:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text())
    value = {"version": VERSION, "schema": SCHEMA, "freeze_timestamp": now(),
             "source_hash": source_hash(), "configuration_hash": config_hash(config),
             "canonical_signal_schema_hash": canonical_schema_hash(), "mode": "SHADOW",
             "phase6_identity": identity(), "allowed_bridge_tools": sorted(READ_ONLY_BRIDGE_TOOLS),
             "live_execution_enabled": False, "enabled_strategy_ids": ["CONTEXT_STRUCTURE_RETRACE_V1"]}
    atomic(MANIFEST, value)
    return value


def manifest_for_orchestration_mode(value: dict[str, Any], orchestration_mode: str,
                                    canonical_publisher: CanonicalSignalPublisher | None) -> dict[str, Any]:
    """Use the authority cutoff as PRIMARY's discovery boundary, not SHADOW's old freeze."""
    if orchestration_mode != "PRIMARY":
        return value
    if canonical_publisher is None:
        raise RuntimeError("PRIMARY requires CanonicalSignalPublisher for its discovery boundary")
    cutoff_utc = canonical_publisher.cutoff_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    # Keep the persisted historical manifest immutable. This in-memory copy
    # prevents pre-T0 strategy state from being rediscovered as new signals.
    return {**value, "freeze_timestamp": cutoff_utc}


def event(store: OrchestrationStore, event_type: str, signal: StrategySignal | None = None,
          payload: dict[str, Any] | None = None, correlation_id: str | None = None, causation_id: str | None = None) -> None:
    row = {"event_id": stable_id("EVT", {"type": event_type, "signal": signal.signal_id if signal else None,
                                             "payload": payload or {}}), "event_type": event_type, "timestamp": now(),
           "signal_id": signal.signal_id if signal else None, "strategy_id": signal.strategy_id if signal else None,
           "strategy_version": signal.strategy_version if signal else None, "correlation_id": correlation_id,
           "causation_id": causation_id, "payload": payload or {}}
    store.append("events", row, row["event_id"])


def load_adapters(config: dict[str, Any], freeze_timestamp: str) -> list[Any]:
    registry = StrategyRegistry(config)
    adapters = []
    for record in registry.enabled():
        if record["strategy_id"] == "CONTEXT_STRUCTURE_RETRACE_V1":
            instances = [x for x in config.get("instances", [])
                         if x.get("strategy_id") == record["strategy_id"] and x.get("enabled")]
            # Context currently has one audited instance. Pass its live policy into
            # the adapter; the adapter applies policy only to newly discovered signals.
            if instances:
                adapters.append(ContextStructureRetraceAdapter(ROOT, freeze_timestamp, instances[0]))
            else:
                adapters.append(ContextStructureRetraceAdapter(ROOT, freeze_timestamp))
        elif record["strategy_id"] == "CONTEXT_STRUCTURE_RETRACE_V2":
            instances = [x for x in config.get("instances", [])
                         if x.get("strategy_id") == record["strategy_id"] and x.get("enabled")]
            for instance in instances:
                adapters.append(ContextStructureRetraceAdapter(
                    ROOT, freeze_timestamp, instance,
                    strategy_id="CONTEXT_STRUCTURE_RETRACE_V2", strategy_version="V2",
                    state_dir_env="CONTEXT_V2_RUNNER_STATE_DIR"))
        elif record["strategy_id"] == "LIQUIDITY_DISPLACEMENT_SCALP_V1":
            # The parent owns the adapter family.  Enabled children are persisted as
            # strategy instances and each keeps an isolated state/dedupe namespace.
            family = [x for x in config.get("instances", []) if x.get("strategy_id") == record["strategy_id"]]
            instances = [x for x in family if x.get("enabled")]
            if instances:
                for instance in instances:
                    definition = DEFINITIONS_BY_INSTANCE_ID.get(instance["instance_id"])
                    if definition is None:
                        raise RuntimeError(f"enabled strategy instance is not audited: {instance['instance_id']}")
                    adapters.append(LiquidityInstanceAdapter(ROOT, definition))
            elif not family:
                # Preserve the existing parent-only forward-paper behavior while a
                # deployment is being migrated to explicit instance rows.
                adapters.append(LiquidityDisplacementAdapter(ROOT, freeze_timestamp))
            # Instance rows exist but every one is OFFLINE: no Liquidity adapter at all.
        elif record["strategy_id"] == "KOJO_STRUCTURE_RECLAIM_V1":
            # Pipeline-created instances from strategy_instance_v2.
            # Only ONLINE instances are present in config["instances"] (loaded by
            # _load_v2_instances_from_database).  If none are online, no adapter is created.
            from orchestration.adapters.kojo_structure_reclaim_adapter import KojoStructureReclaimAdapter
            instances = [x for x in config.get("instances", [])
                         if x.get("strategy_id") == record["strategy_id"] and x.get("enabled")]
            for inst in instances:
                adapters.append(KojoStructureReclaimAdapter(
                    instance_id=inst["instance_id"],
                    display_name=inst.get("display_name", inst["instance_id"]),
                    instruments=inst.get("active_instruments", []),
                ))
        elif record["strategy_id"] in DEFINITIONS_BY_ID:
            # Compatibility for pre-instance database rows; new configuration should
            # register the parent strategy plus child instance rows instead.
            adapters.append(LiquidityInstanceAdapter(ROOT, DEFINITIONS_BY_ID[record["strategy_id"]]))
        elif record.get("enabled"):
            raise RuntimeError(f"enabled strategy adapter is not audited: {record['strategy_id']}")
    return adapters


def route_signal(store: OrchestrationStore, signal: StrategySignal, config: dict[str, Any], provider: MT5ShadowProvider | None,
                 orchestration_mode: str = "SHADOW") -> None:
    audit("signal_routing_started", runner="signal-orchestrator", signal_id=signal.signal_id,
          strategy_id=signal.strategy_id, strategy_instance_id=signal.strategy_instance_id,
          canonical_instrument=signal.canonical_symbol, provider_symbol=signal.broker_symbol_hint,
          direction=signal.direction, orchestration_mode=orchestration_mode)
    registry = PortfolioRegistry(config)
    created = now()
    for route_type, destination in (("AUDIT", "audit-store"), ("DISTRIBUTION", "internal-queue")):
        rid = stable_id("ROUTE", {"signal": signal.signal_id, "type": route_type, "destination": destination})
        store.append("route_decisions", {"route_decision_id": rid, "signal_id": signal.signal_id, "route_type": route_type,
            "destination_id": destination, "status": "QUEUED" if route_type == "DISTRIBUTION" else "ACCEPTED",
            "reason": "ROUTE_ENABLED", "created_at": created}, rid)
    store.append("distribution_queue", {"distribution_intent_id": stable_id("DIST", {"signal": signal.signal_id}),
        "signal_id": signal.signal_id, "strategy_id": signal.strategy_id, "strategy_version": signal.strategy_version,
        "channel": "INTERNAL_QUEUE", "audience_id": "internal-shadow", "created_at": created,
        "status": "QUEUED", "attempt_count": 0, "last_error": None}, signal.signal_id)
    live_state = store.load_state()
    if orchestration_mode != "PRIMARY" and live_classification(signal.to_dict(), live_state) == "POST_LIVE_EXECUTION":
        live_route_id = stable_id("ROUTE", {"signal": signal.signal_id, "type": "REAL_EXECUTION_OUTPUT"})
        store.append("route_decisions", {"route_decision_id": live_route_id, "signal_id": signal.signal_id,
            "route_type": "REAL_EXECUTION_OUTPUT", "destination_id": "real-execution-consumer",
            "status": "QUEUED", "reason": "POST_LIVE_EXECUTION_SIGNAL", "created_at": created}, live_route_id)
        event(store, "LIVE_OUTPUT_PUBLISHED", signal, {"destination": "real-execution-consumer",
                                                         "classification": "POST_LIVE_EXECUTION"})
    for portfolio, account in registry.routes_for(signal.strategy_id):
        if orchestration_mode == "PRIMARY":
            route_type = "EXECUTION_DISABLED"
            rid = stable_id("ROUTE", {"signal": signal.signal_id, "type": route_type,
                                       "destination": portfolio["portfolio_id"] + account["account_id"]})
            store.append("route_decisions", {"route_decision_id": rid, "signal_id": signal.signal_id,
                "route_type": route_type, "destination_id": portfolio["portfolio_id"],
                "account_id": account["account_id"], "status": "DISABLED",
                "reason": "EXECUTION_AUTHORITY_DISABLED", "created_at": created}, rid)
            event(store, "EXECUTION_ROUTE_DISABLED", signal,
                  {"account_id": account["account_id"], "reason": "EXECUTION_AUTHORITY_DISABLED"})
            continue
        account_mode = account.get("execution_mode")
        account_allowed = account_mode == orchestration_mode
        route_type = "SHADOW_EXECUTION" if orchestration_mode == "SHADOW" else "REAL_EXECUTION_DISPOSITION"
        rid = stable_id("ROUTE", {"signal": signal.signal_id, "type": route_type, "destination": portfolio["portfolio_id"] + account["account_id"]})
        destination = portfolio["portfolio_id"]
        store.append("route_decisions", {"route_decision_id": rid, "signal_id": signal.signal_id, "route_type": route_type,
            "destination_id": destination, "account_id": account["account_id"],
            "status": "QUEUED" if account_allowed else "REJECTED",
            "reason": ("SHADOW_MODE" if orchestration_mode == "SHADOW" else "REAL_EXECUTION_MODE") if account_allowed else
                      ("LIVE_EXECUTION_DISABLED" if orchestration_mode == "SHADOW" else "REAL_ACCOUNT_REQUIRED"),
            "created_at": created}, rid)
        if not account_allowed:
            event_type = "LIVE_EXECUTION_BLOCKED" if orchestration_mode == "SHADOW" else "REAL_EXECUTION_ACCOUNT_BLOCKED"
            event(store, event_type, signal, {"account_id": account["account_id"], "reason": "ACCOUNT_MODE_MISMATCH"})
            continue
        try:
            if provider is None:
                raise RuntimeError("broker read provider unavailable for execution-capable orchestration mode")
            raw = provider.account_snapshot(account["account_id"])
            orchestration_state = store.load_state()
            prior_context = orchestration_state.get("last_account_context_id")
            if prior_context and prior_context != raw.get("account_context_id"):
                event(store, "ACCOUNT_CONTEXT_CHANGED", signal, {
                    "account_id": account["account_id"],
                    "previous_account_context_id": prior_context,
                    "account_context_id": raw.get("account_context_id"),
                    "reason": "broker_account_identity_changed_cache_invalidated",
                })
            orchestration_state["last_account_context_id"] = raw.get("account_context_id")
            store.save_state(orchestration_state)
            snapshot_id = stable_id("SNAP", {"account": account["account_id"], "timestamp": raw["timestamp"]})
            raw["snapshot_id"] = snapshot_id
            store.append("account_snapshots", raw, snapshot_id)
            snapshot = AccountSnapshot(snapshot_id=snapshot_id, account_id=account["account_id"], timestamp=raw["timestamp"],
                balance=raw.get("balance"), equity=raw.get("equity"), margin=raw.get("margin"), free_margin=raw.get("free_margin"),
                margin_level=raw.get("margin_level"), currency=raw.get("currency"), leverage=raw.get("leverage"))
            metadata = provider.symbol_metadata(signal.broker_symbol_hint)
            quote = provider.quote(signal.broker_symbol_hint)
            trade_policy = load_policy(ROOT / "orchestration")
            tradeability = evaluate_tradeability(signal.to_dict(), quote, metadata,
                policy_min_rr=float(trade_policy["minimum_effective_rr"]))
            tradeability_row = {"signal_id": signal.signal_id, "strategy_id": signal.strategy_id,
                "symbol": signal.broker_symbol_hint, "direction": signal.direction,
                "created_at": now(), "policy_id": trade_policy["policy_id"],
                "policy_version": trade_policy["policy_version"],
                "signal_rr": tradeability.original_rr,
                "minimum_effective_rr": trade_policy["minimum_effective_rr"],
                **tradeability.to_dict()}
            store.append("tradeability_decisions", tradeability_row,
                         stable_id("TRADEABILITY", {"signal_id": signal.signal_id, "account": account["account_id"]}))
            audit("tradeability_decision", runner="signal-orchestrator", signal_id=signal.signal_id,
                  strategy_id=signal.strategy_id, account_id=account["account_id"],
                  decision=tradeability.status, reason=tradeability.rejection_reason,
                  symbol=signal.broker_symbol_hint)
            if orchestration_mode == "REAL_EXECUTION" and tradeability.status != "TRADEABLE":
                for fraction in config.get("sizing_scenarios", []):
                    rejected = {"signal_id": signal.signal_id, "account_id": account["account_id"],
                        "portfolio_id": portfolio["portfolio_id"], "strategy_id": signal.strategy_id,
                        "strategy_version": signal.strategy_version, "desired_risk_fraction": fraction,
                        "entry": signal.entry_price, "stop": signal.stop_price, "target": signal.target_price,
                        "decision": "REJECTED", "reason": tradeability.rejection_reason,
                        "tradeability": tradeability.to_dict(), "created_at": now()}
                    store.append("sizing_decisions", rejected,
                                 stable_id("SIZE_REJECT", {"signal": signal.signal_id, "account": account["account_id"], "fraction": fraction}))
                event(store, "REAL_TRADEABILITY_BLOCKED", signal, tradeability.to_dict())
                continue
            for fraction in config.get("sizing_scenarios", []):
                decision = RiskSizingEngine().size(signal, portfolio["portfolio_id"], account["account_id"], snapshot.__dict__, metadata, float(fraction))
                decision_row = dict(decision.__dict__)
                if orchestration_mode == "REAL_EXECUTION" and decision.decision == "EXECUTABLE":
                    try:
                        check = provider.order_check(symbol=signal.broker_symbol_hint,
                            side="BUY" if signal.direction == "LONG" else "SELL",
                            volume=float(decision.rounded_volume), stop_loss=signal.stop_price,
                            take_profit=signal.target_price)
                        decision_row["order_check"] = check
                        if check.get("success") is not True or check.get("retcode") not in (None, 0):
                            decision_row["decision"] = "REJECTED"
                            decision_row["reason"] = "ORDER_CHECK_REJECTED"
                    except Exception as exc:
                        decision_row["decision"] = "REJECTED"
                        decision_row["reason"] = "ORDER_CHECK_UNAVAILABLE"
                        decision_row["order_check"] = {"success": False, "error": str(exc)}
                store.append("sizing_decisions", decision_row, decision.sizing_decision_id)
                audit("sizing_decision", runner="signal-orchestrator", signal_id=signal.signal_id,
                      strategy_id=signal.strategy_id, account_id=account["account_id"],
                      decision=decision_row["decision"], reason=decision_row.get("reason"),
                      volume=decision_row.get("rounded_volume"), risk_fraction=fraction)
                event(store, "SIZING_" + ("EXECUTABLE" if decision_row["decision"] == "EXECUTABLE" else "REJECTED"), signal,
                      {"account_id": account["account_id"], "portfolio_id": portfolio["portfolio_id"], "risk_fraction": fraction, "reason": decision_row["reason"]})
        except Exception as exc:
            reason = "BRIDGE_QUEUE_BACKLOG" if "BRIDGE_QUEUE_BACKLOG" in str(exc) else "MISSING_ACCOUNT_DATA"
            store.append("sizing_decisions", {"signal_id": signal.signal_id, "account_id": account["account_id"],
                "portfolio_id": portfolio["portfolio_id"], "decision": "SKIPPED", "reason": reason,
                "error": str(exc), "created_at": now()}, stable_id("SKIP", {"signal": signal.signal_id, "account": account["account_id"]}))
            event(store, "SIZING_SKIPPED", signal, {"account_id": account["account_id"], "reason": reason})
            audit("sizing_decision", runner="signal-orchestrator", signal_id=signal.signal_id,
                  strategy_id=signal.strategy_id, account_id=account["account_id"],
                  decision="SKIPPED", reason=reason, error=str(exc))


def poll_once(store: OrchestrationStore, config: dict[str, Any], mf: dict[str, Any],
              orchestration_mode: str = "SHADOW", *,
              signal_authority_mode: SignalAuthorityMode = SignalAuthorityMode.LEGACY_FILE,
              canonical_publisher: CanonicalSignalPublisher | None = None,
              provider: MT5ShadowProvider | None = None) -> int:
    if orchestration_mode == "PRIMARY" and signal_authority_mode is not SignalAuthorityMode.DB_PRIMARY:
        raise RuntimeError("PRIMARY requires DB_PRIMARY signal authority")
    if signal_authority_mode is SignalAuthorityMode.DB_PRIMARY and canonical_publisher is None:
        raise RuntimeError("DB_PRIMARY requires CanonicalSignalPublisher")
    state = store.load_state()
    seen = (canonical_publisher.existing_signal_ids() if signal_authority_mode is SignalAuthorityMode.DB_PRIMARY
            else set(state.get("processed_signal_ids", [])))
    total = 0
    startup_epoch = load_epoch(EPOCH_PATH)
    if orchestration_mode == "REAL_EXECUTION" and not startup_epoch:
        raise RuntimeError("STARTUP_MARKET_WATERMARK_MISSING")
    watermark_by_strategy = records_by_strategy(startup_epoch or {})
    audit("runner_cycle_started", runner="signal-orchestrator",
          orchestration_mode=orchestration_mode,
          signal_authority_mode=signal_authority_mode.value)
    orchestrator_boundary = mf["freeze_timestamp"]
    if signal_authority_mode is not SignalAuthorityMode.DB_PRIMARY:
        for raw in store.rows("signals"):
            source_ts = raw.get("signal_timestamp")
            classification = classify_source_timestamp(source_ts, orchestrator_boundary)
            correction_id = stable_id("CLASS", {"signal_id": raw["signal_id"], "classification": classification})
            store.append("classification_corrections", {"correction_id": correction_id, "signal_id": raw["signal_id"],
                "original_source_timestamp": source_ts, "orchestrator_freeze_timestamp": orchestrator_boundary,
                "corrected_classification": classification, "corrected_at": now(),
                "reason": "classification_uses_originating_strategy_event_timestamp"}, correction_id)
    if orchestration_mode != "PRIMARY" and provider is None:
        provider = MT5ShadowProvider(config["mcp_url"], caller="SIGNAL_ORCHESTRATOR")
    discovered = []
    adapters = load_adapters(config, orchestrator_boundary)
    audit("strategies_loaded", runner="signal-orchestrator",
          strategies=[getattr(adapter, "strategy_id", adapter.__class__.__name__) for adapter in adapters])
    for adapter in adapters:
        strategy_id = getattr(adapter, "strategy_id", adapter.__class__.__name__)
        before = len(discovered)
        try:
            discovered.extend(adapter.discover_new_signals(seen))
            audit("strategy_scan_completed", runner="signal-orchestrator", strategy_id=strategy_id,
                  decision="SIGNALS_FOUND" if len(discovered) > before else "NO_SIGNAL",
                  signal_count=len(discovered) - before)
        except Exception as exc:
            audit("strategy_scan_failed", runner="signal-orchestrator", strategy_id=strategy_id,
                  decision="ERROR", error=str(exc))
            raise
    known = (set(seen) if signal_authority_mode is SignalAuthorityMode.DB_PRIMARY else
             {row.get("signal_id") for row in store.rows("signals")})
    if signal_authority_mode is not SignalAuthorityMode.DB_PRIMARY:
        # Legacy recovery is intentionally unavailable in DB_PRIMARY. There,
        # PostgreSQL is the signal identity source and the relay recovers only
        # pending outbox rows; the JSONL tailer/file is never consulted.
        sizing_ids = {row.get("signal_id") for row in store.rows("sizing_decisions")}
        for row in store.rows("signals"):
            if row.get("signal_id") not in sizing_ids and row.get("signal_id") not in {x.signal_id for x in discovered}:
                # Older durable rows remain readable only in file-authority modes.
                discovered.append(StrategySignal(**{
                    k: row[k] for k in StrategySignal.__dataclass_fields__ if k in row
                }))
    for signal in discovered:
        trace_emit("SIGNAL_ORCHESTRATOR_RECEIVED", signal_id=signal.signal_id,
                   decision_time=signal.decision_time or signal.signal_timestamp,
                   signal_emitted_at=signal.signal_emitted_at, created_at=signal.created_at,
                   strategy_id=signal.strategy_id, strategy_instance_id=signal.strategy_instance_id,
                   instrument=signal.canonical_symbol, transport="ORCHESTRATOR")
        audit("signal_received", runner="signal-orchestrator", signal_id=signal.signal_id,
              strategy_id=signal.strategy_id, strategy_instance_id=signal.strategy_instance_id,
              canonical_instrument=signal.canonical_symbol, provider_symbol=signal.broker_symbol_hint,
              direction=signal.direction, signal_timestamp=signal.signal_timestamp,
              decision_time=signal.decision_time, entry=signal.entry_price,
              stop=signal.stop_price, target=signal.target_price)
        if signal_authority_mode is SignalAuthorityMode.DB_PRIMARY:
            assert canonical_publisher is not None
            _, inserted = canonical_publisher.publish(signal)
            trace_emit("SIGNAL_DB_PERSIST_RESULT", signal_id=signal.signal_id,
                       decision_time=signal.decision_time or signal.signal_timestamp,
                       signal_emitted_at=signal.signal_emitted_at, created_at=signal.created_at,
                       strategy_id=signal.strategy_id, transport="DB_PRIMARY",
                       outcome="INSERTED" if inserted else "DUPLICATE")
            audit("canonical_signal_decision", runner="signal-orchestrator", signal_id=signal.signal_id,
                  strategy_id=signal.strategy_id,
                  decision="ACCEPTED" if inserted else "DUPLICATE", inserted=inserted)
            if not inserted:
                seen.add(signal.signal_id)
                continue
        else:
            store.append("signals", signal.to_dict(), signal.signal_id)
        if signal.signal_id not in known and signal_authority_mode is not SignalAuthorityMode.DB_PRIMARY:
            event(store, "SIGNAL_CANONICALIZED", signal, {"identity_derivation": "stable_id_v1"})
        delivery_key = stable_id("DELIVERY", {"signal_id": signal.signal_id})
        try:
            watermark = watermark_by_strategy.get(signal.strategy_id)
            if orchestration_mode == "REAL_EXECUTION" and (not watermark or
                    not eligibility(signal.to_dict(), watermark)[0]):
                reason = "STARTUP_REPLAY_BLOCKED"
                classification = eligibility(signal.to_dict(), watermark)[1] if watermark else "UNKNOWN_EVENT_TIME"
                event(store, reason, signal, {"classification": classification,
                                              "startup_epoch_id": (startup_epoch or {}).get("startup_epoch_id"),
                                              "startup_market_watermark": (watermark or {}).get("startup_market_watermark")})
                store.append("delivery_status", {"signal_id": signal.signal_id,
                    "status": reason, "classification": classification, "timestamp": now()}, delivery_key)
                audit("signal_delivery_decision", runner="signal-orchestrator", signal_id=signal.signal_id,
                      strategy_id=signal.strategy_id, decision="BLOCKED", reason=reason,
                      classification=classification)
                seen.add(signal.signal_id)
                state["processed_signal_ids"] = sorted(seen)
                state["last_poll_at"] = now()
                store.save_state(state)
                total += 1
                continue
            store.append("delivery_status", {"signal_id": signal.signal_id, "status": "DELIVERY_ATTEMPTED", "timestamp": now()}, delivery_key + ":attempt")
            route_signal(store, signal, config, provider, orchestration_mode)
            trace_emit("SIGNAL_ORCHESTRATOR_ROUTED", signal_id=signal.signal_id,
                       decision_time=signal.decision_time or signal.signal_timestamp,
                       signal_emitted_at=signal.signal_emitted_at, created_at=signal.created_at,
                       strategy_id=signal.strategy_id, transport="ORCHESTRATOR", outcome="ROUTED")
            store.append("delivery_status", {"signal_id": signal.signal_id, "status": "DELIVERY_COMPLETE", "timestamp": now()}, delivery_key)
            audit("signal_delivery_decision", runner="signal-orchestrator", signal_id=signal.signal_id,
                  strategy_id=signal.strategy_id, decision="COMPLETE", orchestration_mode=orchestration_mode)
        except Exception as exc:
            event(store, "ROUTE_DEGRADED", signal, {"reason": str(exc)})
            store.append("delivery_status", {"signal_id": signal.signal_id, "status": "ROUTE_DEGRADED", "reason": str(exc), "timestamp": now()}, delivery_key)
            audit("signal_delivery_decision", runner="signal-orchestrator", signal_id=signal.signal_id,
                  strategy_id=signal.strategy_id, decision="DEGRADED", error=str(exc))
        # A signal is processed only after a durable downstream disposition.
        seen.add(signal.signal_id)
        state["processed_signal_ids"] = sorted(seen)
        state["last_poll_at"] = now()
        store.save_state(state)
        total += 1
    state["processed_signal_ids"] = sorted(seen); state["last_poll_at"] = now(); state["status"] = "ACTIVE"; store.save_state(state)
    atomic(HEARTBEAT, {"pid": os.getpid(), "status": "ACTIVE", "timestamp": now(), "signals_seen": len(seen)})
    audit("runner_cycle_completed", runner="signal-orchestrator", processed=total,
          signals_seen=len(seen), orchestration_mode=orchestration_mode)
    return total


def safety_audit() -> dict[str, Any]:
    paths = [Path(__file__), *Path(ROOT / "orchestration").rglob("*.py")]
    forbidden = {"mt5_market_order", "mt5_pending_order", "mt5_cancel_pending_order", "mt5_close_position", "mt5_trailing_stop", "order_send", "TRADE_ACTION_DEAL", "TRADE_ACTION_PENDING"}
    tools = []
    forbidden_calls = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "call_bridge" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                tools.append(node.args[1].value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden:
                forbidden_calls.append(node.func.id)
    bad = sorted(set(tools) - READ_ONLY_BRIDGE_TOOLS)
    read_only_declared = any("READ_ONLY_BRIDGE_TOOLS" in path.read_text(encoding="utf-8") for path in paths)
    state = OrchestrationStore(RUNTIME).load_state()
    return {"pass": not bad and not forbidden_calls and read_only_declared, "bridge_tools": sorted(set(tools)),
            "disallowed_bridge_tools": bad, "forbidden_calls": forbidden_calls,
            "live_execution_enabled": bool(state.get("live_execution_enabled", False))}


def startup_audit(config: dict[str, Any], orchestration_mode: str,
                  store: OrchestrationStore | None = None, *,
                  signal_authority_mode: SignalAuthorityMode | None = None,
                  execution_authority_mode: ExecutionAuthorityMode | None = None) -> dict[str, Any]:
    """Validate one explicit startup mode without changing any state."""
    store = store or OrchestrationStore(RUNTIME)
    state = store.load_state()
    audit = safety_audit()
    reasons: list[str] = []
    accounts = [a for a in config.get("accounts", []) if a.get("enabled")]
    if orchestration_mode not in {"SHADOW", "PRIMARY", "REAL_EXECUTION"}:
        reasons.append("UNKNOWN_ORCHESTRATION_MODE")
    configured_orchestrator_mode = os.environ.get("ORCHESTRATOR_MODE", "").strip().upper()
    if configured_orchestrator_mode and configured_orchestrator_mode != orchestration_mode:
        reasons.append("ORCHESTRATOR_MODE_CONFLICTS_WITH_STARTUP_MODE")
    if signal_authority_mode is None:
        try:
            signal_authority_mode = SignalAuthorityFlags.mode_from_env()
        except ValueError as exc:
            reasons.append(f"INVALID_SIGNAL_AUTHORITY:{exc}")
            signal_authority_mode = SignalAuthorityMode.LEGACY_FILE
    if execution_authority_mode is None:
        try:
            execution_authority_mode = execution_authority_mode_from_env(
                required=orchestration_mode in {"PRIMARY", "REAL_EXECUTION"})
        except ValueError as exc:
            reasons.append(f"INVALID_EXECUTION_AUTHORITY:{exc}")
            execution_authority_mode = ExecutionAuthorityMode.DISABLED
    if not audit["pass"]:
        reasons.append("ORDER_ISOLATION_AUDIT_FAILED")
    if orchestration_mode == "SHADOW":
        if signal_authority_mode is SignalAuthorityMode.DB_PRIMARY:
            reasons.append("SHADOW_CANNOT_USE_DB_PRIMARY_SIGNAL_AUTHORITY")
        if execution_authority_mode is not ExecutionAuthorityMode.DISABLED:
            reasons.append("SHADOW_REQUIRES_EXECUTION_DISABLED")
        if config.get("execution_mode") != "SHADOW":
            reasons.append("PLATFORM_MODE_NOT_SHADOW")
        if any(a.get("execution_mode") != "SHADOW" for a in accounts):
            reasons.append("ENABLED_ACCOUNT_NOT_SHADOW")
        if state.get("live_execution_enabled"):
            reasons.append("LIVE_EXECUTION_ENABLED")
    elif orchestration_mode == "PRIMARY":
        if os.environ.get("ORCHESTRATOR_MODE", "").strip().upper() != "PRIMARY":
            reasons.append("ORCHESTRATOR_MODE_MUST_EXPLICITLY_BE_PRIMARY")
        if signal_authority_mode is not SignalAuthorityMode.DB_PRIMARY:
            reasons.append("PRIMARY_REQUIRES_DB_PRIMARY_SIGNAL_AUTHORITY")
        if execution_authority_mode is not ExecutionAuthorityMode.DISABLED:
            reasons.append("PRIMARY_REQUIRES_EXECUTION_DISABLED")
        if state.get("live_execution_enabled"):
            reasons.append("LIVE_EXECUTION_ENABLED")
        try:
            PostgresConfig.from_env().require_explicit_target()
        except ValueError as exc:
            reasons.append(f"CANONICAL_POSTGRES_NOT_CONFIGURED:{exc}")
        if not os.environ.get("NATS_URL", "").strip():
            reasons.append("CANONICAL_NATS_NOT_CONFIGURED")
        if not os.environ.get("SIGNAL_CUTOFF_ID", "").strip():
            reasons.append("SIGNAL_CUTOFF_ID_MISSING")
        if not os.environ.get("SIGNAL_CUTOFF_UTC", "").strip():
            reasons.append("SIGNAL_CUTOFF_UTC_MISSING")
        else:
            try:
                cutoff = datetime.fromisoformat(os.environ["SIGNAL_CUTOFF_UTC"].replace("Z", "+00:00"))
                if cutoff.tzinfo is None:
                    reasons.append("SIGNAL_CUTOFF_UTC_MUST_INCLUDE_TIMEZONE")
            except (TypeError, ValueError):
                reasons.append("SIGNAL_CUTOFF_UTC_INVALID")
    elif orchestration_mode == "REAL_EXECUTION":
        if execution_authority_mode is not ExecutionAuthorityMode.ENABLED:
            reasons.append("REAL_EXECUTION_REQUIRES_EXECUTION_ENABLED")
        if config.get("execution_mode") != "REAL_EXECUTION":
            reasons.append("PLATFORM_MODE_NOT_REAL")
        if not accounts:
            reasons.append("NO_ENABLED_REAL_ACCOUNT")
        for account in accounts:
            if account.get("execution_mode") != "REAL_EXECUTION":
                reasons.append("ENABLED_ACCOUNT_NOT_REAL")
            if account.get("broker_environment") != "REAL":
                reasons.append("REAL_ACCOUNT_ENVIRONMENT_MISMATCH")
        if config.get("execution_mcp_url") != EXECUTION_ENDPOINT or not config.get("execution_transport_verified", False):
            reasons.append("DEDICATED_EXECUTION_TRANSPORT_NOT_VERIFIED")
        runtime = store.root
        manifest_state = json.loads((runtime / "manifest.json").read_text()) if (runtime / "manifest.json").exists() else {}
        real_state_path = runtime.parent / "execution" / "real_state.json"
        real_state = json.loads(real_state_path.read_text()) if real_state_path.exists() else {}
        resume_path = runtime.parent / "execution" / "real_execution_resume.json"
        resume = json.loads(resume_path.read_text()) if resume_path.exists() else {}
        if manifest_state.get("mode") != "REAL_EXECUTION":
            reasons.append("MANIFEST_MODE_NOT_REAL")
        if bool(manifest_state.get("live_execution_enabled")) != bool(state.get("live_execution_enabled")):
            reasons.append("MANIFEST_ORCHESTRATION_LIVE_FLAG_MISMATCH")
        if not state.get("live_execution_enabled"):
            reasons.append("LIVE_EXECUTION_NOT_ENABLED")
        if not real_state.get("armed") or real_state.get("mode") != "REAL_EXECUTION":
            reasons.append("REAL_CONSUMER_NOT_ARMED")
        if not real_state.get("account_context_id"):
            reasons.append("REAL_ACCOUNT_CONTEXT_MISSING")
        if not state.get("live_execution_cutoff_timestamp"):
            reasons.append("LIVE_CUTOFF_MISSING")
        if not resume.get("real_execution_resumed_at") or not resume.get("real_execution_resume_generation"):
            reasons.append("REAL_RESUME_CUTOFF_MISSING")
    return {"pass": not reasons, "mode": orchestration_mode, "reasons": reasons,
            "order_isolation": audit, "live_execution_enabled": bool(state.get("live_execution_enabled", False))}


def live_audit(store: OrchestrationStore, config: dict[str, Any]) -> dict[str, Any]:
    state = store.load_state()
    real_state_path = PLATFORM_RUNTIME / "execution" / "real_state.json"
    real_state = json.loads(real_state_path.read_text()) if real_state_path.exists() else {}
    signals = store.rows("signals")
    return {
        "version": LIVE_OUTPUT_VERSION,
        "live_output_enabled": bool(state.get("live_execution_enabled", False)),
        "live_execution_enabled_at": state.get("live_execution_enabled_at"),
        "live_execution_cutoff_timestamp": state.get("live_execution_cutoff_timestamp"),
        "live_execution_cutoff_signal_id": state.get("live_execution_cutoff_signal_id"),
        "pre_live_signal_count": len(state.get("live_execution_cutoff_signal_ids", [])),
        "canonical_signal_count": len(signals),
        "real_consumer_armed": bool(real_state.get("armed")) and real_state.get("account_context_id") == REAL_CONTEXT,
        "real_account_context": real_state.get("account_context_id"),
        "execution_endpoint": config.get("execution_mcp_url"),
        "execution_transport_verified": bool(config.get("execution_transport_verified", False)),
        "requires_endpoint": EXECUTION_ENDPOINT,
        "broker_writes_attempted": 0,
        "broker_writes_performed": 0,
    }


def live_enable(store: OrchestrationStore, config: dict[str, Any]) -> int:
    """Enable publication only; never arms or writes to a broker."""
    audit = live_audit(store, config)
    if audit["live_output_enabled"]:
        print(json.dumps({"enabled": True, "reason": "ALREADY_ENABLED", **audit}, indent=2))
        return 0
    if config.get("execution_mcp_url") != EXECUTION_ENDPOINT or not config.get("execution_transport_verified", False):
        print(json.dumps({"enabled": False, "reason": "DEDICATED_EXECUTION_TRANSPORT_NOT_VERIFIED", **audit}, indent=2))
        return 2
    if not audit["real_consumer_armed"]:
        print(json.dumps({"enabled": False, "reason": "REAL_CONSUMER_NOT_ARMED", **audit}, indent=2))
        return 2
    signals = store.rows("signals")
    cutoff = now()
    existing_ids = [x["signal_id"] for x in signals]
    state = store.load_state()
    state.update({"live_execution_enabled": True,
                  "live_execution_enabled_at": cutoff,
                  "live_execution_cutoff_timestamp": cutoff,
                  "live_execution_cutoff_signal_id": existing_ids[-1] if existing_ids else None,
                  "live_execution_cutoff_signal_ids": existing_ids,
                  "live_output_version": LIVE_OUTPUT_VERSION,
                  "live_output_status": "ENABLED"})
    store.save_state(state)
    print(json.dumps({"enabled": True, "version": LIVE_OUTPUT_VERSION,
                      "live_execution_enabled_at": cutoff,
                      "live_execution_cutoff_timestamp": cutoff,
                      "live_execution_cutoff_signal_id": state["live_execution_cutoff_signal_id"],
                      "pre_live_signal_count": len(existing_ids),
                      "broker_writes_attempted": 0, "broker_writes_performed": 0}, indent=2))
    return 0


def report(store: OrchestrationStore, config: dict[str, Any], mf: dict[str, Any]) -> str:
    signals = store.rows("signals"); decisions = store.rows("sizing_decisions"); routes = store.rows("route_decisions"); dist = store.rows("distribution_queue")
    corrections = {x["signal_id"]: x["corrected_classification"] for x in store.rows("classification_corrections")}
    prospective = [x for x in signals if corrections.get(x["signal_id"]) == "PROSPECTIVE_ORCHESTRATOR_SIGNAL"]
    mode = config.get("execution_mode", "SHADOW")
    lines = [f"SIGNAL ORCHESTRATOR — {mode}", "=" * 72, f"Status: {store.load_state().get('status')}", f"Freeze: {mf['freeze_timestamp']}", f"Canonical signals stored: {len(signals)}", f"Prospective signals: {len(prospective)}", f"Pre-freeze references: {len(signals)-len(prospective)}", "", "STRATEGIES"]
    for strategy in config.get("strategies", []):
        ss = [x for x in prospective if x["strategy_id"] == strategy["strategy_id"]]
        lines.append(f"{strategy['strategy_id']}: signals={len(ss)} canonicalized={len(ss)} distribution_queued={sum(x['signal_id'] in {d['signal_id'] for d in dist} for x in ss)}")
    lines += ["", "SIZING SCENARIOS"]
    for fraction in config.get("sizing_scenarios", []):
        rows = [x for x in decisions if x.get("desired_risk_fraction") == fraction]
        lines.append(f"{fraction:.2%}: N={len(rows)} executable={sum(x.get('decision') == 'EXECUTABLE' for x in rows)} below_minimum={sum(x.get('reason') == 'BELOW_MINIMUM_VOLUME' for x in rows)} rejected={sum(x.get('decision') == 'REJECTED' for x in rows)}")
    state = store.load_state()
    live_ids = {x["signal_id"] for x in signals if live_classification(x, state) == "POST_LIVE_EXECUTION"}
    prospective_ids = {x["signal_id"] for x in prospective}
    prospective_dist = [x for x in dist if x.get("signal_id") in prospective_ids]
    lines += ["", "DISTRIBUTION — PROSPECTIVE ONLY", f"queued={sum(x.get('status') == 'QUEUED' for x in prospective_dist)} delivered={sum(x.get('status') == 'DELIVERED' for x in prospective_dist)} failed={sum(x.get('status') == 'FAILED' for x in prospective_dist)}", f"stored including references={len(dist)}", "", f"LIVE OUTPUT: {'ENABLED' if state.get('live_execution_enabled') else 'DISABLED'}", f"LIVE OUTPUT VERSION: {state.get('live_output_version', VERSION)}", f"PRE_LIVE_EXECUTION: {len(signals) - len(live_ids)}", f"POST_LIVE_EXECUTION: {len(live_ids)}"]
    return "\n".join(lines)


def _broker_health(provider: MT5ShadowProvider) -> dict[str, Any]:
    """Read bridge health only; this never queues an MT5 request."""
    health = provider.health()
    lifecycle = health.get("lifecycle", {}) or {}
    return {
        "read_path_healthy": bool(lifecycle.get("read_path_healthy", health.get("read_path_healthy", False))),
        "transport_queue_depth": lifecycle.get("transport_queue_depth", health.get("pending")),
        "active_waiters": lifecycle.get("active_waiters"),
        "queued_active": lifecycle.get("queued_active"),
        "dispatched_active": lifecycle.get("dispatched_active"),
        "response_throughput": lifecycle.get("response_throughput"),
        "timeout_count": lifecycle.get("timeout_count"),
        "seconds_since_last_response": lifecycle.get("seconds_since_last_response"),
    }


def account_verification(config: dict[str, Any], store: OrchestrationStore | None = None) -> dict[str, Any]:
    """Perform a single read-only account verification through the real provider."""
    provider = MT5ShadowProvider(config["mcp_url"], caller="SIGNAL_ORCHESTRATOR_ACCOUNT_CLI")
    broker = _broker_health(provider)
    accounts = [x for x in config.get("accounts", []) if x.get("enabled")]
    if not accounts:
        return {"status": "UNAVAILABLE", "reason": "NO_ENABLED_ACCOUNT", "broker_read_path": broker}
    configured = accounts[0]
    try:
        snapshot = provider.account_snapshot(configured["account_id"])
    except Exception as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc), "broker_read_path": _broker_health(provider),
                "valid_for_sizing": False, "account_id": configured["account_id"]}
    retrieved = now()
    previous_context = None
    context_change_event = False
    if store is not None:
        prior = store.rows("account_snapshots")
        if prior:
            previous_context = prior[-1].get("account_context_id")
        context_change_event = any(x.get("event_type") == "ACCOUNT_CONTEXT_CHANGED" for x in store.rows("events"))
    context_id = snapshot.get("account_context_id")
    required = (snapshot.get("currency"), snapshot.get("equity"), snapshot.get("free_margin"), context_id)
    valid = bool(broker["read_path_healthy"] and all(x is not None for x in required)
                 and context_id not in (None, "UNKNOWN"))
    return {
        "status": "VERIFIED" if valid else "UNAVAILABLE",
        "broker_read_path": broker,
        "account_id": configured["account_id"],
        "broker": configured.get("broker"),
        "broker_environment": configured.get("broker_environment"),
        "server": snapshot.get("raw", {}).get("server") or snapshot.get("raw", {}).get("company"),
        "masked_login": (str(snapshot.get("raw", {}).get("login") or snapshot.get("raw", {}).get("account") or "")[:2] + "***"),
        "account_context_id": context_id,
        "previous_persisted_account_context_id": previous_context,
        "account_context_changed": bool(previous_context and previous_context != context_id),
        "account_context_changed_event_recorded": context_change_event,
        "currency": snapshot.get("currency"), "balance": snapshot.get("balance"),
        "equity": snapshot.get("equity"), "margin": snapshot.get("margin"),
        "free_margin": snapshot.get("free_margin"), "margin_level": snapshot.get("margin_level"),
        "leverage": snapshot.get("leverage"),
        "snapshot_timestamp": snapshot.get("timestamp"), "retrieval_timestamp": retrieved,
        "snapshot_age_seconds": snapshot.get("snapshot_age_seconds"),
        "cache_hit": snapshot.get("snapshot_cache_hit", False),
        "fresh": not snapshot.get("snapshot_cache_hit", False) and float(snapshot.get("snapshot_age_seconds", 0) or 0) <= provider.snapshot_ttl_seconds,
        "valid_for_sizing": valid,
        "previous_account_cache_reused": False,
        "historical_prospective_signals_resized": False,
    }


def operational_health(store: OrchestrationStore, config: dict[str, Any]) -> dict[str, Any]:
    verification = account_verification(config, store)
    broker = verification.get("broker_read_path", {})
    return {"broker_read_path": "HEALTHY" if broker.get("read_path_healthy") else "UNHEALTHY",
            "account_context": "VERIFIED" if verification.get("status") == "VERIFIED" else "UNVERIFIED",
            "account_snapshot": "FRESH" if verification.get("fresh") else ("STALE" if verification.get("status") != "VERIFIED" else "UNAVAILABLE"),
            "broker": broker,
            "account_reason": verification.get("reason"),
            "account_context_id": verification.get("account_context_id")}


def acquire_lock() -> None:
    if PID.exists():
        try: os.kill(int(json.loads(PID.read_text())["pid"]), 0)
        except ProcessLookupError: PID.unlink(missing_ok=True)
        else: raise RuntimeError("signal orchestrator already active")
    atomic(PID, {"pid": os.getpid(), "started": now(), "version": VERSION})


def run(args: argparse.Namespace, orchestration_mode: str) -> None:
    configured_orchestrator_mode = os.environ.get("ORCHESTRATOR_MODE", "").strip().upper()
    if orchestration_mode == "PRIMARY" and configured_orchestrator_mode != "PRIMARY":
        raise RuntimeError("primary-start requires ORCHESTRATOR_MODE=PRIMARY")
    if configured_orchestrator_mode and configured_orchestrator_mode != orchestration_mode:
        raise RuntimeError("ORCHESTRATOR_MODE conflicts with the selected startup command")
    config = load_config(); store = OrchestrationStore(RUNTIME)
    signal_authority_mode = SignalAuthorityFlags.mode_from_env()
    execution_authority_mode = execution_authority_mode_from_env(
        required=orchestration_mode in {"PRIMARY", "REAL_EXECUTION"})
    db_conn = None
    canonical_publisher = None
    if signal_authority_mode is SignalAuthorityMode.DB_PRIMARY:
        if not os.environ.get("NATS_URL"):
            raise RuntimeError("DB_PRIMARY requires configured NATS_URL for the independent outbox relay")
        db_config = PostgresConfig.from_env()
        db_config.require_explicit_target()
        db_conn = connect(db_config)
        try:
            canonical_publisher = CanonicalSignalPublisher(
                db_conn,
                cutoff_id=os.environ.get("SIGNAL_CUTOFF_ID", ""),
                cutoff_utc=os.environ.get("SIGNAL_CUTOFF_UTC", ""),
                source_id=os.environ.get("SIGNAL_SOURCE_ID", "signal-orchestrator"),
            )
            canonical_publisher.require_schema()
        except Exception:
            db_conn.close()
            raise
    try:
        audit = startup_audit(config, orchestration_mode, store,
                              signal_authority_mode=signal_authority_mode,
                              execution_authority_mode=execution_authority_mode)
        if not audit["pass"]:
            raise RuntimeError(f"{orchestration_mode.lower()} startup safety audit failed: {audit}")
        mf = manifest_for_orchestration_mode(manifest(config), orchestration_mode, canonical_publisher)
        stop_path = {"SHADOW": STOP, "PRIMARY": PRIMARY_STOP,
                     "REAL_EXECUTION": REAL_STOP}[orchestration_mode]
        acquire_lock(); state = store.load_state(); state["status"] = "ACTIVE"; store.save_state(state); stop_path.unlink(missing_ok=True)
    except Exception:
        if db_conn is not None:
            db_conn.close()
        raise
    halt = {"x": False}
    def handler(signum: int, frame: Any) -> None: halt["x"] = True
    signal.signal(signal.SIGINT, handler); signal.signal(signal.SIGTERM, handler)
    try:
        next_cycle = time.monotonic()
        while not halt["x"] and not stop_path.exists():
            cycle_started = time.monotonic()
            # Instance ONLINE/OFFLINE is re-read every cycle; the rest of the config is fixed at start.
            config = refresh_lifecycle(config)
            try: poll_once(store, config, mf, orchestration_mode,
                           signal_authority_mode=signal_authority_mode,
                           canonical_publisher=canonical_publisher,
                           provider=(None if orchestration_mode == "PRIMARY" else
                                     MT5ShadowProvider(config["mcp_url"], caller="SIGNAL_ORCHESTRATOR")))
            # Management proposals follow the same central authority as entry
            # signals. This creates only typed management intents; the REAL
            # consumer remains the sole broker-write boundary.
            except Exception as exc: atomic(HEARTBEAT, {"pid": os.getpid(), "status": "DEGRADED", "mode": orchestration_mode, "timestamp": now(), "error": str(exc)})
            else:
                # Keep the lifecycle health contract explicit so downstream
                # services cannot mistake a shadow heartbeat for REAL mode.
                signal_count = (len(canonical_publisher.existing_signal_ids())
                                if signal_authority_mode is SignalAuthorityMode.DB_PRIMARY and canonical_publisher
                                else len(store.rows("signals")))
                heartbeat = {"pid": os.getpid(), "status": "ACTIVE", "mode": orchestration_mode,
                             "signal_authority_mode": signal_authority_mode.value,
                             "timestamp": now(), "signals_seen": signal_count}
                atomic(HEARTBEAT, heartbeat)
            if orchestration_mode != "PRIMARY":
                try:
                    authorize_pending_proposals()
                except Exception as management_exc:
                    atomic(RUNTIME / "management_health.json", {"status": "DEGRADED", "error": str(management_exc), "timestamp": now()})
            # Schedule from the cycle start. Sleeping after work used to add
            # scan, persistence, and routing time to the configured interval.
            next_cycle = cycle_started + max(0.25, float(args.interval))
            delay = next_cycle - time.monotonic()
            if delay <= 0:
                delay = max(0.25, float(args.interval))
            audit("orchestrator_cycle_scheduled", runner="signal-orchestrator",
                  cycle_elapsed_ms=round((time.monotonic() - cycle_started) * 1000, 3),
                  configured_interval_ms=round(max(0.25, float(args.interval)) * 1000, 3),
                  next_cycle_delay_ms=round(delay * 1000, 3))
            time.sleep(delay)
    finally:
        state = store.load_state(); state["status"] = "STOPPED"; store.save_state(state); PID.unlink(missing_ok=True)
        if db_conn is not None:
            db_conn.close()


def main() -> None:
    configure_strategy_audit_logging()
    p = argparse.ArgumentParser(description="Signal orchestration with independent signal and execution authority")
    sub = p.add_subparsers(dest="command", required=True)
    start = sub.add_parser("shadow-start"); start.add_argument("--interval", type=float, default=1)
    primary_start = sub.add_parser("primary-start", help="start authoritative DB_PRIMARY orchestration with execution disabled")
    primary_start.add_argument("--interval", type=float, default=1)
    real_start = sub.add_parser("real-start"); real_start.add_argument("--interval", type=float, default=1)
    startup_audit_cmd = sub.add_parser("startup-audit")
    startup_audit_cmd.add_argument("--mode", choices=("SHADOW", "PRIMARY", "REAL_EXECUTION"))
    for name in ("shadow-stop", "primary-stop", "real-stop", "status", "health", "account", "report", "signals", "decisions", "distribution", "audit-order-isolation", "freeze", "live-audit", "live-enable", "live-status"):
        sub.add_parser(name)
    args = p.parse_args(); config = load_config(); store = OrchestrationStore(RUNTIME)
    if args.command == "freeze": print(json.dumps(manifest(config), indent=2)); return
    if args.command == "audit-order-isolation": print(json.dumps(safety_audit(), indent=2)); return
    if args.command == "report": print(report(store, config, manifest(config))); return
    if args.command == "live-audit": print(json.dumps(live_audit(store, config), indent=2, default=str)); return
    if args.command == "live-enable": raise SystemExit(live_enable(store, config))
    if args.command == "live-status":
        state = store.load_state()
        signals = store.rows("signals")
        print(json.dumps({**live_audit(store, config),
                          "post_live_signal_count": sum(live_classification(x, state) == "POST_LIVE_EXECUTION" for x in signals),
                          "live_output_status": state.get("live_output_status", "DISABLED")}, indent=2, default=str))
        return
    if args.command == "account": print(json.dumps(account_verification(config, store), indent=2, default=str)); return
    if args.command in ("signals", "decisions", "distribution"):
        rows = store.rows({"signals": "signals", "decisions": "sizing_decisions", "distribution": "distribution_queue"}[args.command])
        if args.command == "signals":
            corrections = {x["signal_id"]: x["corrected_classification"] for x in store.rows("classification_corrections")}
            rows = [dict(x, classification=corrections.get(x["signal_id"], "UNCLASSIFIED")) for x in rows]
        print(json.dumps(rows, indent=2)); return
    if args.command == "startup-audit":
        mode = args.mode or os.environ.get("ORCHESTRATOR_MODE", "SHADOW").strip().upper()
        print(json.dumps(startup_audit(config, mode), indent=2, default=str)); return
    if args.command == "shadow-stop": STOP.write_text(now()); print("Shadow orchestrator stop requested"); return
    if args.command == "primary-stop": PRIMARY_STOP.write_text(now()); print("Primary orchestrator stop requested"); return
    if args.command == "real-stop": REAL_STOP.write_text(now()); print("Real orchestrator stop requested"); return
    if args.command in ("status", "health"):
        state = store.load_state(); value = {"version": VERSION, "status": state.get("status"),
            "pid": json.loads(PID.read_text())["pid"] if PID.exists() else None,
            "heartbeat": json.loads(HEARTBEAT.read_text()) if HEARTBEAT.exists() else None,
            "live_execution_enabled": bool(store.load_state().get("live_execution_enabled", False)),
            "live_output_version": store.load_state().get("live_output_version", VERSION),
            "live_execution_enabled_at": store.load_state().get("live_execution_enabled_at"),
            "live_execution_cutoff_timestamp": store.load_state().get("live_execution_cutoff_timestamp")}
        if args.command == "health":
            value["broker"] = operational_health(store, config)
        print(json.dumps(value, indent=2, default=str)); return
    mode = {"shadow-start": "SHADOW", "primary-start": "PRIMARY",
            "real-start": "REAL_EXECUTION"}[args.command]
    run(args, mode)


if __name__ == "__main__": main()
