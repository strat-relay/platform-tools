"""Market-data collector health contract (md:health), and a probe:  python -m market_data_cache.health

md:health fields (written by MarketDataCollector._publish):
    process_heartbeat_at    every write; writes happen between bridge calls, so during a long pass its
                            age is bounded by one bridge call
    pass_started_at         when the current / most recent pass began (stable during the pass)
    last_progress_at        last successful bridge fetch stored in Redis (bars, quotes or metadata)
    last_completed_pass_at  when a whole pass finished
    in_pass, current_symbol, symbols_completed_in_pass, symbols_total_in_pass, current_pass_duration
    status / unhealthy_symbols  data health of the bar symbols (unchanged meaning)

Two separate answers:
    PROCESS health  "is the collector itself advancing?"  HEALTHY while the heartbeat is younger than
                    MARKET_DATA_LIVENESS_SECONDS (180 s: several worst-case bridge calls), else STALLED.
                    A slow 62-symbol catch-up pass stays HEALTHY.
    DATA health     HEALTHY / DEGRADED (some bar symbol's last fetch failed) / UNKNOWN (no pass yet).
PROCESS=HEALTHY with DATA=DEGRADED is the normal catch-up state. Strategy freshness requirements are
not defined here; readers keep their own max ages.

The CLI exits 0 when PROCESS is HEALTHY and 1 otherwise, so it can back a liveness probe or alert.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

DEFAULT_LIVENESS_SECONDS = 180.0


def _age(now: float, value: Any) -> float | None:
    return None if value is None else round(now - float(value), 1)


def evaluate(health: dict[str, Any] | None, now: float,
             liveness_seconds: float = DEFAULT_LIVENESS_SECONDS) -> dict[str, Any]:
    health = health or {}
    heartbeat = health.get("process_heartbeat_at")
    if heartbeat is None:
        process = "UNKNOWN"
    else:
        process = "HEALTHY" if now - float(heartbeat) <= liveness_seconds else "STALLED"
    data = {"healthy": "HEALTHY", "degraded": "DEGRADED"}.get(health.get("status"), "UNKNOWN")
    return {"process_health": process, "data_health": data,
            "heartbeat_age_seconds": _age(now, heartbeat),
            "last_progress_age_seconds": _age(now, health.get("last_progress_at")),
            "last_completed_pass_age_seconds": _age(now, health.get("last_completed_pass_at")),
            "in_pass": bool(health.get("in_pass")), "current_symbol": health.get("current_symbol"),
            "pass_progress": [health.get("symbols_completed_in_pass"), health.get("symbols_total_in_pass")],
            "current_pass_duration": health.get("current_pass_duration"),
            "unhealthy_symbols": len(health.get("unhealthy_symbols") or []),
            "liveness_seconds": liveness_seconds}


def main() -> int:
    import redis

    from .store import MarketDataStore

    store = MarketDataStore(redis.Redis.from_url(os.environ["MARKET_DATA_REDIS_URL"], socket_timeout=5.0))
    result = evaluate(store.health(), time.time(),
                      float(os.getenv("MARKET_DATA_LIVENESS_SECONDS", str(DEFAULT_LIVENESS_SECONDS))))
    print(json.dumps(result))
    return 0 if result["process_health"] == "HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
