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
from observability.strategy_audit import audit, configure_strategy_audit_logging


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Liquidity live runtime requires {name}")
    return value


def check_configuration() -> None:
    """Configuration errors stop the process (fail closed); they are checked before the loop."""
    if os.environ.get("LIQUIDITY_LIVE_RUNTIME_ENABLED", "false").lower() != "true":
        raise RuntimeError("Liquidity live runtime is disabled")
    source = os.environ.get("MARKET_DATA_SOURCE", "BRIDGE").strip().upper()
    if source != "REDIS":
        mcp_url = _required("LIQUIDITY_LIVE_MCP_URL")
        if ":22348" in mcp_url:
            raise RuntimeError("Liquidity live runtime accepts read-only bridge 22347, never 22348")
    _required("SIGNAL_CUTOFF_ID")
    _required("SIGNAL_CUTOFF_UTC")


def run_once() -> dict[str, Any]:
    check_configuration()
    mcp_url = os.environ.get("LIQUIDITY_LIVE_MCP_URL", "").strip()
    cutoff_id = _required("SIGNAL_CUTOFF_ID")
    cutoff_utc = _required("SIGNAL_CUTOFF_UTC")
    market_data = build_liquidity_market_data(mcp_url)
    with connect(PostgresConfig.from_env()) as conn:
        runtime = build_runtime(conn=conn, cutoff_id=cutoff_id, cutoff_utc=cutoff_utc,
                                snapshot_reader=market_data.snapshot)
        runtime.publisher.require_schema()
        return runtime.tick()


def main() -> None:
    configure_strategy_audit_logging()
    audit("runner_started", runner="liquidity-live", broker_writes=0)
    interval = max(1.0, float(os.environ.get("LIQUIDITY_LIVE_POLL_SECONDS", "5")))
    check_configuration()
    runner_consecutive_failures = 0
    while True:
        # One failed cycle (a bridge/cache timeout, a transient database error) must not kill the
        # process: nothing is published from a failed tick, and the next tick starts clean.
        try:
            run_once()
            runner_consecutive_failures = 0
        except Exception as exc:  # noqa: BLE001
            runner_consecutive_failures += 1
            audit("tick_failed", runner="liquidity-live", error=f"{type(exc).__name__}: {exc}"[:300],
                  runner_consecutive_failures=runner_consecutive_failures,
                  market_data_consecutive_failures=0)
        time.sleep(interval * min(2 ** max(runner_consecutive_failures - 1, 0), 12))


if __name__ == "__main__":
    main()
