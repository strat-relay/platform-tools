"""CONTEXT_STRUCTURE_RETRACE_V2 forward runner.

V1 remains the sealed historical/runtime identity.  V2 reuses the same causal
decision primitives but has an independent manifest, state directory, schema,
and fingerprint.  It must be explicitly enabled by a V2 strategy instance; it
never mutates or reuses V1's state or freeze manifest.
"""
from __future__ import annotations

import hashlib
import importlib.util
import argparse
import json
import os
from pathlib import Path
from datetime import datetime, timezone

from context_structure_retrace_v2 import V2_CONTRACT_HASH, V2_PARAMETER_HASH
from context_structure_retrace_v2_config import DEFAULTS, INSTANCE_ID, load_from_database


def _load_runtime_module():
    """Load the shared primitives into a private module namespace.

    Mutating the imported V1 module would make a V1 and V2 runner in the same
    process share identity globals. Separate module state keeps the identities
    isolated while retaining the exact causal implementation.
    """
    source = Path(__file__).with_name("context_structure_retrace_forward.py")
    spec = importlib.util.spec_from_file_location("_context_structure_retrace_v2_base", source)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Context runner primitives from {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_v1 = _load_runtime_module()


VERSION = "CONTEXT_STRUCTURE_RETRACE_V2"
SCHEMA_VERSION = "context-structure-retrace-forward-v2-schema-1"
V2_STATE_DIR_ENV = "CONTEXT_V2_RUNNER_STATE_DIR"

# A new identity is intentional: the underlying causal primitives are reused,
# but this runner is not allowed to claim V1's sealed source identity.
V2_DECISION_FINGERPRINT = hashlib.sha256(
    f"{_v1.FROZEN_DECISION_CODE_HASH}|{V2_CONTRACT_HASH}|{V2_PARAMETER_HASH}|{VERSION}".encode()
).hexdigest()

PARAMETER_VALUES = dict(DEFAULTS)
PARAMETER_IDENTITY: dict[str, object] = {
    "parameter_set_id": "context-v2-runtime-default",
    "parameter_fingerprint": "LOCAL_DEFAULTS",
    "revision": 0,
    "source": "LOCAL_DEFAULTS",
}


def _source_hash() -> str:
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(_v1.__file__)):
        digest.update(str(path.name).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _configure_runtime() -> None:
    """Configure the imported runner module in its own V2 namespace."""
    global PARAMETER_VALUES, PARAMETER_IDENTITY
    if getattr(_v1, "_v2_runtime_configured", False):
        state_dir = Path(os.environ.get(V2_STATE_DIR_ENV) or (Path(_v1.ROOT) / "context_v2_runtime"))
        state_dir.mkdir(parents=True, exist_ok=True)
        _v1.STATE_DIR = state_dir
        _v1.MANIFEST = state_dir / "context_structure_retrace_v2_forward_manifest.json"
        PARAMETER_VALUES, PARAMETER_IDENTITY = load_from_database()
        return
    state_dir = Path(__import__("os").environ.get(V2_STATE_DIR_ENV) or
                     (Path(_v1.ROOT) / "context_v2_runtime"))
    _v1.VERSION = VERSION
    _v1.OBSERVABILITY_VERSION = "phase7-v2-report-1"
    _v1.SCHEMA_VERSION = SCHEMA_VERSION
    _v1.STATE_DIR = state_dir
    _v1.LEGACY_STATE = state_dir / "context_structure_retrace_v2_forward_state.json"
    _v1.STATE = state_dir / "context_structure_retrace_v2_forward_state_compact.json"
    _v1.EVENTS = state_dir / "context_structure_retrace_v2_forward.jsonl"
    _v1.HEARTBEAT = state_dir / "context_structure_retrace_v2_forward.heartbeat.json"
    _v1.PID = state_dir / "context_structure_retrace_v2_forward.pid"
    _v1.MANIFEST = state_dir / "context_structure_retrace_v2_forward_manifest.json"
    _v1.SUMMARY = state_dir / "context_structure_retrace_v2_forward_summary.md"
    state_dir.mkdir(parents=True, exist_ok=True)
    _v1.FROZEN_CONFIG = {**_v1.FROZEN_CONFIG,
                         "strategy_version": VERSION,
                         "derived_from": "CONTEXT_STRUCTURE_RETRACE_V1@V1",
                         "v2_contract_hash": V2_CONTRACT_HASH,
                         "v2_parameter_hash": V2_PARAMETER_HASH,
                         "lifecycle": "RESEARCH_ONLY",
                         "broker_order_submission": False}
    _v1.LEGACY_FROZEN_SOURCE_HASH = _source_hash()
    _v1.FROZEN_DECISION_CODE_HASH = V2_DECISION_FINGERPRINT

    PARAMETER_VALUES, PARAMETER_IDENTITY = load_from_database()

    original_detect_patterns = _v1.detect_patterns

    def configured_detect_patterns(*args, **kwargs):
        events = original_detect_patterns(*args, **kwargs)
        timeframe = args[1] if len(args) > 1 else kwargs.get("timeframe")
        if timeframe != "M15":
            return events
        enabled = set(PARAMETER_VALUES.get("enabled_setup_events") or [])
        return [event for event in events if event.get("pattern") in enabled]

    _v1.detect_patterns = configured_detect_patterns

    def configured_geometry(event_bar, direction, snapshot, entry, spread, atr_value):
        lo, hi = float(event_bar["low"]), float(event_bar["high"])
        rng = max(hi - lo, 1e-12)
        buffer = max(float(atr_value or 0) * float(PARAMETER_VALUES["stop_atr_buffer_fraction"]),
                     spread * float(PARAMETER_VALUES["stop_spread_buffer_multiplier"]))
        stop_ref = lo if direction == "LONG" else hi
        stop = stop_ref - buffer if direction == "LONG" else stop_ref + buffer
        extension_fraction = float(PARAMETER_VALUES["target_extension_fraction"])
        extension = hi + extension_fraction * rng if direction == "LONG" else lo - extension_fraction * rng
        structure = snapshot["timeframes"][snapshot["provenance"]["structure_timeframe"]]["sr_context"]
        zones = structure.get("zones", [])
        if direction == "LONG":
            candidates = [float(z["zone_low"]) for z in zones
                          if z.get("support_resistance_role") == "RESISTANCE" and float(z["zone_low"]) > entry]
            opposing = min(candidates) if candidates else None
            if PARAMETER_VALUES["target_selection_policy"] == "OPPOSING_STRUCTURE_ONLY" and opposing is None:
                target_candidates = []
            else:
                target_candidates = ([extension] if PARAMETER_VALUES["target_selection_policy"] == "EXTENSION_ONLY" else
                                     ([opposing] if PARAMETER_VALUES["target_selection_policy"] == "OPPOSING_STRUCTURE_ONLY" else
                                      [extension] + ([opposing] if opposing is not None else [])))
            if not target_candidates:
                risk = entry - stop
                return {"structural_reference": stop_ref, "stop": stop, "stop_distance": risk,
                        "extension_target": extension, "opposing_structure": opposing,
                        "effective_target": entry, "signed_target_distance": 0.0,
                        "target_direction_state": "TARGET_AT_ENTRY", "target_R": 0.0,
                        "spread_target_ratio": None, "spread_stop_ratio": spread / risk if risk > 0 else None,
                        "flags": ["NO_OPPOSING_STRUCTURE_TARGET"]}
            target = min(target_candidates)
            signed = target - entry
        else:
            candidates = [float(z["zone_high"]) for z in zones
                          if z.get("support_resistance_role") == "SUPPORT" and float(z["zone_high"]) < entry]
            opposing = max(candidates) if candidates else None
            if PARAMETER_VALUES["target_selection_policy"] == "OPPOSING_STRUCTURE_ONLY" and opposing is None:
                target_candidates = []
            else:
                target_candidates = ([extension] if PARAMETER_VALUES["target_selection_policy"] == "EXTENSION_ONLY" else
                                     ([opposing] if PARAMETER_VALUES["target_selection_policy"] == "OPPOSING_STRUCTURE_ONLY" else
                                      [extension] + ([opposing] if opposing is not None else [])))
            if not target_candidates:
                risk = stop - entry
                return {"structural_reference": stop_ref, "stop": stop, "stop_distance": risk,
                        "extension_target": extension, "opposing_structure": opposing,
                        "effective_target": entry, "signed_target_distance": 0.0,
                        "target_direction_state": "TARGET_AT_ENTRY", "target_R": 0.0,
                        "spread_target_ratio": None, "spread_stop_ratio": spread / risk if risk > 0 else None,
                        "flags": ["NO_OPPOSING_STRUCTURE_TARGET"]}
            target = max(target_candidates)
            signed = entry - target
        risk = entry - stop if direction == "LONG" else stop - entry
        state = "TARGET_BEYOND_ENTRY" if signed > 0 else "TARGET_AT_ENTRY" if signed == 0 else "TARGET_BEHIND_ENTRY"
        return {"structural_reference": stop_ref, "stop": stop, "stop_distance": risk,
                "extension_target": extension, "opposing_structure": opposing,
                "effective_target": target, "signed_target_distance": signed,
                "target_direction_state": state, "target_R": signed / risk if risk > 0 else None,
                "spread_target_ratio": spread / signed if signed > 0 else None,
                "spread_stop_ratio": spread / risk if risk > 0 else None,
                "flags": (["OPPOSING_STRUCTURE_VERY_CLOSE"] if signed > 0 and risk and signed / risk < 0.10 else [])}

    _v1._geometry = configured_geometry

    def configured_make_setup(symbol, event, event_bar, snapshot, quote, contract):
        direction = event["direction"]
        rng = float(event_bar["high"]) - float(event_bar["low"])
        fraction = float(PARAMETER_VALUES["retracement_entry_fraction"])
        theoretical = (float(event_bar["close"]) - fraction * rng if direction == "LONG"
                       else float(event_bar["close"]) + fraction * rng)
        spread = _v1._spread(event_bar, contract, quote)
        executable_reference = theoretical + spread / 2 if direction == "LONG" else theoretical - spread / 2
        atr_value = snapshot["timeframes"]["M15"]["ema_context"].get("atr")
        geometry = _v1._geometry(event_bar, direction, snapshot, executable_reference, spread, atr_value)
        context = _v1._context(snapshot, direction)
        setup_id = _v1._event_id(symbol, event)
        market_event = hashlib.sha256(f"{symbol}|{event['timestamp']}|{event['pattern']}".encode()).hexdigest()[:20]
        return {"setup_id": setup_id, "market_event_id": market_event, "symbol": symbol, "direction": direction,
                "pattern": event["pattern"], "setup_timestamp": int(event["timestamp"]), "setup_timestamp_iso": _v1.iso(int(event["timestamp"])),
                "event": event, "event_bar": event_bar, "context_snapshot": snapshot, "context_components": context,
                "qualification": "QUALIFIED_FOR_RETRACE_MONITORING", "qualification_flags": context["flags"],
                "retrace_state": "WAITING_FOR_RETRACEMENT", "theoretical_entry": theoretical, "entry_level": theoretical,
                "geometry_at_reference": geometry, "spread_at_detection": spread, "mechanisms": [],
                "opportunities": [], "target_completed": False, "status": "WAITING_FOR_RETRACE",
                "provenance": {"as_of_timestamp": int(event["timestamp"]), "completed_candles_only": True, "future_ohlc_exposed": False,
                               "source": "LIVE_FORWARD", "phase2_representation_hash": _v1.PHASE2_HASH}}

    _v1.make_setup = configured_make_setup

    def configured_load_active_membership(args):
        dsn = os.getenv("TRADING_POSTGRES_DSN") or os.getenv("DATABASE_URL")
        if not dsn:
            return tuple(args.symbols), None
        try:
            import psycopg
            with psycopg.connect(dsn, autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("""SELECT p.provider_symbol, p.revision
                        FROM strategy_mgmt.strategy_instance_v2 i
                        CROSS JOIN LATERAL jsonb_array_elements(i.instruments) AS member
                        JOIN platform.instrument_provider_mapping p
                          ON p.canonical_instrument = member->>'canonical_instrument'
                         AND p.provider = 'MT5' AND p.state = 'ACTIVE'
                       WHERE i.online = true
                         AND (i.attributes->>'instance_id' = %s OR i.id::text = %s)
                         AND member->>'state' = 'ACTIVE'
                       ORDER BY member->>'canonical_instrument'""", (INSTANCE_ID, INSTANCE_ID))
                    rows = cur.fetchall()
            return tuple(row[0] for row in rows), max((int(row[1]) for row in rows), default=None)
        except Exception:
            return tuple(args.symbols), None

    _v1.load_active_membership = configured_load_active_membership

    original_process_bar = _v1._process_bar

    def configured_process_bar(state, symbol, bar, index, m5, contract, quote, source):
        """Keep the shared lifecycle while applying V2's configured M5 window."""
        window = int(PARAMETER_VALUES["max_retrace_candles"])
        adjusted: list[tuple[dict, object]] = []
        if window != 12:
            for setup in state.get("setups", {}).values():
                if setup.get("symbol") != symbol or setup.get("status") != "WAITING_FOR_RETRACE":
                    continue
                if setup.get("m5_start_timestamp") is None:
                    continue
                original = setup["m5_start_timestamp"]
                setup["m5_start_timestamp"] = int(original) + (window - 12) * 300
                adjusted.append((setup, original))
        try:
            original_process_bar(state, symbol, bar, index, m5, contract, quote, source)
        finally:
            for setup, original in adjusted:
                setup["m5_start_timestamp"] = original

    _v1._process_bar = configured_process_bar
    _v1._v2_runtime_configured = True

    def digest_files() -> str:
        return _source_hash()

    def decision_code_hash() -> str:
        return V2_DECISION_FINGERPRINT

    _v1.digest_files = digest_files
    _v1.decision_code_hash = decision_code_hash
    _v1._prospective_boundary = _prospective_boundary


def runtime_identity() -> dict:
    """Persist current V2 identity without freezing or rejecting later changes."""
    _configure_runtime()
    existing = {}
    if _v1.MANIFEST.exists():
        existing = json.loads(_v1.MANIFEST.read_text(encoding="utf-8"))
    activation = existing.get("activation_timestamp") or datetime.now(timezone.utc).isoformat()
    manifest = {
        "strategy_version": VERSION,
        "activation_timestamp": activation,
        "code_hash": _source_hash(),
        "configuration_hash": _v1.config_hash(),
        "schema_version": SCHEMA_VERSION,
        "phase2_representation_hash": _v1.PHASE2_HASH,
        "configuration": _v1.FROZEN_CONFIG,
        "parameter_set": PARAMETER_VALUES,
        "parameter_identity": PARAMETER_IDENTITY,
        "identity_mode": "RUNTIME_MUTABLE",
        "lifecycle": "RESEARCH_ONLY",
        "broker_order_submission": False,
        "execution_isolation": {"mode": "PAPER_READ_ONLY",
                                 "allowed_bridge_tools": sorted(_v1.READ_ONLY_BRIDGE_TOOLS),
                                 "broker_order_submission": False},
    }
    _v1.atomic_json(_v1.MANIFEST, manifest)
    return manifest


def _prospective_boundary(manifest: dict) -> str:
    return str(manifest.get("activation_timestamp", ""))


def run(args: argparse.Namespace) -> None:
    """V2 runner loop with a runtime identity, not a freeze assertion."""
    global PARAMETER_VALUES, PARAMETER_IDENTITY
    manifest = runtime_identity()
    _v1.acquire_lock()
    _v1.STOP_FILE.unlink(missing_ok=True)
    # V2 owns a separate state directory and may be starting for the first
    # time. V1's loader intentionally refuses to bootstrap missing compact
    # state because falling back to its legacy state would be unsafe; V2 must
    # initialize its own empty state instead.
    state = _v1.load_state() if _v1.STATE.exists() else _v1.empty_state()
    state["runner_status"] = "ACTIVE"
    state["poll_interval_seconds"] = args.interval
    state["prospective_boundary"] = manifest["activation_timestamp"]
    _v1.save_state(state)
    _v1.write_heartbeat(state)
    _v1._project_entry_only_outcomes(state)
    stopping = {"value": False}
    last_membership_refresh = 0.0
    membership_symbols = tuple(args.symbols)
    membership_revision = None

    def stop_handler(_signum: int, _frame: object) -> None:
        stopping["value"] = True

    import signal
    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)
    try:
        next_cycle = __import__("time").monotonic()
        while not stopping["value"] and not _v1.STOP_FILE.exists():
            cycle_started = __import__("time").monotonic()
            if __import__("time").monotonic() - last_membership_refresh >= _v1.MEMBERSHIP_REFRESH_SECONDS:
                # ParameterSets are immutable; an operator change is a new set bound
                # to the instance. Refreshing at the same cadence as membership keeps
                # the runner responsive without a database query on every bar.
                PARAMETER_VALUES, PARAMETER_IDENTITY = load_from_database()
                membership_symbols, membership_revision = _v1.load_active_membership(args)
                state["instrument_membership_revision"] = membership_revision
                state["instrument_membership_symbols"] = list(membership_symbols)
                last_membership_refresh = __import__("time").monotonic()
            _v1.poll(state, membership_symbols, args.mcp_url, args.limit)
            _v1._project_entry_only_outcomes(state)
            if args.once:
                break
            next_cycle = cycle_started + max(0.25, float(args.interval))
            delay = next_cycle - __import__("time").monotonic()
            __import__("time").sleep(max(0.25, float(args.interval)) if delay <= 0 else delay)
    finally:
        state["runner_status"] = "STOPPED"
        state["stopped_at"] = _v1.now_iso()
        _v1.save_state(state)
        _v1.write_heartbeat(state, "STOPPED")
        _v1.SUMMARY.write_text(_v1.summary(state) + "\n", encoding="utf-8")
        _v1.release_lock()
        print("CONTEXT_STRUCTURE_RETRACE_V2 research runner stopped cleanly")


def main() -> None:
    _configure_runtime()
    parser = argparse.ArgumentParser(description="CONTEXT_STRUCTURE_RETRACE_V2 research-only runner")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("health")
    sub.add_parser("stop")
    sub.add_parser("audit-order-isolation")
    report = sub.add_parser("report")
    report.add_argument("--symbol", choices=_v1.DEFAULT_SYMBOLS)
    report.add_argument("--recent", type=int)
    start = sub.add_parser("start")
    start.add_argument("--interval", type=float, default=1)
    start.add_argument("--limit", type=int, default=320)
    start.add_argument("--once", action="store_true")
    start.add_argument("--mcp-url", default="http://127.0.0.1:22347/mcp")
    start.add_argument("--symbols", nargs="+", default=list(_v1.DEFAULT_SYMBOLS))
    args = parser.parse_args()
    if args.command in ("status", "health"):
        _v1.status()
    elif args.command == "stop":
        _v1.stop()
    elif args.command == "audit-order-isolation":
        print(json.dumps(_v1.order_isolation_audit(), indent=2))
    elif args.command == "report":
        print(_v1.format_standard_report(_v1.build_standard_report(args.symbol, args.recent)))
    else:
        run(args)


if __name__ == "__main__":
    main()
