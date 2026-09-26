"""Safe workload entry point for a future Liquidity live evaluator deployment.

The default is fail-closed.  The workload cannot start from an accidental image
environment unless an explicit enable flag, canonical DB cutoff, and read-only
22347 bridge URL are supplied.  This module does not contain a broker client.
"""
from __future__ import annotations

import os
import time
from typing import Any

from liquidity_live_runtime import build_runtime
from liquidity_market_data import build_liquidity_market_data
from postgres.config import PostgresConfig
from postgres.db import connect


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Liquidity live runtime requires {name}")
    return value


def run_once() -> dict[str, Any]:
    if os.environ.get("LIQUIDITY_LIVE_RUNTIME_ENABLED", "false").lower() != "true":
        raise RuntimeError("Liquidity live runtime is disabled")
    mcp_url = _required("LIQUIDITY_LIVE_MCP_URL")
    if ":22348" in mcp_url:
        raise RuntimeError("Liquidity live runtime accepts read-only bridge 22347, never 22348")
    cutoff_id = _required("SIGNAL_CUTOFF_ID")
    cutoff_utc = _required("SIGNAL_CUTOFF_UTC")
    market_data = build_liquidity_market_data(mcp_url)
    with connect(PostgresConfig.from_env()) as conn:
        runtime = build_runtime(conn=conn, cutoff_id=cutoff_id, cutoff_utc=cutoff_utc,
                                snapshot_reader=market_data.snapshot)
        runtime.publisher.require_schema()
        return runtime.tick()


def main() -> None:
    interval = max(1.0, float(os.environ.get("LIQUIDITY_LIVE_POLL_SECONDS", "5")))
    while True:
        run_once()
        time.sleep(interval)


if __name__ == "__main__":
    main()
