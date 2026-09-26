"""Publish strategy parameter manifests to PostgreSQL (migration 034), without strategy code.

Manifests are generated from strategy code (strategy_manifests.py) and bundled with the API as
`platform_api/strategy_manifests.json` (tests fail when the bundle drifts from the code), so the
Control API publishes exactly the manifests of the commit it was built from. The runtime-image
script (scripts/publish_strategy_manifests.py) publishes the same content from code directly.

Publishing only upserts the manifest tables for strategies/instances that already exist; it
never creates instances or changes lifecycle, membership, execution or risk state.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

BUNDLE_PATH = Path(__file__).with_name("strategy_manifests.json")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def bundled_manifests() -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    if not BUNDLE_PATH.exists():
        return []
    return [(entry["manifest"], entry["parameter_sets"]) for entry in json.loads(BUNDLE_PATH.read_text(encoding="utf-8"))]


def to_bundle(manifests: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> str:
    entries = [{"manifest": m, "parameter_sets": sorted(sets, key=lambda p: p["instance_id"])}
               for m, sets in sorted(manifests, key=lambda x: x[0]["strategy_id"])]
    return json.dumps(entries, indent=2, sort_keys=True, default=str) + "\n"


def publish(manifests: list[tuple[dict[str, Any], list[dict[str, Any]]]], *, apply: bool,
            connect_fn: Callable[[], Any], published_by: str) -> dict[str, Any]:
    report: dict[str, list[str]] = {"versions_changed": [], "parameter_sets_changed": [],
                                    "skipped_no_strategy": [], "skipped_no_instance": []}
    with connect_fn() as conn:
        with conn.cursor() as cur:
            for manifest, sets in manifests:
                sid, version = manifest["strategy_id"], manifest["strategy_version"]
                cur.execute("SELECT 1 FROM platform.strategy_definition WHERE strategy_id = %s", (sid,))
                if cur.fetchone() is None:
                    report["skipped_no_strategy"].append(sid)
                    continue
                digest = fingerprint(manifest)
                cur.execute("""SELECT manifest_fingerprint FROM platform.strategy_version_manifest
                               WHERE strategy_id = %s AND strategy_version = %s""", (sid, version))
                row = cur.fetchone()
                if row is None or row[0] != digest:
                    report["versions_changed"].append(f"{sid}@{version}")
                    cur.execute("""INSERT INTO platform.strategy_version_manifest
                            (strategy_id, strategy_version, manifest, manifest_fingerprint, published_by)
                            VALUES (%s, %s, %s::jsonb, %s, %s)
                            ON CONFLICT (strategy_id, strategy_version) DO UPDATE SET
                              manifest = EXCLUDED.manifest, manifest_fingerprint = EXCLUDED.manifest_fingerprint,
                              published_at = now(), published_by = EXCLUDED.published_by""",
                                (sid, version, json.dumps(manifest), digest, published_by))
                for p in sets:
                    cur.execute("SELECT 1 FROM platform.strategy_instance WHERE strategy_id = %s AND instance_id = %s",
                                (sid, p["instance_id"]))
                    if cur.fetchone() is None:
                        report["skipped_no_instance"].append(p["instance_id"])
                        continue
                    cur.execute("""SELECT parameter_set_id, config_fingerprint, parameters
                                   FROM platform.strategy_instance_parameter_set WHERE instance_id = %s""",
                                (p["instance_id"],))
                    current = cur.fetchone()
                    if current is not None and (current[0], current[1], current[2]) == (
                            p["parameter_set_id"], p["config_fingerprint"], p["parameters"]):
                        continue
                    report["parameter_sets_changed"].append(p["instance_id"])
                    cur.execute("""INSERT INTO platform.strategy_instance_parameter_set
                            (instance_id, strategy_id, strategy_version, parameter_set_id, config_fingerprint,
                             parameters, published_by)
                            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                            ON CONFLICT (instance_id) DO UPDATE SET
                              strategy_version = EXCLUDED.strategy_version,
                              parameter_set_id = EXCLUDED.parameter_set_id,
                              config_fingerprint = EXCLUDED.config_fingerprint, parameters = EXCLUDED.parameters,
                              published_at = now(), published_by = EXCLUDED.published_by""",
                                (p["instance_id"], sid, version, p["parameter_set_id"], p["config_fingerprint"],
                                 json.dumps(p["parameters"]), published_by))
        if apply:
            conn.commit()
        else:
            conn.rollback()
    return {"dry_run": not apply, **report}
