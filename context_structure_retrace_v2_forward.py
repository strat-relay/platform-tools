"""CONTEXT_STRUCTURE_RETRACE_V2 forward runner.

V1 remains the sealed historical/runtime identity.  V2 reuses the same causal
decision primitives but has an independent manifest, state directory, schema,
and fingerprint.  It must be explicitly enabled by a V2 strategy instance; it
never mutates or reuses V1's state or freeze manifest.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

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


def freeze() -> dict:
    _configure_runtime()
    return _v1.freeze()


def main() -> None:
    _configure_runtime()
    _v1.main()


if __name__ == "__main__":
    main()
