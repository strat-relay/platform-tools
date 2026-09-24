"""TradeManagerVersion identity: manifest schema, canonicalisation, hash, and TM-NONE-1.

A management decision must be reproducible against the frozen TradeManager that emitted it
(A6 07, A7 04). Every member of `identity_manifest` is hashed; `tm_version_id` is derived from
that hash so a version's id *is* a commitment to its behaviour. Nothing here reads or writes the
legacy `trade_manager/` package's `PolicyRegistry` at runtime - TM-NONE never resolves a policy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.strategies.evaluation import canonical_bytes, canonical_hash

from .ids import tm_version_id as _tm_version_id

MANIFEST_SCHEMA = "tm-version-manifest.v1"
DECISION_SCHEMA_VERSION = "trade-manager-decision.v1"
ACTION_VOCABULARY_VERSION = "tm-actions.v1"
REASON_CODE_REGISTRY_VERSION = "tm-reasons.v1"

ACTIONS = ("HOLD", "MOVE_STOP", "MOVE_TO_BREAKEVEN", "TRAIL_STOP", "PARTIAL_PROFIT", "EXIT")


@dataclass(frozen=True)
class TmVersionManifest:
    """Every field is hashed (A7 04 section 1). `label`/`description` are metadata, kept
    alongside but never fed into the hash - see `identity_manifest()`."""
    evaluator_id: str
    label: str
    policy_bundle: tuple[Any, ...] = field(default_factory=tuple)
    resolution_table: tuple[Any, ...] = field(default_factory=tuple)
    observation_spec: dict[str, Any] = field(default_factory=dict)
    price_semantics: dict[str, Any] = field(default_factory=dict)
    arithmetic: dict[str, Any] = field(default_factory=dict)
    code_manifest: dict[str, str] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=lambda: {"strategies": ["*"], "instruments": ["*"]})

    def identity_manifest(self) -> dict[str, Any]:
        """The hashed payload. Excludes `label`/`description`/timestamps/author/environment/
        host/`runtime_instance_id`/transport/promotion-state, all of which must not make
        identical behaviour look different (A6 07 section 3)."""
        return {
            "manifest_schema": MANIFEST_SCHEMA,
            "evaluator_id": self.evaluator_id,
            "decision_schema_version": DECISION_SCHEMA_VERSION,
            "action_vocabulary_version": ACTION_VOCABULARY_VERSION,
            "reason_code_registry_version": REASON_CODE_REGISTRY_VERSION,
            "policy_bundle": list(self.policy_bundle),
            "resolution_table": list(self.resolution_table),
            "observation_spec": self.observation_spec,
            "price_semantics": self.price_semantics,
            "arithmetic": self.arithmetic,
            "code_manifest": self.code_manifest,
            "scope": self.scope,
        }

    def manifest_hash(self) -> str:
        return canonical_hash(self.identity_manifest())

    def tm_version_id(self) -> str:
        return _tm_version_id(self.manifest_hash())

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(self.identity_manifest())


# --------------------------------------------------------------------------------------
# TM-NONE-1: the first canonical, frozen version. All management disabled; every observation
# yields HOLD. It decides nothing about management; it proves the pipeline (A6 07 section 5,
# A7 04 section 2).
# --------------------------------------------------------------------------------------

TM_NONE_1_MANIFEST = TmVersionManifest(
    evaluator_id="tm-none.v1",
    label="TM-NONE-1",
    policy_bundle=(),
    resolution_table=(),
    observation_spec={
        "timeframes_consumed": [],
        "ema_period": None, "swing_lookback_left": None, "swing_lookback_right": None,
        "ema_tolerance": None, "structure_tolerance": None,
        "max_market_age_ms": 60_000, "late_event_rule": "IGNORE_NEVER_EVALUATE",
        "quote_required": True,
    },
    price_semantics={"version": "ps.v1", "reference": "close side = bid for LONG / ask for SHORT; mark = mid"},
    arithmetic={"r_multiple_definition": "(price-entry)/|entry-initial_stop| signed by direction",
                "rounding": "none; no derived-float rounding performed by TM-NONE"},
    code_manifest={"trade_management/tm_none.py": "computed-at-registration"},
    scope={"strategies": ["*"], "instruments": ["*"]},
)


def tm_none_1_manifest_with_code_hash(module_source: bytes) -> TmVersionManifest:
    """Replace the placeholder `code_manifest` entry with the real hash of the running
    `tm_none.py` module bytes, so the version id commits to the code that will evaluate it
    (A6 07 section 4 "runtime check"). Kept as a function rather than a module-import cycle:
    `tm_none.py` imports `versions.py`, not the reverse."""
    import hashlib
    digest = hashlib.sha256(module_source).hexdigest()
    return TmVersionManifest(
        evaluator_id=TM_NONE_1_MANIFEST.evaluator_id, label=TM_NONE_1_MANIFEST.label,
        policy_bundle=TM_NONE_1_MANIFEST.policy_bundle, resolution_table=TM_NONE_1_MANIFEST.resolution_table,
        observation_spec=TM_NONE_1_MANIFEST.observation_spec, price_semantics=TM_NONE_1_MANIFEST.price_semantics,
        arithmetic=TM_NONE_1_MANIFEST.arithmetic, code_manifest={"trade_management/tm_none.py": digest},
        scope=TM_NONE_1_MANIFEST.scope,
    )


TM_NONE_1_LABEL = "TM-NONE-1"
