"""Event-driven entry evaluation against immutable market evidence, never mutable Redis."""
from datetime import datetime, timezone
from types import SimpleNamespace

from core.strategies.entry_quote import EntryQuoteUnavailable
from core.strategies.entry_timing import MAX_ENTRY_DELAY_SECONDS
from core.strategies.evaluation import canonical_hash
from research.library_event_store import EventStore, MARKET, ENTRY, SCHEMA
from research.strategy_library_forward import ForwardObserver, PostgresResearchLedger
from strategy_backtest.library.forward_selection import DEFINITIONS_BY_FORWARD_SELECTOR as BY_KEY


def view(snapshot, event, config, now):
    if snapshot['provider_symbol'] != event['provider_symbol']:
        raise ValueError('market evidence provider mismatch')
    remainder = snapshot['snapshot']
    if remainder.get('source_read_health') is not True:
        raise ValueError('unhealthy event snapshot')
    if not snapshot['observed_at'] <= event['observed_at'] <= now:
        raise ValueError('market event availability clock invalid')
    from strategy_backtest.library.runtime import STEPS
    if any(row['time']+STEPS[tf]>snapshot['observed_at']
           for tf,rows in snapshot['bars'].items() for row in rows):
        raise ValueError('snapshot contains candles unavailable at original source observation')
    return SimpleNamespace(symbol_info=remainder['symbol_info'],
        bars={tf:SimpleNamespace(bars=rows[-config.history:]) for tf,rows in snapshot['bars'].items()},
        cache_age_seconds=now-snapshot['observed_at'], gap_status='NONE', recovery_status='NONE',
        continuity_status='CONTINUOUS', source_timing=remainder.get('source_timing',{}),
        opening_observations=remainder.get('opening_observations',{}), entry_quote=event.get('quote'))


def transition(observer, event, snapshot, *, now, clock=None):
    """Pure transition used by transport and replay tests. No delivery-time backdating."""
    cfg = observer.config
    if cfg.transport != 'JETSTREAM':
        raise ValueError('event consumer requires a fresh JETSTREAM run')
    if event['provider_symbol'] not in cfg.symbols.values():
        return [], 'OUTSIDE_SCOPE'
    if event['observed_at'] < cfg.activation_time:
        return [], 'PRE_ACTIVATION'
    instrument = next(k for k,v in cfg.symbols.items() if v==event['provider_symbol'])
    snapshot_view = view(snapshot,event,cfg,now)
    recovery = now-event['observed_at'] > MAX_ENTRY_DELAY_SECONDS or snapshot_view.cache_age_seconds > cfg.max_age_seconds
    records, blocked = [], []
    for key in cfg.keys:
        d=BY_KEY[key]
        if d.symbols and instrument not in d.symbols:
            continue
        stream_id=key+':'+instrument
        stored=observer.state['streams'].get(stream_id)
        if (event.get('kind')=='QUOTE' and stored
                and stored.get('last_event_snapshot_id')==event['snapshot_id']
                and not stored['runtime']['state']['pending'].get(instrument)):
            continue  # quote updates wake only unresolved next-open opportunities
        if any(len(snapshot['bars'].get(tf,[])) < cfg.history for tf in {d.context,d.execution}):
            raise ValueError('immutable snapshot has insufficient warm-up history')
        try:
            key_now=clock() if clock else now
            snapshot_view.cache_age_seconds=key_now-snapshot['observed_at']
            key_recovery=(recovery or key_now-event['observed_at']>MAX_ENTRY_DELAY_SECONDS
                          or snapshot_view.cache_age_seconds>cfg.max_age_seconds)
            recovery=recovery or key_recovery
            emitted=observer.process(key,instrument,snapshot_view,observed_at=key_now,recovery=key_recovery)
        except EntryQuoteUnavailable as exc:
            # Keep that stream's checkpoint/pending setup untouched. A later genuine quote
            # can retry the same next-open opportunity; never synthesize or reprice a fill.
            blocked.append({'key':key,'reason':str(exc)})
            continue
        observer.state['streams'][stream_id]['last_event_snapshot_id']=event['snapshot_id']
        for record in emitted:
            record['stage_timing']={**snapshot_view.source_timing,
                'market_event_observed_at':event['observed_at'], 'event_processing_started_at':now,
                'market_source_sequence':event['sequence'], 'market_snapshot_id':event['snapshot_id']}
        records.extend(emitted)
    providers=observer.state.get('health',{}).get('providers',{})
    providers[event['provider_symbol']]={'observed_at':now,'status':'DEGRADED' if blocked else 'OK',
                                        'source_sequence':event['sequence']}
    observer.state['health']={'observed_at':now,
        'status':'OK' if set(providers)==set(cfg.symbols.values()) and all(p['status']=='OK' for p in providers.values()) else 'DEGRADED',
        'transport':'JETSTREAM','provider_symbol':event['provider_symbol'],
        'source_sequence':event['sequence'],'blocked':blocked,'recovery':recovery,'providers':providers}
    return records, 'WAITING_QUOTE' if blocked else ('RECOVERY' if recovery else 'PROCESSED')


def process_market(conn, config, event_id, *, now, clock=None):
    store=EventStore(conn);consumer='library-observer:'+config.run_id
    stamp=clock if clock else lambda:now
    stages={'event_consumer_started_at':stamp()}
    with store.atomic(consumer):
        stages['lock_acquired_at']=stamp()
        event=store.event(event_id,MARKET)
        if store.seen(consumer,event_id):
            return 'DUPLICATE'
        provider=event['provider_symbol']
        if provider not in config.symbols.values():
            store.done(consumer,event_id,'OUTSIDE_SCOPE')
            return 'OUTSIDE_SCOPE'
        sequence=event['sequence'];previous=store.cursor(consumer,provider)
        if type(sequence) is not int or sequence != previous+1:
            raise ValueError('market source sequence gap/order conflict; reconcile retained evidence')
        stages['snapshot_read_started_at']=stamp()
        snapshot=store.snapshot(event['snapshot_id'],provider)
        stages['snapshot_read_completed_at']=stamp()
        ledger=PostgresResearchLedger(conn,config.run_id)
        state=ledger.lock_and_load()
        if state is None:
            raise ValueError('fresh event run must be explicitly provisioned before consumption')
        observer=ForwardObserver(config);observer.restore(state)
        attestation=state.get('activation_attestation',{})
        if attestation.get('execution_eligible') is not False or any(attestation.get(k)!=v for k,v in {'schema':SCHEMA,'run_id':config.run_id,
                'binding':observer.binding,'activation_time':config.activation_time,
                'transport':'JETSTREAM','execution_eligible':False}.items()):
            raise ValueError('reviewed event-run activation attestation missing/mismatched')
        stages['evaluation_started_at']=stamp()
        records,result=transition(observer,event,snapshot,now=stamp(),clock=clock)
        stages['evaluation_completed_at']=stamp()
        for record in records:record['stage_timing'].update(stages)
        stages['ledger_write_started_at']=stamp()
        ledger.save(observer.state,records)
        for record in records:
            provenance=record.get('payload',{}).get('provenance',{})
            if record['kind']=='EVALUATION' and provenance.get('research_entry'):
                store.append(ENTRY,provider,{'schema':SCHEMA,'run_id':config.run_id,
                    'record_id':record['event_id'],'strategy_key':record['strategy_key'],
                    'instrument':record['instrument'],'observer_binding':observer.binding,
                    'execution_eligible':False})
        store.advance_cursor(consumer,provider,sequence)
        store.done(consumer,event_id,result)
        stages['ledger_write_completed_at']=stamp()
    stages['transaction_committed_at']=stamp()
    from research.library_event_store import encode
    print(encode({'event':'library_market_event_committed','run_id':config.run_id,
        'source_event_id':event_id,'record_ids':[r['event_id'] for r in records],
        'stage_timing':stages}),flush=True)
    return result


def reconcile_market(conn, config, event_id, *, now, limit=100, clock=None):
    """Bounded recovery from immutable DB evidence when JetStream retention has advanced."""
    store=EventStore(conn);target=store.event(event_id,MARKET)
    provider=target['provider_symbol'];consumer='library-observer:'+config.run_id
    if provider not in config.symbols.values():
        return process_market(conn,config,event_id,now=clock() if clock else now,clock=clock)
    for _ in range(limit):
        sequence=store.cursor(consumer,provider)
        if sequence>=target['sequence']:
            return process_market(conn,config,event_id,now=clock() if clock else now,clock=clock)
        with conn.cursor() as cur:
            cur.execute('''SELECT event_id FROM research.library_stream_outbox
                WHERE subject=%s AND provider_symbol=%s AND sequence=%s''',(MARKET,provider,sequence+1))
            row=cur.fetchone()
        if not row:
            raise ValueError('market source history unavailable; root evidence gap requires review')
        result=process_market(conn,config,row[0],now=clock() if clock else now,clock=clock)
        if row[0]==event_id:return result
    raise RuntimeError('bounded market recovery made progress; redeliver to continue')


def dispatch_research(conn, config, event_id, publisher, *, now, clock=None):
    """The canonical orchestrator publication boundary, research-only and DB_PRIMARY.

    Canonical persistence is already idempotent. If its inner transaction commits
    before inbox completion and the process dies, retry verifies canonical identity
    and records completion without changing its original classification or price.
    """
    from dataclasses import replace
    from orchestration.adapters.strategy_library import StrategyLibraryAdapter
    from research.strategy_library_registration import registration
    store=EventStore(conn);consumer='library-orchestrator:'+config.run_id
    with store.atomic(consumer):
        event=store.event(event_id,ENTRY)
        if event.get('execution_eligible') is not False:
            raise ValueError('research notification execution eligibility invalid')
        if event['run_id'] != config.run_id:
            store.done(consumer,event_id,'OUTSIDE_SCOPE')
            return 'OUTSIDE_SCOPE'
        if store.seen(consumer,event_id):return 'DUPLICATE'
        key=event['strategy_key']
        if config.transport!='JETSTREAM' or key not in config.keys:
            raise ValueError('notification outside frozen run scope')
        identity=registration(key)
        with conn.cursor() as cur:
            cur.execute('''SELECT payload FROM research.strategy_library_forward_event
                WHERE run_id=%s AND event_id=%s AND kind='EVALUATION' ''',(config.run_id,event['record_id']))
            row=cur.fetchone()
        if not row:raise ValueError('durable accepted research entry missing')
        record=row[0]
        if event['observer_binding'] != ForwardObserver(config).binding:
            raise ValueError('notification artifact/run binding mismatch')
        if (record.get('payload',{}).get('provenance',{}).get('dataset_manifest_hash') != event['observer_binding']
                or canonical_hash({'binding':event['observer_binding'],
                    'stream':key+':'+event['instrument'],'evaluation':record['payload']}) != event['record_id']):
            raise ValueError('accepted record evidence identity conflict')
        with conn.cursor() as cur:
            cur.execute('''SELECT i.online,i.execution_eligible,i.execution_mode,i.attributes,p.fingerprint,
                v.version_label,p.frozen,p.values,v.evaluator_key
                FROM strategy_mgmt.strategy_instance_v2 i
                JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id
                JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id
                WHERE i.id=%s FOR SHARE OF i,p,v''',(identity['instance_id'],))
            managed=cur.fetchone()
            cur.execute('SELECT enabled,adapter FROM platform.strategy_definition WHERE strategy_id=%s FOR SHARE',
                        (identity['strategy_id'],))
            parent=cur.fetchone()
        if not managed or not parent or parent!=(True,'StrategyLibraryAdapter'):
            raise ValueError('managed research publication parent unavailable')
        online,eligible,mode,attrs,fingerprint,version,frozen,parameters,evaluator=managed
        if (not online or eligible or mode!='OFF' or attrs.get('status')!='RESEARCH'
            or attrs.get('signal_publication') is not True or attrs.get('publication_transport')!='JETSTREAM'
            or attrs.get('orchestrator_run_id')!=config.run_id
            or attrs.get('publication_activation_time')!=config.activation_time
            or fingerprint!=identity['parameter_hash'] or version!=identity['parameters']['version']
            or not frozen or parameters!=identity['parameters'] or evaluator!=identity['evaluator_key']):
            raise ValueError('managed research OFF/run/parameter publication gate failed')
        if event['instrument'] not in config.symbols:
            raise ValueError('notification instrument outside run')
        # Validate membership directly; a source notification cannot expand the live instance universe.
        with conn.cursor() as cur:
            cur.execute('''SELECT canonical_instrument FROM strategy.instrument_membership
                WHERE strategy_instance_id=%s AND strategy_id=%s AND state='ACTIVE' FOR SHARE ''',
                (identity['instance_id'],identity['strategy_id']))
            instruments=[r[0] for r in cur.fetchall()]
        instance={**attrs,'instance_id':identity['instance_id'],'parameter_set_fingerprint':fingerprint,
                  'execution_mode':mode,'active_instruments':instruments}
        signal=StrategyLibraryAdapter(conn,key,instance).convert(event['record_id'],event['instrument'],record)
        with conn.cursor() as cur:
            cur.execute('SELECT strategy_id,source_event_id,strategy_instance_id FROM strategy.entry_signals WHERE signal_id=%s',
                        (signal.signal_id,))
            existing=cur.fetchone()
        if existing:
            if tuple(existing)!=(signal.strategy_id,signal.source_event_id,signal.strategy_instance_id):
                raise ValueError('canonical signal identity conflict')
            store.done(consumer,event_id,'DUPLICATE_CANONICAL')
            return 'DUPLICATE_CANONICAL'
        now=clock() if clock else now
        if now-float(record['observed_at'])<0:
            raise ValueError('notification observation is in the future')
        if now-datetime.fromisoformat(signal.signal_timestamp.replace('Z','+00:00')).timestamp()>MAX_ENTRY_DELAY_SECONDS:
            metadata={**signal.strategy_metadata,'research_only':True,'execution_eligible':False,
                'signal_emission_class':'RETROSPECTIVE_RESEARCH_EVIDENCE',
                'timing_rejection_reason':'ENTRY_DISPATCH_WINDOW_EXPIRED'}
            provenance={**signal.provenance,'classification':'RETROSPECTIVE_RESEARCH_EVIDENCE',
                'signal_emission_class':'RETROSPECTIVE_RESEARCH_EVIDENCE'}
            signal=replace(signal,strategy_metadata=metadata,provenance=provenance)
        publisher.publish(signal)
        store.done(consumer,event_id,'PUBLISHED_RESEARCH')
    return 'PUBLISHED_RESEARCH'
