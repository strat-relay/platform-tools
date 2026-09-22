"""Standalone PostgreSQL-outbox to JetStream relay for DB_PRIMARY."""
from __future__ import annotations

import argparse
import asyncio
import os
import signal

import nats

from infrastructure.messaging.jetstream import JetStreamPublisher
from infrastructure.messaging.outbox_relay import OutboxRelay
from migration.flags import SignalAuthorityFlags, SignalAuthorityMode
from postgres.config import PostgresConfig
from postgres.db import connect


async def serve(*, idle_seconds: float = 1.0, once: bool = False) -> None:
    if SignalAuthorityFlags.mode_from_env() is not SignalAuthorityMode.DB_PRIMARY:
        raise RuntimeError("standalone production relay requires SIGNAL_AUTHORITY_MODE=DB_PRIMARY")
    db_config = PostgresConfig.from_env()
    db_config.require_explicit_target()
    conn = connect(db_config)
    nc = None
    try:
        with conn.cursor() as cur:
            from postgres.foundation import DATABASE_SCHEMA_VERSION
            cur.execute("SELECT version FROM platform.schema_migrations WHERE version=%s", (DATABASE_SCHEMA_VERSION,))
            row = cur.fetchone()
        if not row:
            raise RuntimeError(f"outbox relay requires PostgreSQL migration {DATABASE_SCHEMA_VERSION}")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if not once:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, stop.set)
        while nc is None:
            try:
                nc = await nats.connect(os.environ["NATS_URL"], user=os.environ.get("P2_NATS_USER"),
                                        password=os.environ.get("P2_NATS_PASSWORD"), name="signal-outbox-relay",
                                        max_reconnect_attempts=-1, reconnect_time_wait=2)
            except Exception:
                if once or stop.is_set():
                    raise
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
        if stop.is_set():
            return
        js = nc.jetstream()
        await js.stream_info("TRADING_CORE")
        relay = OutboxRelay(conn, JetStreamPublisher(js), owner=os.getenv("SIGNAL_OUTBOX_OWNER", "signal-outbox-relay"))
        if once:
            print(await relay.publish_batch())
            return
        await relay.run_forever(stop=stop, idle_seconds=idle_seconds)
    finally:
        if nc is not None:
            await nc.drain()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="publish one batch and exit")
    args = parser.parse_args()
    if args.idle_seconds <= 0:
        parser.error("--idle-seconds must be positive")
    asyncio.run(serve(idle_seconds=args.idle_seconds, once=args.once))


if __name__ == "__main__":
    main()
