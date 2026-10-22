"""Opt-in durable market -> research -> canonical publication transport.

Examples (provision streams is a separate explicit operator action):
 python -m research.library_event_service observer --manifest /config/run.json
 python -m research.library_event_service relay
 python -m research.library_event_service orchestrator --manifest /config/run.json
"""
import argparse
import asyncio
import json
import os
import re
import signal
import time

from research.library_event_store import EventStore, MARKET, ENTRY, open_connection, routed_subject
from research.strategy_library_forward import RunConfig

STREAMS={MARKET:'MARKET_EVIDENCE',ENTRY:'LIBRARY_RESEARCH'}


def report(event, **fields):
    print(json.dumps({'event':event,'observed_at':time.time(),**fields},allow_nan=False),flush=True)


async def relay_batch(conn, js):
    store=EventStore(conn)
    count=0
    # Keep row locks through JetStream acceptance. Crash before publication bookkeeping
    # commits redelivers the same ID; consumers use a durable inbox, not JS dedup alone.
    with store.atomic('library-stream-relay'):
        for event_id,subject in store.pending():
            payload=store.event(event_id,subject)
            publish_started_at=time.time()
            await js.publish(routed_subject(subject,payload),json.dumps({'event_id':event_id}).encode(),
                             headers={'Nats-Msg-Id':event_id})
            report('library_stream_notification_accepted',event_id=event_id,subject=subject,
                   publish_started_at=publish_started_at,publish_acknowledged_at=time.time())
            with conn.cursor() as cur:
                cur.execute('UPDATE research.library_stream_outbox SET published_at=clock_timestamp() WHERE event_id=%s', (event_id,))
            count+=1
    report('library_stream_batch_published',count=count)
    return count


async def handle_message(message, handler, *, subject, allowed_subjects=None):
    async def heartbeat():
        while True:
            await asyncio.sleep(20)
            await message.in_progress()
    task=None
    event_id=None
    try:
        if message.subject not in (allowed_subjects or {subject}) or len(message.data)>1024:
            raise ValueError('notification subject/size invalid')
        payload=json.loads(message.data)
        if set(payload)!={'event_id'} or not re.fullmatch('[0-9a-f]{64}',payload['event_id']):
            raise ValueError('notification must reference one durable event ID')
        event_id=payload['event_id']
        task=asyncio.create_task(heartbeat())
        received_at=time.time()
        result=await asyncio.to_thread(handler,payload['event_id'],received_at)
        await message.ack_sync()
        report('library_stream_message_committed',event_id=payload['event_id'],subject=subject,
               received_at=received_at,result=result)
    except Exception as exc:
        # Never acknowledge a failed effect, create an execution intent, or fall back to Redis.
        report('library_stream_message_blocked',subject=subject,event_id=event_id,error_type=type(exc).__name__,
               database_error_code=getattr(exc,'sqlstate',None),
               reason=str(exc) if isinstance(exc,ValueError) else None)
        await message.nak(delay=5)
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)


async def check_streams(js):
    for subject,name in STREAMS.items():
        info=await js.stream_info(name)
        if set(info.config.subjects)!={subject+'.*'}:
            raise ValueError('research stream ownership/configuration mismatch')
        if (info.config.storage!='file' or info.config.max_bytes<=0
                or info.config.max_bytes>256*1024*1024 or not 0<info.config.max_age<=604800):
            raise ValueError('research stream requires bounded durable file storage')


async def provision_streams(js):
    from nats.js.api import StreamConfig, StorageType, DiscardPolicy
    from nats.js.errors import NotFoundError
    for subject,name in STREAMS.items():
        try:
            info=await js.stream_info(name)
        except NotFoundError:
            await js.add_stream(config=StreamConfig(name=name,subjects=[subject+'.*'],
                storage=StorageType.FILE,discard=DiscardPolicy.NEW,num_replicas=1,
                max_age=86400 if subject==MARKET else 604800,max_bytes=256*1024*1024,max_msg_size=1024))
        else:
            if set(info.config.subjects)!={subject+'.*'}:
                raise ValueError('existing stream ownership conflict; no automatic rewrite')
    await check_streams(js)


async def relay(js, stop):
    import psycopg
    # LISTEN/NOTIFY wakes durable outbox delivery on commit. The timeout is only
    # reconciliation for disconnects/lost wakeups; business work does not wait for it.
    with open_connection() as conn:
        async with await psycopg.AsyncConnection.connect(os.environ['LIBRARY_EVENT_DSN'],autocommit=True) as listener:
            await listener.execute('LISTEN library_stream_outbox')
            wake=asyncio.Event()
            async def notifications():
                async for _ in listener.notifies():wake.set()
            task=asyncio.create_task(notifications())
            try:
                while not stop.is_set():
                    wake.clear()
                    if await relay_batch(conn,js):continue
                    try:await asyncio.wait_for(wake.wait(),timeout=5)
                    except TimeoutError:pass
                    if task.done():task.result()
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)


async def consume(js, role, config, stop):
    from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy
    from nats.errors import TimeoutError as NatsTimeoutError
    from research.library_event_consumer import reconcile_market, dispatch_research
    if config.transport!='JETSTREAM':raise ValueError('fresh JETSTREAM manifest required')
    subject=MARKET if role=='observer' else ENTRY
    filters=([routed_subject(MARKET,{'provider_symbol':p}) for p in config.symbols.values()]
             if role=='observer' else [routed_subject(ENTRY,{'run_id':config.run_id})])
    durable=role+'-'+config.run_id
    # Consumer identities bind the complete manifest/artifact in Postgres as well.
    if not re.fullmatch('[a-zA-Z0-9_-]{1,128}',durable):raise ValueError('DNS-safe durable identity required')
    sub=await js.pull_subscribe(filters[0],durable=durable,stream=STREAMS[subject],
        config=ConsumerConfig(durable_name=durable,ack_policy=AckPolicy.EXPLICIT,
            filter_subjects=filters,deliver_policy=DeliverPolicy.ALL,ack_wait=120,max_ack_pending=1,max_deliver=-1))
    actual=(await js.consumer_info(STREAMS[subject],durable)).config
    actual_filters=set(actual.filter_subjects or ([actual.filter_subject] if actual.filter_subject else []))
    if (actual_filters!=set(filters) or actual.ack_policy!=AckPolicy.EXPLICIT or actual.max_ack_pending!=1
            or actual.deliver_policy!=DeliverPolicy.ALL or actual.ack_wait!=120):
        raise ValueError('existing durable consumer configuration mismatch; no automatic rewind')
    def handler(event_id,received_at):
        with open_connection() as conn:
            if role=='observer':return reconcile_market(conn,config,event_id,now=received_at,clock=time.time)
            from orchestration.canonical_signal_publisher import CanonicalSignalPublisher
            from migration.flags import SignalAuthorityFlags, SignalAuthorityMode
            if SignalAuthorityFlags.mode_from_env() is not SignalAuthorityMode.DB_PRIMARY:
                raise ValueError('event orchestrator requires existing DB_PRIMARY authority')
            publisher=CanonicalSignalPublisher(conn,cutoff_id=os.environ.get('SIGNAL_CUTOFF_ID',''),
                cutoff_utc=os.environ.get('SIGNAL_CUTOFF_UTC',''))
            publisher.require_schema()
            return dispatch_research(conn,config,event_id,publisher,now=received_at,clock=time.time)
    try:
        while not stop.is_set():
            try:messages=await sub.fetch(1,timeout=5)
            except (NatsTimeoutError,TimeoutError):continue
            for message in messages:await handle_message(message,handler,subject=subject,allowed_subjects=set(filters))
    finally:
        await sub.unsubscribe()


async def serve(args):
    import nats
    if os.environ.get('LIBRARY_EVENT_TRANSPORT')!='JETSTREAM':
        raise ValueError('explicit LIBRARY_EVENT_TRANSPORT=JETSTREAM opt-in required')
    if args.role in {'observer','orchestrator'} and os.environ.get('EXECUTION_AUTHORITY_MODE')!='DISABLED':
        raise ValueError('research event processes require execution authority DISABLED')
    config=RunConfig.load(args.manifest) if args.manifest else None
    if args.role in {'observer','orchestrator'} and config is None:raise ValueError('manifest required')
    stop=asyncio.Event();loop=asyncio.get_running_loop()
    for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,stop.set)
    nc=await nats.connect(os.environ['NATS_URL'],user=os.environ.get('P2_NATS_USER'),
        password=os.environ.get('P2_NATS_PASSWORD'),name='library-'+args.role,
        max_reconnect_attempts=-1,reconnect_time_wait=2)
    try:
        js=nc.jetstream()
        if args.role=='provision-streams':await provision_streams(js);return
        await check_streams(js)
        if args.role=='relay':await relay(js,stop)
        else:await consume(js,args.role,config,stop)
    finally:
        await nc.drain()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role',choices=['relay','observer','orchestrator','provision-streams'])
    parser.add_argument('--manifest')
    asyncio.run(serve(parser.parse_args()))


if __name__=='__main__':main()
