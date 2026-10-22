"""Explicit provisioning and durable relay for strategy-ready scheduling hints.

No evaluator, catalog registration, canonical publisher, or execution authority.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import time

from infrastructure.messaging.runtime_events import READY, CACHE, STREAM, connection, report


async def check_stream(js):
    info = await js.stream_info(STREAM)
    cfg = info.config
    if (set(cfg.subjects) != {READY+'.*', CACHE+'.*'} or cfg.storage != 'file'
            or not 0 < cfg.max_age <= 86400 or not 0 < cfg.max_bytes <= 64*1024*1024
            or cfg.discard != 'new' or cfg.max_msg_size != 1024):
        raise ValueError('strategy wakeup stream configuration conflict; no automatic rewrite')


async def provision(js):
    from nats.js.errors import NotFoundError
    from nats.js.api import StreamConfig, StorageType, DiscardPolicy
    try:
        await js.stream_info(STREAM)
    except NotFoundError:
        await js.add_stream(config=StreamConfig(name=STREAM, subjects=[READY+'.*', CACHE+'.*'],
            storage=StorageType.FILE, discard=DiscardPolicy.NEW, max_age=300,
            max_bytes=64*1024*1024, max_msg_size=1024, num_replicas=1))
    await check_stream(js)


async def relay_batch(conn, js):
    # Publish before marking committed. Crash after acceptance retries the same
    # event ID; repeated hints are safe because the existing publisher deduplicates.
    count = 0
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute('''SELECT event_id,runtime_id,payload FROM platform.strategy_runtime_outbox
                WHERE published_at IS NULL ORDER BY created_at,event_id
                LIMIT 100 FOR UPDATE SKIP LOCKED''')
            rows = cur.fetchall()
        for event_id, runtime, payload in rows:
            started = time.monotonic()
            provider = payload.get('provider_symbol')
            subject = CACHE+'.'+provider.encode().hex() if provider else READY+'.'+runtime
            await js.publish(subject, json.dumps({'event_id': event_id}).encode(),
                             headers={'Nats-Msg-Id': event_id})
            with conn.cursor() as cur:
                cur.execute('UPDATE platform.strategy_runtime_outbox SET published_at=clock_timestamp() WHERE event_id=%s',
                            (event_id,))
            report('strategy_ready_nats_accepted', event_id=event_id, runtime_id=runtime,
                   publish_ack_ms=(time.monotonic()-started)*1000)
            count += 1
    return count


def prune_delivered(conn):
    # These rows are scheduling hints, not research evidence. Keep a bounded
    # publication audit window without growing the primary DB on every quote.
    # Never remove pending hints or any strategy/evaluation/outcome/checkpoint row.
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute('''WITH expired AS (
                SELECT event_id FROM platform.strategy_runtime_outbox
                WHERE published_at < clock_timestamp()-interval '1 hour'
                ORDER BY published_at,event_id LIMIT 1000 FOR UPDATE SKIP LOCKED)
                DELETE FROM platform.strategy_runtime_outbox o USING expired e
                WHERE o.event_id=e.event_id''')


async def relay(js, stop):
    import psycopg
    with connection() as conn:
        async with await psycopg.AsyncConnection.connect(os.environ['STRATEGY_EVENT_DSN'], autocommit=True) as listener:
            await listener.execute('LISTEN strategy_runtime_outbox')
            wake = asyncio.Event()

            async def notifications():
                async for _ in listener.notifies():
                    wake.set()
            task = asyncio.create_task(notifications())
            last_prune = time.monotonic()
            try:
                while not stop.is_set():
                    wake.clear()
                    if time.monotonic()-last_prune >= 60:
                        prune_delivered(conn)
                        last_prune = time.monotonic()
                    if await relay_batch(conn, js):
                        continue
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=5)
                    except TimeoutError:
                        pass  # lost notification/reconnect recovery, not delivery cadence
                    if task.done():
                        task.result()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def serve(role):
    import nats
    for name in ('NATS_URL', 'P2_NATS_USER', 'P2_NATS_PASSWORD'):
        if not os.environ.get(name):
            raise ValueError('explicit scoped NATS credentials required')
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    nc = await nats.connect(os.environ['NATS_URL'], user=os.environ['P2_NATS_USER'],
        password=os.environ['P2_NATS_PASSWORD'], name='strategy-ready-'+role, connect_timeout=5)
    try:
        js = nc.jetstream()
        if role == 'provision':
            await provision(js)
        else:
            await check_stream(js)
            await relay(js, stop)
    finally:
        await nc.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('provision', 'relay'))
    args = parser.parse_args()
    asyncio.run(serve(args.role))


if __name__ == '__main__':
    main()
