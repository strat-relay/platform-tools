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


def _source_hash() -> str:
    digest = hashlib.sha256()
    for path in (Path(__file__), Path(_v1.__file__)):
        digest.update(str(path.name).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _configure_runtime() -> None:
    """Configure the imported runner module in its own V2 namespace."""
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
    _v1.FROZEN_CONFIG = {**_v1.FROZEN_CONFIG,
                         "strategy_version": VERSION,
                         "derived_from": "CONTEXT_STRUCTURE_RETRACE_V1@V1",
                         "v2_contract_hash": V2_CONTRACT_HASH,
                         "v2_parameter_hash": V2_PARAMETER_HASH,
                         "lifecycle": "RESEARCH_ONLY",
                         "broker_order_submission": False}
    _v1.LEGACY_FROZEN_SOURCE_HASH = _source_hash()
    _v1.FROZEN_DECISION_CODE_HASH = V2_DECISION_FINGERPRINT

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
    manifest = runtime_identity()
    _v1.acquire_lock()
    _v1.STOP_FILE.unlink(missing_ok=True)
    state = _v1.load_state()
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
