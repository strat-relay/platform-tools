"""Registers a frozen TmVersion + a legacy_stream_binding for every strategy whose definition in
platform.strategy_definition (migration 028) carries its own `trade_management` block.

    "trade_management": {
        "policy": "tm-breakeven-trail.v1",
        "label": "TM-BREAKEVEN-TRAIL-1-CONTEXT-DEFAULT",
        "params": {"breakeven_trigger_r": 1.0, "trail_trigger_r": 1.5, "trail_distance_r": 0.5}
    }

A strategy with no `trade_management` block is untouched and keeps falling back to
DEFAULT_TM_NONE (trade_management/binding.py) - this script only ever ADDS a binding, never
removes or edits an existing one (legacy_stream_binding rows are append-only facts, matching this
package's own convention: "A later change to legacy_stream_binding affects only trades opened
afterwards").

Idempotent and safe to re-run: the TmVersion insert is ON CONFLICT DO NOTHING (a given
policy+params tuple always hashes to the same tm_version_id, so re-running with identical config
is a no-op), and the binding insert is skipped outright if an identical binding_id already
exists.

Every registered version is explicitly recorded SHADOW_ONLY in tm_version_promotion (mirroring
migration 014's own seed for TM-NONE-1) - this script has no path that could ever mark a version
PUBLISHABLE, matching trade_management/publication_gate.py's own default.

Does NOT touch EXECUTION_AUTHORITY_MODE, risk policy, or any execution/broker table - this is
trade_management schema only. Does NOT run automatically; an operator runs this explicitly,
the same way scripts/seed_v2_risk_policy.py is only ever run explicitly.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from postgres.db import connect
from trade_management.ids import binding_id as _binding_id
from trade_management.versions import TmVersionManifest, tm_breakeven_trail_manifest


# Extend this as new evaluators are built - each entry maps a config `policy` string to a
# manifest builder. Keeps this script from silently accepting a policy name no evaluator actually
# implements (trade_management/decision_engine.py would fail closed with UnknownEvaluator at
# decision time otherwise - better to catch it here, before anything is frozen/bound).
_MANIFEST_BUILDERS = {
    "tm-breakeven-trail.v1": lambda params, label: tm_breakeven_trail_manifest(
        breakeven_trigger_r=params["breakeven_trigger_r"], trail_trigger_r=params["trail_trigger_r"],
        trail_distance_r=params["trail_distance_r"], label=label),
}


class SeedError(RuntimeError):
    pass


def _manifest_for(strategy: dict[str, Any]) -> TmVersionManifest:
    config = strategy["trade_management"]
    policy = config["policy"]
    builder = _MANIFEST_BUILDERS.get(policy)
    if builder is None:
        raise SeedError(f"{strategy['strategy_id']}: unknown trade_management.policy {policy!r} "
                        f"(known: {sorted(_MANIFEST_BUILDERS)})")
    label = config.get("label", policy)
    return builder(config.get("params", {}), label)


def _ensure_version(cur: Any, manifest: TmVersionManifest, *, decided_by: str) -> str:
    tm_version_id = manifest.tm_version_id()
    cur.execute(
        """INSERT INTO trade_management.trade_manager_version
               (tm_version_id, evaluator_id, label, manifest, manifest_hash, status)
           VALUES (%s,%s,%s,%s::jsonb,%s,'FROZEN')
           ON CONFLICT (tm_version_id) DO NOTHING""",
        (tm_version_id, manifest.evaluator_id, manifest.label,
         json.dumps(manifest.identity_manifest(), sort_keys=True), manifest.manifest_hash()))
    cur.execute(
        """INSERT INTO trade_management.tm_version_promotion
               (tm_version_id, publication_eligibility, decided_by, evidence_ref)
           SELECT %s, 'SHADOW_ONLY', %s, NULL
           WHERE NOT EXISTS (
               SELECT 1 FROM trade_management.tm_version_promotion WHERE tm_version_id = %s)""",
        (tm_version_id, decided_by, tm_version_id))
    return tm_version_id


def _ensure_binding(cur: Any, *, strategy_id: str, tm_version_id: str, valid_from: str) -> str:
    binding_id = _binding_id(strategy_id=strategy_id, strategy_instance_id=None, instrument=None,
                             tm_version_id=tm_version_id, resolution="LEGACY_STATIC", valid_from=valid_from)
    binding_hash = binding_id  # matches DefaultTmNoneResolver's own convention: this binding_id
    # already is a content hash of every field that identifies it, so no separate hash is needed.
    cur.execute(
        """INSERT INTO trade_management.legacy_stream_binding
               (binding_id, strategy_id, strategy_instance_id, instrument, tm_version_id,
                valid_from, binding_hash)
           VALUES (%s,%s,NULL,NULL,%s,%s,%s)
           ON CONFLICT (binding_id) DO NOTHING""",
        (binding_id, strategy_id, tm_version_id, valid_from, binding_hash))
    return binding_id


def _configured_from_database(cur: Any) -> list[dict[str, Any]]:
    """Strategies whose definition (platform.strategy_definition, migration 028) carries a
    trade_management policy block."""
    cur.execute("""SELECT strategy_id, trade_management FROM platform.strategy_definition
                   WHERE trade_management IS NOT NULL ORDER BY strategy_id""")
    rows = []
    for strategy_id, policy in cur.fetchall():
        rows.append({"strategy_id": strategy_id,
                     "trade_management": json.loads(policy) if isinstance(policy, str) else policy})
    return rows


def seed(*, config_path: Path | None = None, valid_from: str, decided_by: str,
         connect_fn: Any = connect) -> list[dict[str, str]]:
    """Policies come from the database by default. `config_path` is an explicit one-off override
    (e.g. importing a legacy platform.json); there is no implicit file read."""
    results: list[dict[str, str]] = []
    with connect_fn() as conn:
        with conn.cursor() as cur:
            if config_path is not None:
                strategies = json.loads(Path(config_path).read_text(encoding="utf-8"))["strategies"]
                configured = [s for s in strategies if "trade_management" in s]
            else:
                configured = _configured_from_database(cur)
            for strategy in configured:
                manifest = _manifest_for(strategy)
                tm_version_id = _ensure_version(cur, manifest, decided_by=decided_by)
                binding_id = _ensure_binding(cur, strategy_id=strategy["strategy_id"],
                                             tm_version_id=tm_version_id, valid_from=valid_from)
                results.append({"strategy_id": strategy["strategy_id"], "tm_version_id": tm_version_id,
                               "binding_id": binding_id, "label": manifest.label})
        conn.commit()
    return results


def main() -> None:
    import argparse
    import sys
    from datetime import datetime, timezone

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--valid-from", default=None,
                        help="ISO-8601 timestamp bindings take effect from (default: now)")
    parser.add_argument("--decided-by", default="script:seed_tm_bindings")
    args = parser.parse_args()
    valid_from = args.valid_from or datetime.now(timezone.utc).isoformat()

    try:
        results = seed(valid_from=valid_from, decided_by=args.decided_by)
    except SeedError as exc:
        print(f"SEED_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps({"seeded": results, "valid_from": valid_from}, indent=2))


if __name__ == "__main__":
    main()
