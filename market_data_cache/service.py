"""Market-data collector process:  python -m market_data_cache.service

Environment:
    MARKET_DATA_BRIDGE_URL     read-only bridge /mcp endpoint (22347; 22348 is refused)
    MARKET_DATA_REDIS_URL      Redis for the cache
    TRADING_POSTGRES_DSN / PG*  read-only: instrument memberships, provider mappings, open trades
    MARKET_QUOTE_SYMBOLS       extra comma-separated provider symbols to keep quotes hot
    MARKET_DATA_FULL_LIMIT=321  MARKET_DATA_INCREMENTAL_LIMIT=8  MARKET_QUOTE_REFRESH_SECONDS=2
    MARKET_QUOTES_PER_PASS=10   MARKET_METADATA_REFRESH_SECONDS=300

Bar symbols: every ACTIVE strategy instrument membership with an ACTIVE MT5 mapping. Quote
symbols: instruments of OPEN managed trades plus MARKET_QUOTE_SYMBOLS. Metadata symbols: the V2
risk policy's allowed symbols. It only reads the bridge and PostgreSQL.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any

log = logging.getLogger("market_data_cache")


def main() -> None:
    import redis

    from contracts.mt5_bridge import BridgeEndpoint, Mt5ReadClient
    from postgres.config import PostgresConfig
    from postgres.db import connect
    from .collector import MarketDataCollector
    from .store import MarketDataStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = os.environ["MARKET_DATA_BRIDGE_URL"].strip()
    if ":22348" in url:
        raise SystemExit("market data must never be read from the execution bridge (22348)")
    client = Mt5ReadClient(BridgeEndpoint(url, profile="research"))
    store = MarketDataStore(redis.Redis.from_url(os.environ["MARKET_DATA_REDIS_URL"], socket_timeout=5.0))
    pg = PostgresConfig.from_env()

    def rows(sql: str, params: tuple[Any, ...] = ()) -> list[str]:
        with connect(pg, readonly=True) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]

    def bar_symbols() -> list[str]:
        return rows("""SELECT DISTINCT p.provider_symbol FROM strategy.instrument_membership m
                       JOIN platform.instrument_provider_mapping p
                         ON p.canonical_instrument = m.canonical_instrument AND p.provider = 'MT5' AND p.state = 'ACTIVE'
                       WHERE m.state = 'ACTIVE' ORDER BY 1""")

    def quote_symbols() -> list[str]:
        extra = [s.strip() for s in os.getenv("MARKET_QUOTE_SYMBOLS", "").split(",") if s.strip()]
        return extra + rows("""SELECT DISTINCT p.provider_symbol FROM trade_management.managed_trade t
                               JOIN platform.instrument_provider_mapping p
                                 ON p.canonical_instrument = t.instrument AND p.provider = 'MT5' AND p.state = 'ACTIVE'
                               WHERE t.state = 'OPEN' ORDER BY 1""")

    def metadata_symbols() -> list[str]:
        return rows("""SELECT DISTINCT p.provider_symbol FROM execution_v2.risk_policy_allowed_symbol a
                       JOIN platform.instrument_provider_mapping p
                         ON p.canonical_instrument = a.symbol AND p.provider = 'MT5' AND p.state = 'ACTIVE'
                       WHERE a.policy_id = 'current' ORDER BY 1""")

    cached: dict[str, tuple[float, list[str]]] = {}

    def every(name: str, fn, seconds: float = 60.0):
        def wrapped() -> list[str]:
            at, value = cached.get(name, (0.0, []))
            if time.time() - at >= seconds:
                try:
                    value = fn()
                    cached[name] = (time.time(), value)
                except Exception:
                    log.exception("symbol list %s unavailable; keeping the previous list", name)
            return value
        return wrapped

    collector = MarketDataCollector(
        store, lambda tool, args: client.call(tool, args),
        bar_symbols=every("bars", bar_symbols), quote_symbols=every("quotes", quote_symbols, 15.0),
        metadata_symbols=every("metadata", metadata_symbols, 300.0),
        full_limit=int(os.getenv("MARKET_DATA_FULL_LIMIT", "321")),
        incremental_limit=int(os.getenv("MARKET_DATA_INCREMENTAL_LIMIT", "8")),
        quote_interval=float(os.getenv("MARKET_QUOTE_REFRESH_SECONDS", "2")),
        max_quotes_per_pass=int(os.getenv("MARKET_QUOTES_PER_PASS", "10")),
        metadata_interval=float(os.getenv("MARKET_METADATA_REFRESH_SECONDS", "300")))
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    while not stop["now"]:
        result = collector.tick()
        if result["bars"] or result["failing"]:
            log.info("bars fetched=%d full=%d failing=%s commands_total=%d", len(result["bars"]),
                     sum(1 for r in result["bars"] if r.get("full")), result["failing"], collector.commands)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
