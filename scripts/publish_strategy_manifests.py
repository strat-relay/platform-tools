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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_api.strategy_manifest_publisher import publish  # noqa: E402,F401  (re-exported for callers)
from strategy_manifests import all_manifests  # noqa: E402


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
