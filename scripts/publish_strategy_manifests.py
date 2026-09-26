"""Publish strategy parameter manifests (strategy_manifests.py) to PostgreSQL (migration 034).

Dry run by default: prints what would change. With --apply, upserts the StrategyVersion manifests
and the ParameterSet of every instance row that exists; instances without a row are skipped (this
never creates instances). Idempotent: unchanged manifests are left untouched.

    python -m scripts.publish_strategy_manifests [--apply] [--published-by NAME]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_manifests import all_manifests, fingerprint  # noqa: E402


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--published-by", default="publish_strategy_manifests")
    args = parser.parse_args(argv)
    from postgres.config import PostgresConfig
    from postgres.db import connect
    result = publish(all_manifests(), apply=args.apply, connect_fn=lambda: connect(PostgresConfig.from_env()),
                     published_by=args.published_by)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
