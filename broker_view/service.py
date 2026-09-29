"""Broker-view populator:  python -m broker_view.service

Every BROKER_VIEW_REFRESH_SECONDS (15) it reads each allow-listed read-only broker tool once from
the read bridge and stores the answer in Redis. It is the only broker-view writer, and it never
calls anything outside platform_api.control.READ_ONLY_BROKER_TOOLS.

Environment:
    BROKER_VIEW_BRIDGE_URL          read-only bridge /mcp endpoint (22347; 22348 is refused)
    BROKER_VIEW_REDIS_URL           Redis for the view
    BROKER_VIEW_REFRESH_SECONDS=15  BROKER_VIEW_BRIDGE_TIMEOUT_SECONDS=10
"""
from __future__ import annotations

import logging
import os
import signal
import time
from typing import Any, Callable

from .store import BrokerViewStore

log = logging.getLogger("broker_view")


def unique_reads(tools: dict[str, tuple[str, dict[str, Any] | None]]) -> list[tuple[str, dict[str, Any] | None]]:
    """(tool, arguments) pairs, each once, in declaration order (history-orders and deals share one)."""
    seen: list[tuple[str, dict[str, Any] | None]] = []
    for pair in tools.values():
        if pair not in seen:
            seen.append(pair)
    return seen


def populate_once(bridge: Any, store: BrokerViewStore, reads: list[tuple[str, dict[str, Any] | None]], *,
                  clock: Callable[[], float] = time.time) -> dict[str, Any]:
    ok: list[str] = []
    errors: dict[str, str] = {}
    for tool, arguments in reads:
        try:
            data = bridge.call(tool, arguments)
        except OSError as exc:  # transport failure (timeout, refused): the bridge is unreachable
            errors[tool] = f"{type(exc).__name__}: {exc}"
            for rest, _ in reads[reads.index((tool, arguments)) + 1:]:
                errors.setdefault(rest, "skipped: bridge unreachable this pass")
            break
        except Exception as exc:  # noqa: BLE001 - one bad tool never blocks the others
            errors[tool] = f"{type(exc).__name__}: {exc}"
            continue
        store.put(tool, arguments, data, observed_at=clock())
        ok.append(tool)
    health = {"status": "healthy" if not errors else ("degraded" if ok else "unavailable"),
              "updated_at": clock(), "ok": ok, "errors": errors}
    store.put_health(health)
    return health


def main() -> None:
    import redis

    from platform_api.control import READ_ONLY_BROKER_TOOLS, ReadOnlyBridgeReader

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    url = os.environ["BROKER_VIEW_BRIDGE_URL"].strip()
    if ":22348" in url:
        raise SystemExit("the broker view must never read the execution bridge (22348)")
    interval = float(os.getenv("BROKER_VIEW_REFRESH_SECONDS", "15"))
    bridge = ReadOnlyBridgeReader(url, timeout=float(os.getenv("BROKER_VIEW_BRIDGE_TIMEOUT_SECONDS", "10")))
    store = BrokerViewStore(redis.Redis.from_url(os.environ["BROKER_VIEW_REDIS_URL"], socket_timeout=5.0))
    reads = unique_reads(READ_ONLY_BROKER_TOOLS)

    stopping = False

    def stop(*_: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("broker view populator: %d reads every %gs from %s", len(reads), interval, url)
    while not stopping:
        started = time.monotonic()
        try:
            health = populate_once(bridge, store, reads)
            log.info("broker view %s ok=%s errors=%s", health["status"], health["ok"], health["errors"])
        except Exception:  # noqa: BLE001 - Redis hiccup: keep the loop alive
            log.exception("broker view pass failed")
        deadline = started + interval
        while not stopping and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


if __name__ == "__main__":
    main()
