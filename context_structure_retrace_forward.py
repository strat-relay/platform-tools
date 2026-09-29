"""CONTEXT_STRUCTURE_RETRACE_V1 isolated paper-only forward collector.

This module is intentionally separate from every liquidity-displacement
runner.  It has a read-only MT5 bridge allow-list and no broker execution
path.  The frozen strategy is a causal context/structure representation;
all entries and exits here are simulated ledger events only.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import signal
import sys
import time
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from context_structure_retrace.config import ResearchTimeframes
from context_structure_retrace.data import CausalReplay, bar_end, iso
from context_structure_retrace.indicators import atr
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace.replay import feature_snapshot
from paper_runner import call_bridge
from context_structure_retrace_compact_state import project_state
from strategy_report_format import format_standard_report

ROOT = Path(__file__).resolve().parent
VERSION = "CONTEXT_STRUCTURE_RETRACE_V1"
OBSERVABILITY_VERSION = "phase6-report-1"
SCHEMA_VERSION = "context-structure-retrace-forward-v1-phase6-schema-1"
PHASE2_HASH = "923d0d2762b6b78515a96e96dba17e42e34818aa82c406dc9ebc6f43b1a54c41"
DEFAULT_SYMBOLS = ("XAUUSDm", "BTCUSDm", "USDJPYm", "EURUSDm")
TF = ResearchTimeframes(execution="M15", lower=("M5",), higher=("H1", "H4"))
READ_ONLY_BRIDGE_TOOLS = frozenset({"mt5_symbol_info", "mt5_quote", "mt5_rates", "mt5_symbol_snapshot"})
MEMBERSHIP_REFRESH_SECONDS = 15

# Runtime artifacts (state, event ledger, heartbeat, pid, summary, frozen manifest) live in
# CONTEXT_RUNNER_STATE_DIR, so the runner can execute from an immutable release image while its
# state stays on persistent storage. Defaults to the code directory (previous behaviour).
STATE_DIR = Path(os.environ.get("CONTEXT_RUNNER_STATE_DIR") or ROOT)
LEGACY_STATE = STATE_DIR / "context_structure_retrace_forward_state.json"
STATE = STATE_DIR / "context_structure_retrace_forward_state_compact.json"
EVENTS = STATE_DIR / "context_structure_retrace_forward.jsonl"
HEARTBEAT = STATE_DIR / "context_structure_retrace_forward.heartbeat.json"
PID = STATE_DIR / "context_structure_retrace_forward.pid"
MANIFEST = STATE_DIR / "context_structure_retrace_forward_manifest.json"
SUMMARY = STATE_DIR / "context_structure_retrace_forward_summary.md"
STOP_FILE = Path("/tmp/context-structure-retrace-v1-paper.stop")

# Recovery classification is transport/continuity metadata around the frozen
# evaluator.  It is deliberately outside the decision-function fingerprint.
_ACTIVE_RECOVERY_CONTEXT: dict[str, Any] | None = None


def load_active_membership(args: argparse.Namespace) -> tuple[tuple[str, ...], int | None]:
    """Resolve canonical instance membership to provider symbols at runtime (database mapping).

    The runner polls this durable snapshot; it never writes membership and it
    does not make provider symbols part of the strategy domain model. The CLI
    list remains a test/local fallback when no canonical database is configured.
    """
    dsn = os.getenv("TRADING_POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        return tuple(args.symbols), None
    try:
        import psycopg
        with psycopg.connect(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                # Provider symbols come from the database mapping (migration 027), never from a
                # file. An ACTIVE member without an ACTIVE mapping is simply not evaluated.
                cur.execute("""SELECT p.provider_symbol, greatest(m.revision, p.revision)
                    FROM strategy.instrument_membership m
                    JOIN platform.instrument_provider_mapping p
                      ON p.canonical_instrument = m.canonical_instrument AND p.provider = 'MT5' AND p.state = 'ACTIVE'
                    WHERE m.strategy_instance_id = %s AND m.strategy_id = %s AND m.state = 'ACTIVE'
                    ORDER BY m.canonical_instrument""", ("phase6", VERSION))
                rows = cur.fetchall()
        return tuple(symbol for symbol, _ in rows), max((int(revision) for _, revision in rows), default=0)
    except Exception as exc:
        # A membership read outage must not silently re-enable stale symbols.
        # Keep the process alive for observability, but evaluate no symbols.
        print(f"INSTRUMENT_MEMBERSHIP_UNAVAILABLE {type(exc).__name__}: {exc}")
        return (), None


def atomic_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest_files() -> str:
    h = hashlib.sha256()
    for name in ("context_structure_retrace_forward.py",):
        p = ROOT / name
        h.update(name.encode()); h.update(p.read_bytes())
    return h.hexdigest()


FROZEN_CONFIG = {
    "strategy_version": VERSION,
    "execution_timeframe": "M15",
    "lower_context": ["M5"],
    "higher_context": ["H1", "H4"],
    "setup_events": ["BULLISH_ENGULFING", "BEARISH_ENGULFING", "MORNING_STAR", "EVENING_STAR", "BULLISH_REJECTION_WICK", "BEARISH_REJECTION_WICK"],
    "retracement": "DEPTH_ONLY at 20% of originating setup range; other mechanisms recorded when observed",
    "stop": "ORIGINATING_SETUP_EXTREME with minimal causal volatility/spread safety buffer",
    "target": "STRUCTURE_CAPPED_EXTENSION; setup extreme plus 0.50*setup range, capped only by directionally valid opposing structure",
    "no_remaining_target": "target behind or at executable entry is non-tradable",
    "reentry": "INTERACT -> LEAVE_ZONE -> COMPLETED_LOWER_TF_CLOSE_OUTSIDE -> RETURN -> THESIS_VALID -> TARGET_NOT_COMPLETED; target completion returns are RETURN_AFTER_SETUP_TARGET_COMPLETED",
    "scale_in": "DISABLED; potential scale-in is logged only",
    "two_leg_risk": {"total": 1.0, "leg_a": 0.5, "leg_b": 0.5},
    "time_exit": "NONE",
    "commission": "UNKNOWN_UNRESOLVED",
    "slippage": "SHADOW_SENSITIVITY_ONLY",
    "symbols": list(DEFAULT_SYMBOLS),
}

# The manifest's code_hash is the frozen pre-observability source identity.
# Decision-code hashing below lets later restarts detect changes to the
# actual V1 lifecycle while permitting read-only report code to evolve.
LEGACY_FROZEN_SOURCE_HASH = "f931fe449d1bee78fde768374ad7ce88ded3f19f9afdf349e9acb47602772a0f"
DECISION_FUNCTION_NAMES = ("_geometry", "make_setup", "_fill", "_process_bar", "process_symbol",
                           "_evaluate_open_position", "_unevaluated_open_positions")
FROZEN_DECISION_CODE_HASH = "0a990dd5b3418bd065a702a3a50ffcebcf20dd26565fd432326b1f32ef3aeacf"
# Earlier decision-code identities, kept so historical signals (which carry their fingerprint in
# source_strategy_fingerprint) stay attributable. Trading rules/config are unchanged across them.
PRIOR_DECISION_CODE_HASHES = {
    "70dba71d28fe8a5c09f9033b80eeb4c27a733c6c342537e03c631f41e2a1cdda": "V1 decision code through 2026-09-26: OPEN positions stopped receiving exit "
             "evaluation once their setup left FILLED (e.g. INVALIDATED_NO_REENTRY) or was compacted away",
}


def config_hash() -> str:
    return hashlib.sha256(json.dumps(FROZEN_CONFIG, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def decision_code_hash() -> str:
    parts = []
    for name in DECISION_FUNCTION_NAMES:
        obj = globals().get(name)
        if obj is not None:
            parts.append(name + "\n" + inspect.getsource(obj))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def bridge_read(name: str, arguments: dict[str, Any], mcp_url: str) -> dict[str, Any]:
    """Read-only bridge boundary.  Any non-read tool is rejected."""
    if name not in READ_ONLY_BRIDGE_TOOLS:
        raise RuntimeError(f"paper-only read boundary rejected bridge tool: {name}")
    return call_bridge(mcp_url, name, arguments)


def empty_state() -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION, "strategy_version": VERSION, "manifest": str(MANIFEST),
        "created_at": now_iso(), "last_poll_at": None, "last_successful_read_at": None,
        "symbols": {}, "setups": {}, "positions": {}, "counters": {"events": 0, "setups": 0, "opportunities": 0, "positions": 0},
        "prospective_boundary": None, "runner_status": "STOPPED", "kill_switch": "OFF",
    }


def load_state() -> dict[str, Any]:
    if not STATE.exists():
        raise RuntimeError("compact Phase 6 state is missing; refusing to fall back to legacy full state")
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    # The legacy full state is immutable after cutover.  Project before every
    # checkpoint so append_event() and poll completion never serialize the
    # historical context payload.  Keep the live object graph intact: callers
    # may still hold references to symbol/setup/position dictionaries while a
    # lifecycle event checkpoints.  Replacing state['symbols'] (and the other
    # nested maps) here leaves those references detached and loses cursor and
    # lifecycle mutations made later in the same poll.
    compact = project_state(state)
    atomic_json(STATE, compact)


def _event_identity(event: dict[str, Any]) -> str | None:
    """Return the replay identity for a lifecycle event, if it has one."""
    event_type = event.get("type")
    entity = (event.get("economic_position_id") or event.get("entry_opportunity_id") or
              event.get("setup_id") or event.get("symbol"))
    if not event_type or not entity:
        return None
    # Lifecycle transitions are state facts, not append-count facts.  A
    # replay after an interrupted checkpoint must not emit a second terminal
    # transition for the same domain entity.
    if event_type in {"SETUP_DETECTED", "FORWARD_BASELINE_INITIALIZED", "FILLED", "TARGET_HIT", "STOPPED", "SETUP_INVALIDATED_BEFORE_ENTRY",
                      "INVALIDATED_NO_REENTRY", "NO_RETRACE", "RETURN_AFTER_SETUP_TARGET_COMPLETED"}:
        return f"{event_type}|{entity}"
    return None


_EVENT_IDENTITY_CACHE_PATH: Path | None = None
_EVENT_IDENTITIES: set[str] = set()


def _event_already_written(identity: str) -> bool:
    global _EVENT_IDENTITY_CACHE_PATH, _EVENT_IDENTITIES
    if not EVENTS.exists():
        return False
    if _EVENT_IDENTITY_CACHE_PATH != EVENTS:
        _EVENT_IDENTITY_CACHE_PATH = EVENTS
        _EVENT_IDENTITIES = set()
        try:
            with EVENTS.open(encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        prior = json.loads(line)
                        prior_identity = _event_identity(prior)
                        if prior_identity:
                            _EVENT_IDENTITIES.add(prior_identity)
        except (OSError, json.JSONDecodeError):
            pass
    if identity in _EVENT_IDENTITIES:
        return True
    return False


def append_event(event: dict[str, Any], state: dict[str, Any]) -> None:
    if _ACTIVE_RECOVERY_CONTEXT and event.get("symbol") == _ACTIVE_RECOVERY_CONTEXT["symbol"]:
        event = dict(event)
        event["source"] = "GAP_RECOVERY"
        event["recovery_generation"] = _ACTIVE_RECOVERY_CONTEXT["generation"]
        event["recovery_boundary_m5"] = _ACTIVE_RECOVERY_CONTEXT["boundary_m5"]
    identity = _event_identity(event)
    if identity and _event_already_written(identity):
        return
    event = {"schema": SCHEMA_VERSION, "strategy_version": VERSION, "event_time": now_iso(), **event}
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":"), sort_keys=True, default=str) + "\n")
    if identity:
        _EVENT_IDENTITIES.add(identity)
    state["counters"]["events"] += 1
    save_state(state)


def write_heartbeat(state: dict[str, Any], status: str = "ACTIVE") -> None:
    atomic_json(HEARTBEAT, {"runner_pid": os.getpid(), "timestamp": now_iso(), "status": status,
                            "last_successful_mt5_read": state.get("last_successful_read_at"),
                            "symbols": {s: {"last_m5": v.get("last_m5"), "last_m15": v.get("last_m15")} for s, v in state.get("symbols", {}).items()},
                            "poll_interval_seconds": state.get("poll_interval_seconds")})


def acquire_lock() -> None:
    if PID.exists():
        try:
            old = int(json.loads(PID.read_text()).get("pid"))
            os.kill(old, 0)
            raise RuntimeError(f"Forward runner already active PID {old}")
        except ProcessLookupError:
            PID.unlink(missing_ok=True)
        except ValueError:
            PID.unlink(missing_ok=True)
    atomic_json(PID, {"pid": os.getpid(), "started": now_iso(), "strategy": VERSION})


def release_lock() -> None:
    PID.unlink(missing_ok=True)


def freeze() -> dict[str, Any]:
    manifest = {
        "strategy_version": VERSION, "freeze_timestamp": now_iso(), "code_hash": digest_files(),
        "configuration_hash": config_hash(), "schema_version": SCHEMA_VERSION,
        "phase2_representation_hash": PHASE2_HASH, "configuration": FROZEN_CONFIG,
        "data_status_before_freeze": "EXPOSED_DEVELOPMENT_DATA",
        "data_status_after_freeze": "PROSPECTIVE_FORWARD_DATA",
        "execution_isolation": {"mode": "PAPER_READ_ONLY", "allowed_bridge_tools": sorted(READ_ONLY_BRIDGE_TOOLS), "broker_order_submission": False},
    }
    if MANIFEST.exists():
        old = json.loads(MANIFEST.read_text())
        if old.get("configuration_hash") != manifest["configuration_hash"]:
            raise RuntimeError("existing freeze manifest does not match current code/config; V1 is immutable")
        return old
    atomic_json(MANIFEST, manifest)
    return manifest


def assert_frozen() -> dict[str, Any]:
    if not MANIFEST.exists(): raise RuntimeError("V1 is not frozen; run freeze after tests")
    manifest = json.loads(MANIFEST.read_text())
    # The manifest's full-file hash predates observability-only persistence.
    # Keep the immutable decision-code fingerprint as the execution guard so
    # provenance metadata can evolve without changing V1 decisions.
    if manifest.get("code_hash") != LEGACY_FROZEN_SOURCE_HASH and decision_code_hash() != FROZEN_DECISION_CODE_HASH:
        raise RuntimeError("frozen source identity mismatch; refusing to start")
    if manifest.get("configuration_hash") != config_hash(): raise RuntimeError("frozen configuration mismatch; refusing to start")
    if manifest.get("phase2_representation_hash") != PHASE2_HASH: raise RuntimeError("Phase 2 representation hash mismatch")
    if FROZEN_DECISION_CODE_HASH and decision_code_hash() != FROZEN_DECISION_CODE_HASH: raise RuntimeError("frozen decision-code fingerprint mismatch; refusing to start")
    return manifest


def _completed(rates: dict[str, Any]) -> list[dict[str, Any]]:
    rows = rates.get("rates", [])
    return rows[:-1] if len(rows) > 1 else []


def read_symbol(symbol: str, mcp_url: str, limit: int = 320, include_provenance: bool = False) -> tuple[Any, ...]:
    if os.getenv("MARKET_DATA_SOURCE", "BRIDGE").strip().upper() == "REDIS":
        # Same (contract, quote, bars) shape from the market-data cache (market_data_cache/),
        # which one collector keeps current; no bridge call here. Missing/stale data raises,
        # exactly like a failed bridge read. Not part of the frozen decision code.
        from market_data_cache.reader import default_store, read_symbol_cached
        return read_symbol_cached(default_store(), symbol, limit, include_provenance)
    # One bounded, read-only per-symbol snapshot replaces the six serialized
    # reads previously needed for one strategy evaluation.  The EA builds the
    # snapshot from the same QuoteJson/RatesJson/SymbolInfoJson primitives;
    # this function only adapts the response back to the frozen strategy shape.
    snapshot = bridge_read("mt5_symbol_snapshot", {
        "symbol": symbol, "timeframes": ["M5", "M15", "H1", "H4"], "limit": limit,
    }, mcp_url)
    if not snapshot.get("healthy") or snapshot.get("source_read_health") is not True:
        raise RuntimeError("symbol snapshot unhealthy: " + json.dumps(snapshot.get("components", {}), sort_keys=True))
    contract = snapshot.get("symbol_info")
    quote = snapshot.get("quote")
    raw_rates = snapshot.get("rates") or {}
    required = ("M5", "M15", "H1", "H4")
    if not isinstance(contract, dict) or not isinstance(quote, dict) or any(tf not in raw_rates for tf in required):
        raise RuntimeError("symbol snapshot missing required component")
    bars = {tf: _completed(raw_rates[tf]) for tf in required}
    if not include_provenance:
        return contract, quote, bars
    quote_time = quote.get("time") if isinstance(quote, dict) else None
    producer = {
        "source_read_health": snapshot.get("source_read_health"),
        "source_market_data_timestamp": iso(int(quote_time)) if quote_time is not None else None,
        "provenance_source": "PHASE6_SUCCESSFUL_SNAPSHOT",
    }
    return contract, quote, bars, producer


def _spread(bar: dict[str, Any], contract: dict[str, Any], quote: dict[str, Any]) -> float:
    point = float(contract.get("point", contract.get("tick_size", 0.0)) or 0.0)
    if quote.get("ask") is not None and quote.get("bid") is not None:
        return abs(float(quote["ask"]) - float(quote["bid"]))
    return float(bar.get("spread", 0) or 0) * point


def _context(snapshot: dict[str, Any], direction: str) -> dict[str, Any]:
    tf = snapshot["provenance"]["structure_timeframe"]
    item = snapshot["timeframes"].get(tf, {})
    ema = item.get("ema_context", {})
    zones = item.get("sr_context", {}).get("zones", [])
    price = float((item.get("completed_candle") or {}).get("close", 0))
    supports = [z for z in zones if float(z.get("midpoint", 0)) <= price]
    resistances = [z for z in zones if float(z.get("midpoint", 0)) >= price]
    below = max(supports, key=lambda z: float(z["midpoint"]), default=None)
    above = min(resistances, key=lambda z: float(z["midpoint"]), default=None)
    contradictions = []
    for tf2 in ("H1", "H4"):
        if snapshot["timeframes"].get(tf2, {}).get("completed_direction") not in (None, "UNKNOWN", "UP" if direction == "LONG" else "DOWN"):
            contradictions.append(tf2)
    return {"direction": direction, "nearest_support": below, "nearest_resistance": above,
            "distance_to_support": price - float(below["midpoint"]) if below else None,
            "distance_to_resistance": float(above["midpoint"]) - price if above else None,
            "ema_ordering": ema.get("ordering"), "ema_slopes": ema.get("slopes"),
            "ema_values": ema.get("ema_values_completed", {}), "htf_contradictions": contradictions,
            "flags": (["HTF_CONTRADICTION_FLAG"] if contradictions else []) + (["NEAR_OPPOSING_STRUCTURE_FLAG"] if (above if direction == "LONG" else below) else [])}


def _geometry(event_bar: dict[str, Any], direction: str, snapshot: dict[str, Any], entry: float, spread: float, atr_value: float | None) -> dict[str, Any]:
    lo, hi = float(event_bar["low"]), float(event_bar["high"]); rng = max(hi - lo, 1e-12)
    buffer = max(float(atr_value or 0) * 0.10, spread * 1.25)
    stop_ref = lo if direction == "LONG" else hi
    stop = stop_ref - buffer if direction == "LONG" else stop_ref + buffer
    extension = hi + 0.50 * rng if direction == "LONG" else lo - 0.50 * rng
    structure = snapshot["timeframes"][snapshot["provenance"]["structure_timeframe"]]["sr_context"]
    zones = structure.get("zones", [])
    opposing = None
    if direction == "LONG":
        candidates = [float(z["zone_low"]) for z in zones if z.get("support_resistance_role") == "RESISTANCE" and float(z["zone_low"]) > entry]
        opposing = min(candidates) if candidates else None
        target = min([extension] + ([opposing] if opposing is not None else []))
        signed = target - entry
    else:
        candidates = [float(z["zone_high"]) for z in zones if z.get("support_resistance_role") == "SUPPORT" and float(z["zone_high"]) < entry]
        opposing = max(candidates) if candidates else None
        target = max([extension] + ([opposing] if opposing is not None else []))
        signed = entry - target
    risk = entry - stop if direction == "LONG" else stop - entry
    target_state = "TARGET_BEYOND_ENTRY" if signed > 0 else "TARGET_AT_ENTRY" if signed == 0 else "TARGET_BEHIND_ENTRY"
    return {"structural_reference": stop_ref, "stop": stop, "stop_distance": risk, "extension_target": extension,
            "opposing_structure": opposing, "effective_target": target, "signed_target_distance": signed,
            "target_direction_state": target_state, "target_R": signed / risk if risk > 0 else None,
            "spread_target_ratio": spread / signed if signed > 0 else None, "spread_stop_ratio": spread / risk if risk > 0 else None,
            "flags": (["OPPOSING_STRUCTURE_VERY_CLOSE"] if signed > 0 and risk and signed / risk < 0.10 else [])}


def _event_id(symbol: str, event: dict[str, Any]) -> str:
    return event["event_id"]


def make_setup(symbol: str, event: dict[str, Any], event_bar: dict[str, Any], snapshot: dict[str, Any], quote: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    direction = event["direction"]
    rng = float(event_bar["high"]) - float(event_bar["low"])
    theoretical = float(event_bar["close"]) - 0.20 * rng if direction == "LONG" else float(event_bar["close"]) + 0.20 * rng
    spread = _spread(event_bar, contract, quote)
    executable_reference = theoretical + spread / 2 if direction == "LONG" else theoretical - spread / 2
    atr_value = snapshot["timeframes"]["M15"]["ema_context"].get("atr")
    geometry = _geometry(event_bar, direction, snapshot, executable_reference, spread, atr_value)
    context = _context(snapshot, direction)
    setup_id = _event_id(symbol, event)
    market_event = hashlib.sha256(f"{symbol}|{event['timestamp']}|{event['pattern']}".encode()).hexdigest()[:20]
    return {"setup_id": setup_id, "market_event_id": market_event, "symbol": symbol, "direction": direction,
            "pattern": event["pattern"], "setup_timestamp": int(event["timestamp"]), "setup_timestamp_iso": iso(int(event["timestamp"])),
            "event": event, "event_bar": event_bar, "context_snapshot": snapshot, "context_components": context,
            "qualification": "QUALIFIED_FOR_RETRACE_MONITORING", "qualification_flags": context["flags"],
            "retrace_state": "WAITING_FOR_RETRACEMENT", "theoretical_entry": theoretical, "entry_level": theoretical,
            "geometry_at_reference": geometry, "spread_at_detection": spread, "mechanisms": [],
            "opportunities": [], "target_completed": False, "status": "WAITING_FOR_RETRACE",
            "provenance": {"as_of_timestamp": int(event["timestamp"]), "completed_candles_only": True, "future_ohlc_exposed": False,
                           "source": "LIVE_FORWARD", "phase2_representation_hash": PHASE2_HASH}}


def _m5_mechanisms(prefix: list[dict[str, Any]], direction: str, symbol: str) -> list[str]:
    if not prefix: return []
    bar = prefix[-1]; body = abs(float(bar["close"]) - float(bar["open"])); rng = max(float(bar["high"]) - float(bar["low"]), 1e-12)
    out = []
    if (direction == "LONG" and float(bar["close"]) > float(bar["open"]) and min(float(bar["open"]), float(bar["close"])) - float(bar["low"]) >= max(body * 2, 1e-12) and (float(bar["close"]) - float(bar["low"])) / rng >= .65) or (direction == "SHORT" and float(bar["close"]) < float(bar["open"]) and float(bar["high"]) - max(float(bar["open"]), float(bar["close"])) >= max(body * 2, 1e-12) and (float(bar["close"]) - float(bar["low"])) / rng <= .35):
        out.append("REJECTION_WICK")
    events = detect_patterns(prefix, "M5", bar_end(bar, "M5"), symbol, completed_override=prefix)
    wanted = {"LONG": {"BULLISH_ENGULFING", "MORNING_STAR"}, "SHORT": {"BEARISH_ENGULFING", "EVENING_STAR"}}[direction]
    if any(x["pattern"] in wanted for x in events): out.append("LOWER_TF_ENGULFING" if any("ENGULFING" in x["pattern"] for x in events) else "MORNING_EVENING_STAR")
    out.append("DEPTH_ONLY")
    return sorted(set(out))


def _persist_setup(state: dict[str, Any], setup: dict[str, Any], reason: str | None = None) -> None:
    state["setups"][setup["setup_id"]] = setup
    state["counters"]["setups"] += 1
    append_event({"type": "SETUP_DETECTED", "symbol": setup["symbol"], "setup_id": setup["setup_id"], "market_event_id": setup["market_event_id"], "direction": setup["direction"], "pattern": setup["pattern"], "qualification": setup["qualification"], "reason": reason, "source": setup["provenance"]["source"]}, state)


def _fill(state: dict[str, Any], setup: dict[str, Any], bar: dict[str, Any], index: int, m5: list[dict[str, Any]], contract: dict[str, Any], quote: dict[str, Any]) -> None:
    spread = _spread(bar, contract, quote)
    direction = setup["direction"]
    level = float(setup["entry_level"])
    executable = level + spread / 2 if direction == "LONG" else level - spread / 2
    geom = _geometry(setup["event_bar"], direction, setup["context_snapshot"], executable, spread, setup["context_snapshot"]["timeframes"]["M15"]["ema_context"].get("atr"))
    if geom["target_direction_state"] != "TARGET_BEYOND_ENTRY":
        setup["status"] = "NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY"; setup["retrace_state"] = setup["status"]
        append_event({"type": setup["status"], "symbol": setup["symbol"], "setup_id": setup["setup_id"], "entry_opportunity_id": None, "target_geometry": geom}, state); return
    number = len(setup["opportunities"]) + 1
    opportunity_id = hashlib.sha256(f"{setup['setup_id']}|opportunity|{number}".encode()).hexdigest()[:20]
    position_id = hashlib.sha256(f"{opportunity_id}|economic".encode()).hexdigest()[:20]
    mechanisms = _m5_mechanisms(m5[:index + 1], direction, setup["symbol"])
    opportunity = {"entry_opportunity_id": opportunity_id, "entry_attempt_id": hashlib.sha256(f"{opportunity_id}|attempt|1".encode()).hexdigest()[:20], "economic_position_id": position_id, "symbol": setup["symbol"], "direction": direction, "setup_id": setup["setup_id"], "fill_timestamp": int(bar["time"]), "fill_timestamp_iso": iso(int(bar["time"])), "fill_candle_number": index - setup.get("m5_start_index", index), "entry_mechanisms": mechanisms, "theoretical_entry": level, "executable_paper_entry": executable, "spread_at_fill": spread, "stop": geom["stop"], "target": geom["effective_target"], "geometry": geom, "leg_a": {"allocation_R": 0.5, "status": "OPEN"}, "leg_b": {"allocation_R": 0.5, "status": "OPEN", "runner_hypotheses": ["+1R", "+1.5R", "+2R", "+3R", "LOWER_TF_STRUCTURE_TRAIL", "EMA_STRUCTURE_EXIT", "OPPOSITE_PRICE_ACTION_EXIT"]}, "status": "OPEN", "mfe_price": 0.0, "mae_price": 0.0, "entry_bar": bar, "reentry_type": "INITIAL" if number == 1 else "REENTRY_BEFORE_TARGET_COMPLETION"}
    setup["opportunities"].append(opportunity); setup["status"] = "FILLED"; setup["retrace_state"] = "FILLED"; state["positions"][position_id] = opportunity; state["counters"]["opportunities"] += 1; state["counters"]["positions"] += 1
    append_event({"type": "FILLED", "source": setup["provenance"]["source"], "symbol": setup["symbol"], "setup_id": setup["setup_id"], "market_event_id": setup["market_event_id"], "entry_opportunity_id": opportunity_id, "economic_position_id": position_id, "entry": executable, "stop": geom["stop"], "target": geom["effective_target"], "entry_mechanisms": mechanisms}, state)


def _evaluate_open_position(state: dict[str, Any], symbol: str, setup: dict[str, Any] | None, position: dict[str, Any], direction: str, bar: dict[str, Any], source: str) -> None:
    # V1 exit rule, unchanged: bar high/low against the frozen stop/target, stop takes
    # precedence when both are touched in the same bar, realized R = -1 or target_R.
    low, high = float(bar["low"]), float(bar["high"])
    entry = float(position["executable_paper_entry"]); stop = float(position["stop"]); target = float(position["target"])
    adverse = (entry - low) if direction == "LONG" else (high - entry); favorable = (high - entry) if direction == "LONG" else (entry - low)
    position["mfe_price"] = max(float(position.get("mfe_price", 0)), favorable); position["mae_price"] = max(float(position.get("mae_price", 0)), adverse)
    hit_stop = low <= stop if direction == "LONG" else high >= stop
    hit_target = high >= target if direction == "LONG" else low <= target
    if hit_stop or hit_target:
        reason = "STOPPED" if hit_stop else "TARGET_HIT"
        position["status"] = reason; position["exit_timestamp"] = int(bar["time"]); position["exit_reason"] = reason; position["realized_R"] = -1.0 if hit_stop else float(position["geometry"]["target_R"]); position["leg_a"]["status"] = reason; position.setdefault("leg_b", {})["breakeven_activated"] = bool(hit_target)
        if hit_target and setup is not None: setup["target_completed"] = True
        append_event({"type": reason, "source": source, "symbol": symbol, "setup_id": setup["setup_id"] if setup is not None else position.get("setup_id"), "economic_position_id": position["economic_position_id"], "realized_R": position["realized_R"], "mfe": position["mfe_price"], "mae": position["mae_price"]}, state)


def _unevaluated_open_positions(state: dict[str, Any], symbol: str) -> list[tuple[dict[str, Any] | None, dict[str, Any]]]:
    # Position lifecycle is independent of setup lifecycle. Setup status governs entry and
    # re-entry only; an OPEN position still needs exit evaluation after its setup leaves
    # FILLED (e.g. INVALIDATED_NO_REENTRY) or is no longer retained in compact state.
    # Setup-held positions are the lifecycle authority (see project_state); a positions-map
    # record is used only when no retained setup holds that position.
    found: list[tuple[dict[str, Any] | None, dict[str, Any]]] = []
    held: set[str] = set()
    for setup in state["setups"].values():
        for position in setup.get("opportunities", []):
            held.add(str(position.get("economic_position_id")))
            if setup["symbol"] == symbol and setup["status"] not in ("WAITING_FOR_RETRACE", "FILLED") and position.get("status") == "OPEN":
                found.append((setup, position))
    for position_id, position in state.get("positions", {}).items():
        if str(position_id) not in held and position.get("symbol") == symbol and position.get("status") == "OPEN":
            found.append((None, position))
    return found


def _process_bar(state: dict[str, Any], symbol: str, bar: dict[str, Any], index: int, m5: list[dict[str, Any]], contract: dict[str, Any], quote: dict[str, Any], source: str) -> None:
    evaluated: set[str] = set()
    for setup in list(state["setups"].values()):
        if setup["symbol"] != symbol or setup["status"] not in ("WAITING_FOR_RETRACE", "FILLED"): continue
        if int(bar["time"]) <= int(setup["setup_timestamp"]): continue
        direction = setup["direction"]; low, high = float(bar["low"]), float(bar["high"])
        if setup["status"] == "WAITING_FOR_RETRACE":
            if (direction == "LONG" and low <= float(setup["event_bar"]["low"])) or (direction == "SHORT" and high >= float(setup["event_bar"]["high"])):
                setup["status"] = "INVALIDATED_NO_REENTRY"; setup["retrace_state"] = "SETUP_INVALIDATED_BEFORE_ENTRY"; append_event({"type": "SETUP_INVALIDATED_BEFORE_ENTRY", "source": source, "symbol": symbol, "setup_id": setup["setup_id"]}, state); continue
            level = float(setup["entry_level"])
            touched = low <= level <= high
            held = float(bar["close"]) >= level if direction == "LONG" else float(bar["close"]) <= level
            if touched and held:
                setup["m5_start_index"] = index
                _fill(state, setup, bar, index, m5, contract, quote)
                continue
            setup["m5_start_index"] = setup.get("m5_start_index", index)
            if index - setup["m5_start_index"] >= 12:
                setup["status"] = "NO_RETRACE"; setup["retrace_state"] = "NO_RETRACE"; append_event({"type": "NO_RETRACE", "source": source, "symbol": symbol, "setup_id": setup["setup_id"]}, state)
        else:
            level = float(setup["entry_level"])
            zone_width = max(float(setup.get("spread_at_detection") or 0.0), 1e-12)
            in_zone = float(bar["low"]) <= level + zone_width / 2 and float(bar["high"]) >= level - zone_width / 2
            closed_outside = float(bar["close"]) > level + zone_width / 2 or float(bar["close"]) < level - zone_width / 2
            if not setup.get("zone_left") and closed_outside:
                setup["zone_left"] = True
                append_event({"type": "LEAVE_ENTRY_ZONE", "source": source, "symbol": symbol, "setup_id": setup["setup_id"], "entry_opportunity_id": setup["opportunities"][-1]["entry_opportunity_id"]}, state)
            if setup.get("zone_left") and in_zone:
                setup["zone_left"] = False
                if setup.get("target_completed"):
                    setup["status"] = "RETURN_AFTER_SETUP_TARGET_COMPLETED"
                    append_event({"type": "RETURN_AFTER_SETUP_TARGET_COMPLETED", "source": source, "symbol": symbol, "setup_id": setup["setup_id"]}, state)
                elif setup.get("thesis_invalidated"):
                    setup["status"] = "INVALIDATED_NO_REENTRY"
                    append_event({"type": "INVALIDATED_NO_REENTRY", "source": source, "symbol": symbol, "setup_id": setup["setup_id"]}, state)
                else:
                    open_position = any(p.get("status") == "OPEN" for p in setup["opportunities"])
                    if open_position:
                        append_event({"type": "POTENTIAL_SCALE_IN", "source": source, "symbol": symbol, "setup_id": setup["setup_id"], "reason": "POSITION_ALREADY_OPEN_SCALEIN_DISABLED"}, state)
                    else:
                        _fill(state, setup, bar, index, m5, contract, quote)
            for position in setup["opportunities"]:
                if position["status"] != "OPEN": continue
                evaluated.add(position["economic_position_id"])
                _evaluate_open_position(state, symbol, setup, position, direction, bar, source)
        if setup["status"] in ("FILLED", "RETURN_AFTER_SETUP_TARGET_COMPLETED"):
            if (direction == "LONG" and low <= float(setup["event_bar"]["low"])) or (direction == "SHORT" and high >= float(setup["event_bar"]["high"])):
                setup["thesis_invalidated"] = True
                if not setup.get("target_completed"):
                    setup["status"] = "INVALIDATED_NO_REENTRY"
                    append_event({"type": "INVALIDATED_NO_REENTRY", "source": source, "symbol": symbol, "setup_id": setup["setup_id"]}, state)
    for setup, position in _unevaluated_open_positions(state, symbol):
        if position["economic_position_id"] in evaluated or int(bar["time"]) <= int(position["fill_timestamp"]): continue
        direction = setup["direction"] if setup is not None else position.get("direction")
        if direction not in ("LONG", "SHORT"): continue  # legacy setup-less record without frozen direction
        evaluated.add(position["economic_position_id"])
        _evaluate_open_position(state, symbol, setup, position, direction, bar, source)


def process_symbol(state: dict[str, Any], symbol: str, contract: dict[str, Any], quote: dict[str, Any], bars: dict[str, list[dict[str, Any]]]) -> None:
    m5, m15 = bars["M5"], bars["M15"]
    if not m5 or not m15: return
    sym = state["symbols"].setdefault(symbol, {"last_m5": None, "last_m15": None, "initialized": False})
    current_m5 = int(m5[-1]["time"]); current_m15 = int(m15[-1]["time"])
    if not sym["initialized"]:
        sym.update({"last_m5": current_m5, "last_m15": current_m15, "initialized": True, "last_candle": iso(current_m5)})
        append_event({"type": "FORWARD_BASELINE_INITIALIZED", "symbol": symbol, "last_completed_m5": current_m5, "last_completed_m15": current_m15, "source": "LIVE_FORWARD"}, state); return
    last_m5 = int(sym.get("last_m5") or current_m5); last_m15 = int(sym.get("last_m15") or current_m15)
    expected = 300
    if current_m5 - last_m5 > expected:
        missing = max(0, current_m5 // expected - last_m5 // expected - 1)
        append_event({"type": "DATA_GAP_DETECTED", "symbol": symbol, "gap_start": last_m5, "gap_end": current_m5, "missing_completed_m5_candles": missing, "reason": "UNKNOWN", "source": "GAP_RECOVERY"}, state)
    # Register only newly completed M15 setup events, in chronological order.
    for bar in m15:
        ts = int(bar["time"])
        if ts <= last_m15: continue
        prefix = [x for x in m15 if int(x["time"]) <= ts]
        as_of = bar_end(bar, "M15")
        events = detect_patterns(prefix, "M15", as_of, symbol, completed_override=prefix)
        if not events: continue
        replay = CausalReplay(bars)
        snapshot = feature_snapshot(replay, symbol, as_of, quote=quote, contract=contract, timeframes=TF, structure_max_points=4)
        for event in events:
            setup = make_setup(symbol, event, bar, snapshot, quote, contract)
            _persist_setup(state, setup)
    new_bars = [bar for bar in m5 if int(bar["time"]) > last_m5]
    for bar in new_bars:
        _process_bar(state, symbol, bar, m5.index(bar), m5, contract, quote, "GAP_RECOVERY" if current_m5 - last_m5 > expected else "LIVE_FORWARD")
    sym.update({"last_m5": current_m5, "last_m15": current_m15, "last_candle": iso(current_m5)})


def _recovery_context(state: dict[str, Any], symbol: str, current_m5: int) -> tuple[bool, bool, int, int]:
    """Advance the per-symbol continuity state without changing V1 decisions.

    A gap poll and the first fresh poll proving continuity remain recovery
    observations.  Only the following fresh poll is prospective.  The
    boundary is the newest completed M5 candle in the recovery snapshot, not
    process wall-clock time.
    """
    sym = state["symbols"].setdefault(symbol, {"last_m5": None, "last_m15": None, "initialized": False})
    prior = sym.get("last_m5")
    gap = prior is not None and current_m5 - int(prior) > 300
    if gap:
        generation = int(sym.get("recovery_generation", 0)) + 1
        sym.update({"recovery_state": "GAP_RECOVERY", "recovery_generation": generation,
                    "recovery_boundary_m5": current_m5})
    state_name = sym.get("recovery_state", "NORMAL")
    active = gap or state_name in {"GAP_RECOVERY", "RECOVERY_CAUGHT_UP"}
    generation = int(sym.get("recovery_generation", 0))
    boundary = int(sym.get("recovery_boundary_m5") or current_m5)
    return active, gap, generation, boundary


def _finish_recovery(state: dict[str, Any], symbol: str, current_m5: int, active: bool, boundary: int) -> None:
    if not active:
        return
    sym = state["symbols"][symbol]
    if current_m5 >= boundary:
        # The current poll proved continuity.  The next fresh poll is the
        # first one permitted to create prospective candidates.
        sym["recovery_state"] = "NORMAL"
        sym["recovery_completed_boundary_m5"] = current_m5


def _tag_recovery_lineage(state: dict[str, Any], symbol: str, before_setups: set[str],
                          before_positions: set[str], generation: int, boundary: int) -> None:
    """Make recovery provenance sticky on newly created setup lineages."""
    for setup_id, setup in state.get("setups", {}).items():
        if setup.get("symbol") != symbol:
            continue
        if setup_id not in before_setups:
            provenance = dict(setup.get("provenance") or {})
            provenance.update({"source": "GAP_RECOVERY", "gap_recovery": True,
                               "recovery_generation": generation,
                               "recovery_boundary_m5": boundary})
            setup["provenance"] = provenance
        for position in setup.get("opportunities", []):
            position_id = position.get("economic_position_id")
            if position_id in before_positions:
                continue
            position_provenance = dict(position.get("provenance") or {})
            position_provenance.update({"gap_recovery": True,
                                        "recovery_generation": generation,
                                        "recovery_boundary_m5": boundary})
            position["provenance"] = position_provenance


def persist_new_opportunity_provenance(state: dict[str, Any], before_opportunities: set[str], symbol: str,
                                       evaluation_timestamp: str, gap_recovery: bool,
                                       producer_provenance: dict[str, Any] | None = None) -> None:
    """Attach immutable read provenance to opportunities created by one poll.

    This function is deliberately outside the frozen decision-function set.
    It only observes newly-created records and never changes strategy fields.
    """
    evaluation_epoch = datetime.fromisoformat(evaluation_timestamp.replace("Z", "+00:00")).timestamp()
    for position_id, position in state.get("positions", {}).items():
        if position_id in before_opportunities or position.get("symbol") not in (None, symbol):
            continue
        producer_provenance = producer_provenance or {}
        source_timestamp = producer_provenance.get("source_market_data_timestamp")
        source_epoch = None
        if source_timestamp:
            source_epoch = datetime.fromisoformat(str(source_timestamp).replace("Z", "+00:00")).timestamp()
        position["provenance"] = {
            "source_read_health": producer_provenance.get("source_read_health"),
            "source_market_data_timestamp": str(source_timestamp) if source_timestamp else None,
            "source_data_age": max(0.0, evaluation_epoch - source_epoch) if source_epoch is not None else None,
            "gap_recovery": bool(gap_recovery),
            "evaluation_timestamp": evaluation_timestamp,
            "provenance_source": producer_provenance.get("provenance_source", "PHASE6_SUCCESSFUL_SNAPSHOT"),
        }


def poll(state: dict[str, Any], symbols: tuple[str, ...], mcp_url: str, limit: int) -> None:
    state["last_poll_at"] = now_iso(); state["runner_status"] = "ACTIVE"
    for symbol in symbols:
        try:
            contract, quote, bars, producer_provenance = read_symbol(symbol, mcp_url, limit, include_provenance=True)
            evaluation_timestamp = now_iso()
            before_opportunities = set(state.get("positions", {}))
            before_setups = set(state.get("setups", {}))
            prior_last_m5 = state.get("symbols", {}).get(symbol, {}).get("last_m5")
            completed_m5 = bars.get("M5", [])
            current_m5 = int(completed_m5[-1]["time"]) if completed_m5 else None
            recovery_active, gap_recovery, recovery_generation, recovery_boundary = _recovery_context(state, symbol, current_m5)
            global _ACTIVE_RECOVERY_CONTEXT
            _ACTIVE_RECOVERY_CONTEXT = ({"symbol": symbol, "generation": recovery_generation,
                                         "boundary_m5": recovery_boundary} if recovery_active else None)
            try:
                process_symbol(state, symbol, contract, quote, bars)
            finally:
                _ACTIVE_RECOVERY_CONTEXT = None
            # This is observability only.  The frozen evaluator above remains
            # unchanged; provenance is attached only to opportunities created
            # by this successful read/evaluation and is never backfilled.
            persist_new_opportunity_provenance(state, before_opportunities, symbol,
                                                evaluation_timestamp, recovery_active or gap_recovery, producer_provenance)
            if recovery_active:
                _tag_recovery_lineage(state, symbol, before_setups, before_opportunities,
                                      recovery_generation, recovery_boundary)
                _finish_recovery(state, symbol, current_m5, recovery_active, recovery_boundary)
            state["symbols"].setdefault(symbol, {})["last_quote"] = quote
            state["symbols"][symbol]["contract"] = contract
            state["last_successful_read_at"] = now_iso()
        except Exception as exc:
            state["symbols"].setdefault(symbol, {})["last_error"] = str(exc)
            append_event({"type": "READ_ERROR", "symbol": symbol, "error": str(exc), "source": "LIVE_FORWARD"}, state)
    save_state(state); write_heartbeat(state)


def summary(state: dict[str, Any]) -> str:
    positions = [p for p in state.get("positions", {}).values()]
    closed = [p for p in positions if p.get("status") in ("STOPPED", "TARGET_HIT")]
    rs = [float(p["realized_R"]) for p in closed if p.get("realized_R") is not None]
    return "\n".join([f"# {VERSION} — PAPER ONLY", f"Runner: {state.get('runner_status')}", f"Prospective boundary: {state.get('prospective_boundary')}", f"Setups: {state['counters']['setups']}", f"Entry opportunities: {state['counters']['opportunities']}", f"Closed Leg-A outcomes: {len(closed)}", f"Cumulative realized R: {sum(rs):.4f}", f"Open simulated positions: {sum(p.get('status') == 'OPEN' for p in positions)}", f"Commission: UNKNOWN_UNRESOLVED", "Broker order submission: DISABLED"])


def status() -> None:
    state = load_state(); manifest = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    print(json.dumps({"strategy": VERSION, "manifest": manifest, "runner_status": state.get("runner_status"), "pid": json.loads(PID.read_text()).get("pid") if PID.exists() else None, "last_poll": state.get("last_poll_at"), "last_successful_read": state.get("last_successful_read_at"), "symbols": state.get("symbols"), "counters": state.get("counters"), "open_positions": [p for p in state.get("positions", {}).values() if p.get("status") == "OPEN"], "paper_only": True, "broker_order_submission": False}, indent=2, default=str))


def _read_events() -> list[dict[str, Any]]:
    if not EVENTS.exists():
        return []
    rows = []
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _prospective_boundary(manifest: dict[str, Any]) -> str:
    return str(manifest.get("freeze_timestamp", "2026-09-16T05:00:08Z"))


def _is_prospective_event(row: dict[str, Any], boundary: str) -> bool:
    if row.get("type") == "FORWARD_BASELINE_INITIALIZED":
        return False
    return str(row.get("event_time", "")) >= boundary


def _position_is_prospective(position: dict[str, Any], boundary: str) -> bool:
    return str(position.get("fill_timestamp_iso", "")) >= boundary


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _percent(n: int, d: int) -> str:
    return "N/A" if not d else f"{100.0 * n / d:.1f}%"


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    peak = 0.0; equity = 0.0; worst = 0.0
    for value in values:
        equity += value; peak = max(peak, equity); worst = max(worst, peak - equity)
    return worst


SHADOW_FAVORABLE_THRESHOLDS = {"+1R": 1.0, "+1.5R": 1.5, "+2R": 2.0, "+3R": 3.0}


def _shadow_position_observation(position: dict[str, Any]) -> dict[str, Any]:
    """Read already-persisted causal MFE/MAE; never creates observations.

    _process_bar updates mfe_price/mae_price only after _fill returns and only
    while the economic position is OPEN. Consequently these values represent
    V1-position MFE/MAE through the V1 exit, not post-exit shadow continuation.
    Threshold timestamps were not persisted by the original runner and remain
    unavailable rather than being inferred from future data.
    """
    geometry = position.get("geometry") or {}
    risk = float(geometry.get("stop_distance") or 0.0)
    mfe_price = max(0.0, float(position.get("mfe_price") or 0.0))
    mae_price = max(0.0, float(position.get("mae_price") or 0.0))
    mfe_r = mfe_price / risk if risk > 0 else None
    mae_r = mae_price / risk if risk > 0 else None
    reached = {name: bool(mfe_r is not None and mfe_r >= threshold) for name, threshold in SHADOW_FAVORABLE_THRESHOLDS.items()}
    return {
        "economic_position_id": position.get("economic_position_id"),
        "symbol": position.get("symbol"),
        "setup_id": position.get("setup_id"),
        "status": position.get("status"),
        "scope": "V1_POSITION_MFE_MAE_UNTIL_V1_EXIT",
        "initial_risk_distance": risk,
        "mfe_price": mfe_price,
        "mae_price": mae_price,
        "mfe_R": mfe_r,
        "mae_R": mae_r,
        "thresholds_reached": reached,
        "threshold_timestamps": {name: None for name in SHADOW_FAVORABLE_THRESHOLDS},
        "post_exit_shadow_max_R": None,
        "post_exit_shadow_duration": None,
        "timestamp_note": "Not persisted by the original runner; no timestamp inferred by reporting.",
    }


def report_data(symbol: str | None = None, recent: int | None = None) -> dict[str, Any]:
    """Build a report from existing files only; this function has no writes."""
    state = load_state()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    boundary = _prospective_boundary(manifest)
    events = [x for x in _read_events() if _is_prospective_event(x, boundary) and (symbol is None or x.get("symbol") == symbol)]
    if recent:
        events = events[-int(recent):]
    event_counts: dict[str, int] = {}
    for row in events:
        event_counts[row.get("type", "UNKNOWN")] = event_counts.get(row.get("type", "UNKNOWN"), 0) + 1
    setups = [x for x in state.get("setups", {}).values() if int(x.get("setup_timestamp", 0)) * 1 >= 0 and (symbol is None or x.get("symbol") == symbol) and str(x.get("provenance", {}).get("source")) == "LIVE_FORWARD"]
    # State setup objects are only included in prospective state after the
    # freeze boundary. The event filter is authoritative for report counts.
    setup_ids = {x.get("setup_id") for x in events if x.get("type") == "SETUP_DETECTED"}
    setups = [x for x in setups if x.get("setup_id") in setup_ids]
    positions = []
    # Derive the report view from setup-owned opportunity records. This keeps
    # symbol/setup identity correct even for older state records that predate
    # redundant symbol fields on the position object.
    for setup in setups:
        for raw_position in setup.get("opportunities", []):
            position = dict(raw_position)
            # Older ledger entries did not redundantly persist symbol or
            # direction on the opportunity.  The setup is the authoritative
            # owner, so enrich the report view without mutating the ledger.
            position.setdefault("symbol", setup.get("symbol"))
            position.setdefault("direction", setup.get("direction"))
            position.setdefault("setup_id", setup.get("setup_id"))
            position.setdefault("pattern", setup.get("pattern"))
            if _position_is_prospective(position, boundary) and (symbol is None or setup.get("symbol") == symbol):
                positions.append(position)
    closed = [p for p in positions if p.get("status") in ("STOPPED", "TARGET_HIT")]
    open_positions = [p for p in positions if p.get("status") == "OPEN"]
    target_hits = [p for p in closed if p.get("status") == "TARGET_HIT"]
    losses = [p for p in closed if p.get("status") == "STOPPED"]
    rs = [float(p["realized_R"]) for p in closed if p.get("realized_R") is not None]
    wins = [r for r in rs if r > 0]; loss_rs = [r for r in rs if r < 0]; breakeven = [r for r in rs if r == 0]
    geometry = [p.get("geometry", {}) for p in positions if p.get("geometry")]
    target_r = [float(g["target_R"]) for g in geometry if g.get("target_R") is not None and float(g["target_R"]) > 0]
    buckets = {"<0.25R": 0, "0.25-0.50R": 0, "0.50-0.75R": 0, "0.75-1.00R": 0, "1.00-1.50R": 0, ">1.50R": 0}
    for value in target_r:
        key = "<0.25R" if value < .25 else "0.25-0.50R" if value < .50 else "0.50-0.75R" if value < .75 else "0.75-1.00R" if value < 1 else "1.00-1.50R" if value < 1.5 else ">1.50R"
        buckets[key] += 1
    mechanisms = {name: 0 for name in ("DEPTH_ONLY", "REJECTION_WICK", "LOWER_TF_ENGULFING", "MORNING_EVENING_STAR", "EMA_TOUCH_REJECTION", "STRUCTURE_TOUCH_REJECTION")}
    for position in positions:
        for name in position.get("entry_mechanisms", []):
            if name in mechanisms: mechanisms[name] += 1
    reasons = {name: event_counts.get(name, 0) for name in ("NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY", "SETUP_INVALIDATED_BEFORE_ENTRY", "NO_RETRACE", "NO_VALID_ENTRY_SIGNAL", "SAME_OPPORTUNITY_HOVER", "DUPLICATE_ENTRY_OPPORTUNITY", "POSITION_ALREADY_OPEN_SCALEIN_DISABLED", "RETURN_AFTER_SETUP_TARGET_COMPLETED", "THESIS_INVALIDATED")}
    by_symbol = {}
    for name in DEFAULT_SYMBOLS:
        if symbol and name != symbol: continue
        sid = {x.get("setup_id") for x in setups if x.get("symbol") == name}
        ps = [p for p in positions if p.get("symbol") == name or p.get("setup_id") in sid]
        vals = [float(p["realized_R"]) for p in ps if p.get("realized_R") is not None]
        by_symbol[name] = {"setups": sum(1 for x in setups if x.get("symbol") == name), "opportunities": len(ps), "positions": len(ps), "open": sum(p.get("status") == "OPEN" for p in ps), "closed": sum(p.get("status") in ("STOPPED", "TARGET_HIT") for p in ps), "realized_R": sum(vals)}
    shadow = {"registered_hypotheses": {name: 0 for name in ("+1R", "+1.5R", "+2R", "+3R", "LOWER_TF_STRUCTURE_TRAIL", "EMA_STRUCTURE_EXIT", "OPPOSITE_PRICE_ACTION_EXIT")}, "favorable_thresholds_reached": {name: 0 for name in SHADOW_FAVORABLE_THRESHOLDS}, "R_at_time": {f"R@{minutes}m": [] for minutes in (30, 60, 90, 120, 180)}, "scope": "V1_POSITION_MFE_MAE_UNTIL_V1_EXIT", "post_exit_continuation": "UNAVAILABLE_NOT_TRACKED", "position_observations": []}
    for p in positions:
        for name in p.get("leg_b", {}).get("runner_hypotheses", []): shadow["registered_hypotheses"][name] += 1
        observation = _shadow_position_observation(p)
        shadow["position_observations"].append(observation)
        for name, did_reach in observation["thresholds_reached"].items():
            shadow["favorable_thresholds_reached"][name] += int(did_reach)
    # Backward-compatible local alias is intentionally not used in output;
    # registration and actual threshold observations must remain separate.
    shadow["runner_hypotheses"] = shadow.pop("registered_hypotheses")
    mfe_values = [x["mfe_R"] for x in shadow["position_observations"] if x["mfe_R"] is not None]
    mae_values = [x["mae_R"] for x in shadow["position_observations"] if x["mae_R"] is not None]
    shadow["summary"] = {"N": len(shadow["position_observations"]), "closed_N": sum(x["status"] in ("STOPPED", "TARGET_HIT") for x in shadow["position_observations"]), "open_N": sum(x["status"] == "OPEN" for x in shadow["position_observations"]), "median_MFE_R": sorted(mfe_values)[len(mfe_values)//2] if mfe_values else None, "median_MAE_R": sorted(mae_values)[len(mae_values)//2] if mae_values else None, "maximum_MFE_R": max(mfe_values) if mfe_values else None, "maximum_MAE_R": max(mae_values) if mae_values else None}
    meaningful = [x for x in events if x.get("type") not in ("READ_ERROR",)]
    return {"manifest": manifest, "boundary": boundary, "runner_status": state.get("runner_status"), "heartbeat": json.loads(HEARTBEAT.read_text()) if HEARTBEAT.exists() else {}, "event_counts": event_counts, "setups": setups, "positions": positions, "closed": closed, "open": open_positions, "outcomes": {"N": len(closed), "target_hits": len(target_hits), "losses": len(losses), "breakevens": len(breakeven), "open": len(open_positions), "target_hit_rate": _percent(len(target_hits), len(closed)), "profitable_close_rate": _percent(len(wins), len(closed)), "loss_rate": _percent(len(losses), len(closed)), "breakeven_rate": _percent(len(breakeven), len(closed)), "realized_R": sum(rs), "expectancy_R": sum(rs) / len(rs) if rs else None, "profit_factor": sum(wins) / abs(sum(loss_rs)) if loss_rs else None, "max_drawdown_R": _max_drawdown(rs)}, "target_geometry": {"N": len(target_r), "buckets": buckets, "median_target_R": sorted(target_r)[len(target_r)//2] if target_r else None, "median_spread_target_ratio": sorted(float(g["spread_target_ratio"]) for g in geometry if g.get("spread_target_ratio") is not None)[len([g for g in geometry if g.get("spread_target_ratio") is not None])//2] if any(g.get("spread_target_ratio") is not None for g in geometry) else None, "TARGET_NEAR_SPREAD_SCALE": sum("TARGET_NEAR_SPREAD_SCALE" in g.get("flags", []) for g in geometry), "OPPOSING_STRUCTURE_VERY_CLOSE": sum("OPPOSING_STRUCTURE_VERY_CLOSE" in g.get("flags", []) for g in geometry)}, "mechanisms": mechanisms, "reasons": reasons, "by_symbol": by_symbol, "recent_events": meaningful[-10:], "shadow": shadow}


def _age_seconds(iso_ts: Any, now: datetime) -> float | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        return (now - ts).total_seconds()
    except (TypeError, ValueError):
        return None


def _standard_position(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "economic_position_id": p.get("economic_position_id"),
        "setup_id": p.get("setup_id"),
        "symbol": p.get("symbol"),
        "direction": p.get("direction"),
        "entry_time": p.get("fill_timestamp_iso"),
        "entry_price": p.get("executable_paper_entry", p.get("theoretical_entry")),
        "status": p.get("status"),
    }


def _standard_closed_position(p: dict[str, Any]) -> dict[str, Any]:
    row = _standard_position(p)
    row.update({
        "close_time": p.get("close_timestamp_iso"),
        "close_price": p.get("close_price"),
        "outcome": p.get("status"),
        "realized_R": p.get("realized_R"),
        "mfe_R": None,
        "mae_R": None,
        "exit_reason": p.get("exit_reason"),
    })
    return row


def build_standard_report(symbol: str | None = None, recent: int | None = None) -> dict[str, Any]:
    """Standard cross-strategy observability report, shared by the CLI
    `report` subcommand and the Control API. This function has no writes and
    performs no computation report_data() doesn't already own — it only maps
    report_data()'s output (plus a second read-only load_state() call for
    fields report_data() doesn't thread through) into the common contract.
    """
    data = report_data(symbol, recent)
    state = load_state()
    m = data["manifest"]
    hb = data["heartbeat"]
    o = data["outcomes"]
    now = datetime.now(timezone.utc)

    heartbeat_ts = hb.get("timestamp")
    last_read_ts = state.get("last_successful_read_at")

    return {
        "identity": {
            "strategy_id": VERSION,
            "display_name": "Context Structure Retrace V1",
            "strategy_version": None,
            "configuration_version": m.get("configuration_hash"),
            "source_identity": m.get("code_hash"),
            "decision_fingerprint": decision_code_hash(),
            "freeze_identity": {
                "configuration_hash": m.get("configuration_hash"),
                "phase2_representation_hash": m.get("phase2_representation_hash"),
            },
            "freeze_timestamp": m.get("freeze_timestamp"),
            "observability_version": OBSERVABILITY_VERSION,
            "sample_boundary": data["boundary"],
            "observed_at": now.isoformat(),
        },
        "status": {
            "runner_status": data["runner_status"],
            "last_runner_heartbeat": heartbeat_ts,
            "runner_heartbeat_age": _age_seconds(heartbeat_ts, now),
            "bridge_status": None,  # not tracked by this strategy's own report
            "data_status": "LIVE" if last_read_ts else None,
            "last_market_timestamp": last_read_ts,
            "data_age": _age_seconds(last_read_ts, now),
            "kill_switch": STOP_FILE.exists(),
            "observability_timestamp": now.isoformat(),
        },
        "sample": {
            "scope": "FORWARD_PROSPECTIVE",
            "boundary": data["boundary"],
        },
        "lifecycle": [
            {"stage": "detected", "label": "Setups Detected", "count": len(data["setups"])},
            {"stage": "qualified", "label": "Qualified", "count": data["event_counts"].get("SETUP_DETECTED", 0)},
            {"stage": "invalidated", "label": "Invalidated", "count": data["event_counts"].get("SETUP_INVALIDATED_BEFORE_ENTRY", 0)},
            {"stage": "no_retrace", "label": "No Retrace", "count": data["event_counts"].get("NO_RETRACE", 0)},
            {"stage": "entered", "label": "Entry Opportunities", "count": len(data["positions"])},
            {"stage": "open", "label": "Open", "count": len(data["open"])},
            {"stage": "closed", "label": "Closed", "count": len(data["closed"])},
        ],
        "performance": {
            "trades": o["N"],
            "wins": o["target_hits"],
            "losses": o["losses"],
            "breakevens": o["breakevens"],
            "open": o["open"],
            "win_rate": o["target_hit_rate"],
            "loss_rate": o["loss_rate"],
            "breakeven_rate": o["breakeven_rate"],
            "realized_R": o["realized_R"],
            "expectancy_R": o["expectancy_R"],
            "profit_factor": o["profit_factor"],
            "max_drawdown_R": o["max_drawdown_R"],
            # mfe_R / mae_R deliberately absent here: this strategy's own
            # report has no post-exit MFE/MAE concept — see the separate
            # phase7 shadow report (context_structure_retrace_phase7_observer.py)
        },
        "symbols": [
            {
                "symbol": name,
                "detected": row.get("setups"),
                "opportunities": row.get("opportunities"),
                "entries": row.get("opportunities"),
                "open": row.get("open"),
                "closed": row.get("closed"),
                "realized_R": row.get("realized_R"),
            }
            for name, row in data["by_symbol"].items()
        ],
        "open_positions": [_standard_position(p) for p in data["open"]],
        "closed_positions": [_standard_closed_position(p) for p in data["closed"]],
        "rejection_reasons": [
            {"reason_code": name, "display_reason": name.replace("_", " ").title(), "count": count}
            for name, count in data["reasons"].items()
        ],
        "data_quality": {
            "data_status": "LIVE" if last_read_ts else "UNKNOWN",
            "last_market_timestamp": last_read_ts,
            "freshness": _age_seconds(last_read_ts, now),
            "gap_status": "NOT_TRACKED",
            "missing_observations": None,
            "recovered": None,
            "recovery_source": None,
        },
        "recent_activity": [
            {
                "timestamp": row.get("event_time"),
                "strategy_id": VERSION,
                "symbol": row.get("symbol"),
                "event_type": row.get("type"),
                "display_event": str(row.get("type", "")).replace("_", " ").title() or None,
                "direction": row.get("direction"),
                "setup_id": row.get("setup_id"),
                "opportunity_id": row.get("economic_position_id"),
                "economic_position_id": row.get("economic_position_id"),
                "reason_code": row.get("reason"),
                "display_reason": str(row.get("reason") or "").replace("_", " ").title() or None,
                "metadata": row,
            }
            for row in data["recent_events"]
        ],
        "reference_performance": None,  # no historical/validation reference tracked by this strategy
        "extensions": {
            "strategy_id": VERSION,
            "target_geometry": data["target_geometry"],
            "entry_mechanisms": data["mechanisms"],
            "by_symbol_detail": data["by_symbol"],
        },
    }


def print_report(symbol: str | None = None, recent: int | None = None) -> None:
    data = report_data(symbol, recent)
    m = data["manifest"]; o = data["outcomes"]; g = data["target_geometry"]
    hb = data["heartbeat"]
    print(f"{VERSION} — PROSPECTIVE PAPER REPORT (READ-ONLY)")
    print("=" * 72)
    print(f"STRATEGY: {VERSION} | observability: {OBSERVABILITY_VERSION}")
    print(f"Freeze: {data['boundary']} | Runner: {data['runner_status']} | Heartbeat: {hb.get('timestamp', 'N/A')}")
    print(f"Frozen source identity: {m.get('code_hash', 'N/A')} | Config: {m.get('configuration_hash', 'N/A')}")
    print(f"Decision-code fingerprint: {decision_code_hash()} | Phase2: {m.get('phase2_representation_hash', 'N/A')}")
    print(f"\nSAMPLE (prospective only, boundary {data['boundary']})")
    print(f"Setups: {len(data['setups'])} | Qualified: {data['event_counts'].get('SETUP_DETECTED', 0)} | Invalidated: {data['event_counts'].get('SETUP_INVALIDATED_BEFORE_ENTRY', 0)} | No retrace: {data['event_counts'].get('NO_RETRACE', 0)}")
    print(f"Entry opportunities: {len(data['positions'])} | Economic positions: {len(data['positions'])} | Closed: {len(data['closed'])} | Open: {len(data['open'])}")
    print(f"Re-entries: {sum(p.get('reentry_type') == 'REENTRY_BEFORE_TARGET_COMPLETION' for p in data['positions'])} | Scale-ins blocked: {data['event_counts'].get('POTENTIAL_SCALE_IN', 0)} | Returns after target: {data['event_counts'].get('RETURN_AFTER_SETUP_TARGET_COMPLETED', 0)} | No remaining target: {data['event_counts'].get('NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY', 0)}")
    print("\nBY SYMBOL")
    for name, row in data["by_symbol"].items(): print(f"{name}: setups={row['setups']} opportunities={row['opportunities']} positions={row['positions']} open={row['open']} closed={row['closed']} realized_R={_fmt(row['realized_R'])}")
    def position_line(position: dict[str, Any]) -> str:
        entry = position.get("executable_paper_entry", position.get("theoretical_entry", "N/A"))
        stop = position.get("stop", "N/A")
        target = position.get("target", "N/A")
        realized = position.get("realized_R")
        realized_text = "OPEN" if realized is None else f"{float(realized):+.3f}R"
        return (f"{position.get('symbol', 'N/A')} | {position.get('direction', 'N/A'):5} | "
                f"{position.get('fill_timestamp_iso', 'N/A')} | entry={entry} | stop={stop} | "
                f"target={target} | result={realized_text} | status={position.get('status', 'N/A')}")
    print("\nOPEN POSITIONS")
    if data["open"]:
        for position in data["open"]: print(position_line(position))
    else:
        print("None")
    print("\nCLOSED POSITIONS")
    if data["closed"]:
        for position in data["closed"]: print(position_line(position))
    else:
        print("None")
    print(f"\nOUTCOMES (N={o['N']} closed; open={o['open']})")
    print(f"Target hits: {o['target_hits']} | Losses: {o['losses']} | Breakevens: {o['breakevens']} | Open: {o['open']}")
    print(f"Target-hit rate: {o['target_hit_rate']} | Profitable-close rate: {o['profitable_close_rate']} | Loss rate: {o['loss_rate']} | Breakeven rate: {o['breakeven_rate']}")
    print(f"Realized R: {_fmt(o['realized_R'])} | Expectancy R: {_fmt(o['expectancy_R'])} | Profit factor: {_fmt(o['profit_factor'])} | Max DD: {_fmt(o['max_drawdown_R'])}")
    print("\nTARGET GEOMETRY (valid economic opportunities)")
    print(" | ".join(f"{k}: {v}" for k, v in g["buckets"].items()))
    print(f"Median target R: {_fmt(g['median_target_R'])} | Median spread/target: {_fmt(g['median_spread_target_ratio'])} | Near-spread: {g['TARGET_NEAR_SPREAD_SCALE']} | Very-close structure: {g['OPPOSING_STRUCTURE_VERY_CLOSE']}")
    print("\nENTRY MECHANISMS (descriptive counts; economic N is not multiplied)")
    print(" | ".join(f"{k}: {v}" for k, v in data["mechanisms"].items()))
    print("\nREJECTIONS / NON-TRADES")
    for k, v in data["reasons"].items(): print(f"{k}: {v}")
    print("\nSHADOW — NOT PART OF FROZEN V1 ENTRY DECISIONS")
    print("SHADOW RUNNER HYPOTHESES REGISTERED")
    print(", ".join(f"{k}={v}" for k, v in data["shadow"]["runner_hypotheses"].items()))
    print("SHADOW FAVORABLE-EXCURSION THRESHOLDS ACTUALLY REACHED")
    print(", ".join(f"{k}={v}" for k, v in data["shadow"]["favorable_thresholds_reached"].items()))
    ss = data["shadow"]["summary"]
    print(f"Shadow MFE/MAE scope: V1_POSITION_MFE_MAE_UNTIL_V1_EXIT | N={ss['N']} closed={ss['closed_N']} open={ss['open_N']}")
    print(f"Median MFE: {_fmt(ss['median_MFE_R'])}R | Median MAE: {_fmt(ss['median_MAE_R'])}R | Maximum MFE: {_fmt(ss['maximum_MFE_R'])}R | Maximum MAE: {_fmt(ss['maximum_MAE_R'])}R")
    print("Shadow post-exit continuation: N/A — not tracked by the frozen runner")
    print("R@30m/R@60m/R@90m/R@120m/R@180m: N/A until forward duration observations exist")
    print("\nRECENT ACTIVITY")
    for row in data["recent_events"]:
        print(f"{row.get('event_time','N/A')} | {row.get('symbol','N/A')} | {row.get('type','N/A')} | setup={row.get('setup_id','N/A')} | reason={row.get('reason', row.get('exit_reason',''))}")



def run(args: argparse.Namespace) -> None:
    manifest = assert_frozen(); acquire_lock(); STOP_FILE.unlink(missing_ok=True)
    state = load_state(); state["runner_status"] = "ACTIVE"; state["poll_interval_seconds"] = args.interval; state["prospective_boundary"] = manifest["freeze_timestamp"]; save_state(state); write_heartbeat(state)
    # Outcome persistence is a post-checkpoint projection. It never runs in
    # _process_bar() or participates in frozen ENTRY_ONLY decisions.
    _project_entry_only_outcomes(state)
    stopping = {"value": False}
    last_membership_refresh = 0.0
    membership_symbols = tuple(args.symbols)
    membership_revision = None
    def stop_handler(signum: int, frame: Any) -> None:
        stopping["value"] = True
    signal.signal(signal.SIGINT, stop_handler); signal.signal(signal.SIGTERM, stop_handler)
    try:
        while not stopping["value"] and not STOP_FILE.exists():
            if time.monotonic() - last_membership_refresh >= MEMBERSHIP_REFRESH_SECONDS:
                membership_symbols, membership_revision = load_active_membership(args)
                state["instrument_membership_revision"] = membership_revision
                state["instrument_membership_symbols"] = list(membership_symbols)
                last_membership_refresh = time.monotonic()
            poll(state, membership_symbols, args.mcp_url, args.limit)
            _project_entry_only_outcomes(state)
            if args.once: break
            for _ in range(max(1, args.interval)):
                if stopping["value"] or STOP_FILE.exists(): break
                time.sleep(1)
    finally:
        state["runner_status"] = "STOPPED"; state["stopped_at"] = now_iso(); save_state(state); write_heartbeat(state, "STOPPED"); SUMMARY.write_text(summary(state) + "\n", encoding="utf-8"); release_lock()
        print("CONTEXT_STRUCTURE_RETRACE_V1 paper runner stopped cleanly")


def _project_entry_only_outcomes(state: dict[str, Any]) -> None:
    """Best-effort projection after a poll; a PG outage never changes strategy decisions."""
    if not os.getenv("ENTRY_OUTCOME_SIGNAL_CUTOFF_ID"):
        return
    try:
        from context_structure_retrace_outcome_projector import project_entry_only_outcomes
        # Project from the lifecycle authority (setup-held positions, as project_state defines
        # it), not the in-memory positions map: after load_state() those are separate objects,
        # and exits recorded on the setup-held position would otherwise stay invisible to the
        # canonical outcome table until the next runner restart.
        result = project_entry_only_outcomes(project_state(state))
        print(f"ENTRY_ONLY_OUTCOME_PROJECTION {json.dumps(result, sort_keys=True)}")
    except Exception as exc:
        # The next completed poll retries. The frozen runner remains live and
        # its already-checkpointed state remains authoritative during an outage.
        print(f"ENTRY_ONLY_OUTCOME_PROJECTION_UNAVAILABLE {type(exc).__name__}: {exc}")


def stop() -> None:
    STOP_FILE.write_text(now_iso(), encoding="utf-8"); print("Stop requested; paper runner will shut down after its current read cycle.")


def order_isolation_audit() -> dict[str, Any]:
    source = (ROOT / "context_structure_retrace_forward.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name): calls.append(node.func.id)
    forbidden_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "bridge_read":
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value not in READ_ONLY_BRIDGE_TOOLS:
                forbidden_calls.append(node.args[0].value)
    return {"read_only_tools": sorted(READ_ONLY_BRIDGE_TOOLS), "non_readonly_bridge_calls": sorted(forbidden_calls), "call_bridge_imported": "call_bridge" in calls or "call_bridge" in source, "order_submission_code_path": False}


def main() -> None:
    p = argparse.ArgumentParser(description="CONTEXT_STRUCTURE_RETRACE_V1 isolated paper-only runner")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("freeze")
    sub.add_parser("status")
    sub.add_parser("health")
    sub.add_parser("stop")
    sub.add_parser("audit-order-isolation")
    r = sub.add_parser("report"); r.add_argument("--symbol", choices=DEFAULT_SYMBOLS); r.add_argument("--recent", type=int)
    s = sub.add_parser("start"); s.add_argument("--interval", type=int, default=15); s.add_argument("--limit", type=int, default=320); s.add_argument("--once", action="store_true"); s.add_argument("--mcp-url", default="http://127.0.0.1:22347/mcp"); s.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    args = p.parse_args()
    if args.command == "freeze": print(json.dumps(freeze(), indent=2)); return
    if args.command in ("status", "health"): status(); return
    if args.command == "report": print(format_standard_report(build_standard_report(args.symbol, args.recent))); return
    if args.command == "stop": stop(); return
    if args.command == "audit-order-isolation": print(json.dumps(order_isolation_audit(), indent=2)); return
    run(args)


if __name__ == "__main__": main()
