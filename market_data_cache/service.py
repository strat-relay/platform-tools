from __future__ import annotations

import os
import time


def main() -> None:
    import redis
    from contracts.mt5_bridge import BridgeEndpoint, Mt5ReadClient
    from .collector import MarketDataCollector
    from .store import MarketDataStore

    client = Mt5ReadClient(BridgeEndpoint(os.environ["MARKET_DATA_BRIDGE_URL"], profile="research"))
    store = MarketDataStore(redis.Redis.from_url(os.environ["MARKET_DATA_REDIS_URL"], socket_timeout=5.0))
    symbols = [s.strip() for s in os.getenv("MARKET_DATA_SYMBOLS", "XAUUSDm,BTCUSDm,USDJPYm,EURUSDm").split(",") if s.strip()]
    collector = MarketDataCollector(store, lambda tool, args: client.call(tool, args), bar_symbols=lambda: symbols)
    while True:
        collector.tick()
        time.sleep(1.0)


if __name__ == "__main__":
    main()
