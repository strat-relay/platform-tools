"""Entrypoint for the unified post-emission outcome resolver.

The service is deliberately fail-closed.  A deployment must explicitly opt into
primary ownership; without that flag it cannot write outcome state or rows.
"""
from __future__ import annotations

import argparse
import json
import os
import time

from outcome_resolver_runtime import OutcomeResolverRuntime, ResolverRedisCandleStore
from postgres.config import PostgresConfig
from postgres.db import connect


def _primary_enabled() -> bool:
    return (os.environ.get("OUTCOME_RESOLVER_ENABLED", "false").lower() == "true"
            and os.environ.get("OUTCOME_RESOLVER_PRIMARY", "false").lower() == "true")


def run_once(*, limit: int = 100) -> dict[str, object]:
    if not _primary_enabled():
        raise RuntimeError("Outcome Resolver primary ownership is disabled")
    import redis

    cfg = PostgresConfig.from_env()
    cfg.require_explicit_target()
    redis_url = os.environ.get("MARKET_DATA_REDIS_URL")
    if not redis_url:
        raise RuntimeError("MARKET_DATA_REDIS_URL is required")
    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5)
    with connect(cfg) as conn:
        runtime = OutcomeResolverRuntime(
            conn=conn,
            candle_store=ResolverRedisCandleStore(client),
            holder_id=os.environ.get("OUTCOME_RESOLVER_HOLDER_ID"),
        )
        return runtime.tick(limit=limit)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            print(json.dumps(run_once(limit=args.limit), sort_keys=True), flush=True)
        except Exception as exc:  # fail closed, next cycle can recover
            print(json.dumps({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        if args.once:
            return
        time.sleep(max(0.25, args.interval))


if __name__ == "__main__":
    main()
