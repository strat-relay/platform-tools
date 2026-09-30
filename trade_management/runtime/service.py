"""Wires every P4.2 runtime component into ONE supervised process (mission section
"ONE RUNTIME WORKLOAD IF PRACTICAL"): the ManagedTrade opening consumer, the observation
scheduler loop, and the TM-NONE decision consumer all run as concurrent `asyncio` tasks in this
single process/pod, sharing one PostgreSQL connection and one NATS/JetStream connection -
matching the existing single-connection-per-process convention already used by
`scripts/p2_shadow/worker.py` and `scripts/signal_outbox_relay.py`, not a new pattern.

No broker/execution component of any kind is started here. `main()` fails closed
(`RuntimeConfigError`) before connecting to anything if configuration is missing or unsafe.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from postgres.db import connect
from trade_management.binding import ChainedResolver, DefaultTmNoneResolver, LegacyStaticResolver
from trade_management.market_data import MarketDataProvider
from trade_management.versions import TM_NONE_1_MANIFEST

from .config import DECISION_CONSUMER_NAME, OPEN_CONSUMER_NAME, RuntimeConfig
from .health import HealthState, start_health_server
from .market_data_live import LiveMarketDataProvider, build_bridge_client
from .observation_runtime import observation_tick
from .open_consumer_runtime import bootstrap_open_consumer, make_open_consumer, subscribe_open_consumer
from .streams import ensure_p4_streams, verify_trading_core_unchanged
from .tm_none_runtime import bootstrap_tm_none_consumer, subscribe_tm_none_consumer

log = logging.getLogger("trade_management.runtime")

TM_NONE_1_VERSION_ID = TM_NONE_1_MANIFEST.tm_version_id()


class RuntimeContext:
    """Everything `run()` needs, split out so tests can construct it with fakes without going
    through `main()`'s real NATS/PostgreSQL/bridge connections."""

    def __init__(self, *, conn: Any, js: Any, provider: MarketDataProvider, config: RuntimeConfig,
                health: HealthState) -> None:
        self.conn = conn
        self.js = js
        self.provider = provider
        self.config = config
        self.health = health


async def observation_loop(ctx: RuntimeContext, stop: asyncio.Event) -> None:
    from infrastructure.messaging.jetstream import JetStreamPublisher
    publisher = JetStreamPublisher(ctx.js)
    ctx.health.mark_ready("observation_loop")
    while not stop.is_set():
        try:
            summary = await observation_tick(ctx.conn, publisher, ctx.provider)
            log.info("observation_tick %s", summary)
        except Exception:
            log.exception("observation_tick failed; will retry next interval")
        try:
            await asyncio.wait_for(stop.wait(), timeout=ctx.config.observation_interval_seconds)
        except TimeoutError:
            pass


async def bootstrap(ctx: RuntimeContext) -> dict[str, Any]:
    """All one-time, idempotent setup: verify TRADING_CORE, ensure TRADING_OBSERVATION,
    establish (or load) the activation boundary, bootstrap both durable consumers."""
    await verify_trading_core_unchanged(ctx.js)
    await ensure_p4_streams(ctx.js)
    boundary = await bootstrap_open_consumer(ctx.js, ctx.conn, consumer_name=OPEN_CONSUMER_NAME)
    ctx.health.mark_ready("activation_boundary", detail=boundary)
    await bootstrap_tm_none_consumer(ctx.js, consumer_name=DECISION_CONSUMER_NAME)
    return boundary


async def run(ctx: RuntimeContext, stop: asyncio.Event) -> None:
    await bootstrap(ctx)

    # Resolve configured strategy bindings at ManagedTrade creation time. TM-NONE remains
    # the fail-closed fallback for strategies without an explicit binding.
    resolver = ChainedResolver([
        LegacyStaticResolver(),
        DefaultTmNoneResolver(tm_version_id=TM_NONE_1_VERSION_ID),
    ])
    open_consumer = make_open_consumer(lambda: ctx.conn, resolver, consumer_name=OPEN_CONSUMER_NAME)
    await subscribe_open_consumer(ctx.js, open_consumer, consumer_name=OPEN_CONSUMER_NAME)
    ctx.health.mark_ready("open_consumer")

    await subscribe_tm_none_consumer(ctx.js, lambda: ctx.conn, consumer_name=DECISION_CONSUMER_NAME)
    ctx.health.mark_ready("tm_none_consumer")

    await observation_loop(ctx, stop)


async def main_async() -> None:
    config = RuntimeConfig.from_env()
    health = HealthState()
    start_health_server(health, port=config.health_port)

    conn = connect(config.postgres)
    health.mark_ready("postgres")

    import nats
    nc = await nats.connect(config.nats_url, user=config.nats_user, password=config.nats_password,
                            name="p4-managed-trade-runtime", max_reconnect_attempts=-1, reconnect_time_wait=2)
    js = nc.jetstream()
    health.mark_ready("nats")

    provider = LiveMarketDataProvider(build_bridge_client(config.bridge_mcp_url))

    ctx = RuntimeContext(conn=conn, js=js, provider=provider, config=config, health=health)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    try:
        await run(ctx, stop)
    finally:
        await nc.drain()
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main_async())
