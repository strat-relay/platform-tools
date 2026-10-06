"""Risk-state collector process:  python -m execution_v2.risk_state.collector_service

Environment:
    V2_EXECUTION_ACCOUNT_ID, V2_EXECUTION_BRIDGE_MODE, V2_BROKER_SYMBOL_MAP_JSON   (as the execution runtime)
    V2_READ_BRIDGE_URL        read-only bridge /mcp endpoint (22347); the collector only reads
    RISK_REDIS_URL            Redis for hot risk state
    RISK_FAST_REFRESH_SECONDS=10  RISK_HISTORY_REFRESH_SECONDS=30  RISK_REFERENCE_REFRESH_SECONDS=300
    TRADING_POSTGRES_DSN / PG*  read-only: allowed symbols, provider mappings, attempt states

It never submits, fences or writes to the broker, and never writes PostgreSQL.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

log = logging.getLogger("execution_v2.risk_state.collector")


def _seconds(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def main() -> None:
    import redis

    from postgres.config import PostgresConfig
    from postgres.db import connect
    from ..risk_policy_store import read_effective_policy
    from ..runtime.bridge_client import HttpBridgeFenceClient
    from ..runtime.service import platform_broker_tickets
    from ..symbols import SymbolMappingError, catalog_symbol_lookup, resolve_broker_symbol
    from .collector import RiskStateCollector, attempt_states_from_postgres
    from .snapshot import account_ref
    from .store import RedisRiskStateStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    account_id = os.environ["V2_EXECUTION_ACCOUNT_ID"].strip()
    mode = os.getenv("V2_EXECUTION_BRIDGE_MODE", "demo").strip().lower()
    read_url = os.environ["V2_READ_BRIDGE_URL"].strip()
    pg = PostgresConfig.from_env()
    connect_fn = lambda *, readonly=True: connect(pg, readonly=readonly)  # noqa: E731
    lookup = catalog_symbol_lookup(connect_fn)
    reader = HttpBridgeFenceClient(base_url=read_url, read_base_url=read_url,
                                   execution_mode=f"{mode.upper()}_EXECUTION", timeout_s=15.0)
    store = RedisRiskStateStore(redis.Redis.from_url(os.environ["RISK_REDIS_URL"], socket_timeout=5.0),
                                account_ref(account_id))

    def broker_symbol(canonical: str) -> str:
        return resolve_broker_symbol(canonical, account_id=account_id, mode=mode, catalog_lookup=lookup)

    def reference_symbols() -> list[str]:
        policy, _ = read_effective_policy(connect_fn)
        out = []
        for canonical in policy.allowed_symbols or ():
            try:
                out.append(broker_symbol(canonical))
            except SymbolMappingError:
                log.warning("no broker mapping for allowed symbol %s; its reference metadata is not collected", canonical)
        return out

    reverse: dict[str, str] = {}

    def canonical_for(provider_symbol: str) -> str | None:
        if provider_symbol not in reverse:
            with connect_fn() as conn, conn.cursor() as cur:
                cur.execute("""SELECT canonical_instrument FROM platform.instrument_provider_mapping
                               WHERE provider = 'MT5' AND provider_symbol = %s AND state = 'ACTIVE'""", (provider_symbol,))
                row = cur.fetchone()
            reverse[provider_symbol] = row[0] if row else None
        return reverse[provider_symbol]

    def read_tool(tool: str, arguments: dict[str, Any]) -> Any:
        return reader._read_tool(tool, arguments)

    collector = RiskStateCollector(
        store, read_tool, canonical_for=canonical_for, reference_symbols=reference_symbols,
        owned_tickets=lambda: platform_broker_tickets(connect_fn, account_id),
        fast_interval=_seconds("RISK_FAST_REFRESH_SECONDS", 10.0),
        history_interval=_seconds("RISK_HISTORY_REFRESH_SECONDS", 30.0),
        reference_interval=_seconds("RISK_REFERENCE_REFRESH_SECONDS", 300.0))
    attempt_states = attempt_states_from_postgres(connect_fn)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    log.info("risk-state collector started for account %s", store.ref)
    while not stop["now"]:
        ran = collector.tick()
        try:
            changes = collector.reconcile_reservations(attempt_states)
            if changes:
                log.info("reservations reconciled: %s", changes)
        except Exception:
            log.exception("reservation reconciliation failed; reservations keep holding capacity")
        if ran:
            log.info("collected %s -> health %s", ran, collector.health.get("status"))
        time.sleep(1.0)


if __name__ == "__main__":
    main()
