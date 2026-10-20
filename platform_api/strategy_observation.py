"""Read-only, instance-scoped observation and evaluation drill-down.

Counters use persisted observation time, one immutable run and one date window.
Checkpoint health is current and is never presented as historical range health.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
import math

PA = {'PRICE_ACTION_SUPPORT_RESISTANCE_RETEST':'sr_retest',
      'PRICE_ACTION_ENGULFING_CONTINUATION':'engulfing_continuation'}
PA_KEYS = {'price_action_sr_retest_v1':'PRICE_ACTION_SUPPORT_RESISTANCE_RETEST',
           'price_action_engulfing_continuation_v1':'PRICE_ACTION_ENGULFING_CONTINUATION'}
SAFE_PROVENANCE = ('parameter_values','parameter_hash','parameter_set_id','primitive_manifest','primitive_version',
    'dependency_manifest_hash','dataset_manifest_hash','data_snapshot_id','artifact_digest',
    'market_input_policy','provider_tick_size','tick_size_source','entry_timing',
    'information_cutoff','rejection_reason','reason','entry_open','entry_reference','stop','target',
    'planned_r','actual_entry_r','entry_cancelled','level','zone','research_entry','proposal')


def iso(value):
    if value is None:return None
    if isinstance(value,datetime):return value.astimezone(timezone.utc).isoformat()
    return str(value)


def timestamp(value):
    if value is None:return None
    try:
        if isinstance(value,datetime):parsed=value
        elif isinstance(value,(int,float)) or str(value).replace('.','',1).isdigit():
            parsed=datetime.fromtimestamp(float(value),timezone.utc)
        else:parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError,TypeError,OverflowError,OSError):return None


def period(query,now):
    key=(query.get('period') or ['RUN'])[0].upper()
    start=None;end=now
    if key=='TODAY':start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    elif key=='24H':start=now-timedelta(hours=24)
    elif key=='7D':start=now-timedelta(days=7)
    elif key=='CUSTOM':
        start=timestamp((query.get('from') or [''])[0]);end=timestamp((query.get('to') or [''])[0])
        if start is None or end is None or end<=start:raise ValueError('Custom range requires timezone-aware from < to')
    elif key!='RUN':raise ValueError('period must be RUN, TODAY, 24H, 7D or CUSTOM')
    return start,end,key


def reason_of(evaluation):
    p=evaluation.get('provenance') or {}
    return p.get('rejection_reason') or p.get('reason') or evaluation.get('decision') or 'UNKNOWN'


def lifecycle(evaluation,signal_id=None,publish_status=None):
    reason=reason_of(evaluation)
    accepted=reason in {'ENTRY_OBSERVED_RESEARCH_ONLY','ENTRY_ACCEPTED'} or evaluation.get('decision')=='SIGNAL'
    proposal=reason in {'RESEARCH_PROPOSAL_ONLY','PROPOSAL'}
    if accepted and signal_id:return 'PUBLISHED' if publish_status=='PUBLISHED' else 'CANONICAL_CREATED'
    if accepted:return 'ACCEPTED_AWAITING_PUBLICATION'
    if proposal:return 'AWAITING_ENTRY'
    if (evaluation.get('provenance') or {}).get('entry_cancelled'):return 'CANCELLED'
    return 'NO_SETUP' if evaluation.get('decision')=='NO_CANDIDATE' else 'REJECTED'


class ObservationRepository:
    def __init__(self,connect_fn,clock=None):
        self.connect=connect_fn;self.clock=clock or (lambda:datetime.now(timezone.utc))

    def owner(self,cur,strategy_id,instance_id):
        cur.execute('''SELECT i.id::text,v.evaluator_key,i.attributes,i.instruments,p.parameter_set_id,d.family_key
          FROM strategy_mgmt.strategy_instance_v2 i
          JOIN strategy_mgmt.strategy_version v ON v.id=i.strategy_version_id
          JOIN strategy_mgmt.parameter_set p ON p.id=i.parameter_set_id
          JOIN strategy_mgmt.strategy_definition d ON d.id=v.definition_id
          WHERE i.id::text=%s OR i.attributes->>'legacy_instance_id'=%s OR i.attributes->>'instance_id'=%s''',
          (instance_id,instance_id,instance_id))
        rows=cur.fetchall()
        from strategy_backtest.library import BY_KEY
        keys={**PA_KEYS,'kojo_structure_reclaim_v3':'KOJO_STRUCTURE_RECLAIM_V3',
              'kojo_structure_reclaim':'KOJO_STRUCTURE_RECLAIM_V1'}
        for key,definition in BY_KEY.items():keys['library.'+key+'@V1']=definition.strategy_id
        from strategy_backtest.library.forward_selection import DEFINITIONS_BY_FORWARD_SELECTOR
        for definition in DEFINITIONS_BY_FORWARD_SELECTOR.values():
            keys['library.'+definition.key+'@'+definition.version]=definition.strategy_id
        for iid,key,attrs,instruments,parameter,family in rows:
            attrs=attrs or {};sid=keys.get(key) or attrs.get('strategy_id') or family
            if sid==strategy_id or 'LIBRARY:'+sid==strategy_id or family==strategy_id or (strategy_id=='CONTEXT_STRUCTURE_RETRACE' and sid in {'CONTEXT_STRUCTURE_RETRACE_V1','CONTEXT_STRUCTURE_RETRACE_V2'}):
                return {'id':iid,'strategy_id':sid,'canonical_id':attrs.get('legacy_instance_id') or iid,
                        'attributes':attrs,'instruments':instruments or [],'parameter_set':parameter}
        cur.execute('SELECT instance_id,strategy_id,attributes FROM platform.strategy_instance WHERE instance_id=%s AND strategy_id=%s',
                    (instance_id,strategy_id))
        row=cur.fetchone()
        if row:return {'id':str(row[0]),'strategy_id':strategy_id,'canonical_id':str(row[0]),'attributes':row[2] or {},'instruments':[],'parameter_set':None}
        return None

    def source(self,cur,owner,strategy_id):
        attrs=owner['attributes'];state={};run_id=owner['id'];key=PA.get(strategy_id)
        if key:
            cur.execute('SELECT state,last_observed_at FROM research.price_action_observer_state WHERE instance_id=%s AND strategy_key=%s',
                        (owner['id'],key));row=cur.fetchone()
            if row:state=row[0] or {}
            sql="""SELECT event_id,instrument,observed_at,payload->'event' AS evaluation,
                'price-action-observer:'||event_id AS source_event_id
                FROM research.price_action_observer_evaluation WHERE instance_id=%s AND strategy_key=%s"""
            params=[owner['id'],key];coverage='COMPLETE_LEDGER';source='PA_OBSERVER'
        elif attrs.get('library_key'):
            # The run identity is authoritative in the forward evidence ledger. Older
            # registrations may have the library key but lack the copied orchestrator_run_id
            # instance attribute; do not fall through to generic canonical evaluations, which
            # are a different evidence source and can produce a misleading SOURCE_UNAVAILABLE.
            key=attrs['library_key'];run_id=attrs.get('orchestrator_run_id')
            if not run_id:
                cur.execute('''SELECT run_id
                    FROM research.strategy_library_forward_event
                    WHERE strategy_key=%s
                    GROUP BY run_id
                    ORDER BY max(observed_at) DESC, run_id DESC
                    LIMIT 1''',(key,))
                discovered=cur.fetchone()
                run_id=discovered[0] if discovered else None
            if run_id:
                cur.execute('SELECT state FROM research.strategy_library_forward_state WHERE run_id=%s',(run_id,));row=cur.fetchone()
                if row:state=row[0] or {}
                sql="""SELECT event_id,instrument,observed_at,payload->'payload' AS evaluation,
                    'strategy-library:'||run_id||':'||event_id AS source_event_id
                    FROM research.strategy_library_forward_event WHERE run_id=%s AND strategy_key=%s AND kind='EVALUATION'"""
                params=[run_id,key];coverage='COMPLETE_LEDGER';source='LIBRARY_OBSERVER'
            else:
                # No forward run exists for this registered library key. Preserve the generic
                # fallback for other instances, but never manufacture a library run identity.
                sql="""SELECT e.evaluation_id AS event_id,e.instrument,e.created_at AS observed_at,
                    e.canonical_payload AS evaluation,NULL::text AS source_event_id
                    FROM strategy.evaluations e WHERE e.strategy_id=%s AND
                    (e.provenance->>'strategy_instance_id'=%s OR EXISTS
                      (SELECT 1 FROM strategy.entry_signals s WHERE s.evaluation_id=e.evaluation_id AND s.strategy_instance_id=%s))"""
                params=[strategy_id,owner['canonical_id'],owner['canonical_id']];coverage='PARTIAL';source='CANONICAL_EVALUATIONS'
        else:
            sql="""SELECT e.evaluation_id AS event_id,e.instrument,e.created_at AS observed_at,
                e.canonical_payload AS evaluation,NULL::text AS source_event_id
                FROM strategy.evaluations e WHERE e.strategy_id=%s AND
                (e.provenance->>'strategy_instance_id'=%s OR EXISTS
                  (SELECT 1 FROM strategy.entry_signals s WHERE s.evaluation_id=e.evaluation_id AND s.strategy_instance_id=%s))"""
            params=[strategy_id,owner['canonical_id'],owner['canonical_id']]
            coverage='PARTIAL';source='CANONICAL_EVALUATIONS'
        return state,run_id,key,sql,params,coverage,source

    def read(self,strategy_id,instance_id,query,event_id=None):
        now=self.clock();start,end,preset=period(query,now)
        try:offset=int((query.get('offset') or ['0'])[0]);limit=int((query.get('limit') or ['25'])[0])
        except ValueError:raise ValueError('Invalid pagination')
        if offset<0 or not 1<=limit<=100:raise ValueError('limit must be 1..100; offset must be nonnegative')
        instrument=(query.get('instrument') or [''])[0].strip().upper()
        with self.connect(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                cur.execute("SET LOCAL statement_timeout = '8s'")
                owner=self.owner(cur,strategy_id,instance_id)
                if owner is None:return None
                strategy_id=owner["strategy_id"]
                state,run_id,key,base,params,coverage,source=self.source(cur,owner,strategy_id)
                requested=(query.get('run_id') or [run_id])[0]
                if requested!=run_id:raise ValueError('Run does not belong to selected instance')
                cur.execute('WITH source AS ('+base+') SELECT min(observed_at) FROM source',tuple(params))
                earliest=cur.fetchone()[0]
                if start is None:start=timestamp(state.get('t0')) or timestamp(earliest) or end
                if start>end:raise ValueError('Range starts after end')
                active=self.active_instruments(state,owner,key)
                scope=" AND observed_at >= %s AND observed_at < %s";args=params+[start,end]
                if instrument:scope+=' AND instrument=%s';args.append(instrument)
                elif active:scope+=' AND instrument=ANY(%s)';args.append(active)
                scoped='SELECT * FROM source WHERE true'+scope
                reason="COALESCE(f.evaluation->'provenance'->>'rejection_reason',f.evaluation->'provenance'->>'reason',f.evaluation->>'decision','UNKNOWN')"
                join=""" LEFT JOIN LATERAL (SELECT s.signal_id,s.signal_emitted_at,s.ingested_at,
                    o.publish_status,o.published_at,s.strategy_metadata
                    FROM strategy.entry_signals s LEFT JOIN platform.outbox_events o
                      ON o.event_id=s.signal_id||':entry.created'
                    WHERE s.strategy_id=%s AND s.strategy_instance_id=%s
                    AND (s.source_event_id=f.source_event_id OR s.evaluation_id=f.event_id)
                    ORDER BY s.ingested_at DESC LIMIT 1) linked ON true """
                cte='WITH source AS ('+base+'), filtered AS ('+scoped+') '
                link_args=[strategy_id,owner['canonical_id']]
                if event_id:
                    cur.execute(cte+'''SELECT f.event_id,f.instrument,f.observed_at,f.evaluation,
                        linked.signal_id,linked.publish_status,linked.signal_emitted_at,linked.ingested_at,
                        linked.published_at,linked.strategy_metadata FROM filtered f '''+join+' WHERE f.event_id=%s',tuple(args+link_args+[event_id]))
                    row=cur.fetchone()
                    if not row:return None
                    detail=self.detail(row,state,source)
                    candidate=(row[3] or {}).get('candidate_id')
                    detail['episode_history']=[]
                    detail['episode_note']='Later transitions are unavailable without an explicit matching candidate identity. Qualification describes this evaluation, not a claim that it is still pending.'
                    if candidate:
                        history_sql=('WITH source AS ('+base+') SELECT f.event_id,f.instrument,f.observed_at,f.evaluation, linked.signal_id,linked.publish_status FROM source f '+join+
                            " WHERE f.evaluation->>'candidate_id'=%s AND f.instrument=%s AND f.observed_at >= %s AND f.event_id<>%s ORDER BY f.observed_at,f.event_id LIMIT 20")
                        cur.execute(history_sql,tuple(params+link_args+[candidate,row[1],row[2],row[0]]))
                        detail['episode_history']=[self.evaluation_row(r) for r in cur.fetchall()]
                        detail['episode_note']='Later evidence is joined only by recorded candidate identity within this instance/run. It may fall outside the selected dates. No timestamp-based match is inferred.'
                    return detail
                cur.execute(cte+'SELECT f.instrument,'+reason+''' AS reason,f.evaluation->>'decision',
                    linked.signal_id IS NOT NULL,linked.publish_status,count(*),max(f.observed_at)
                    FROM filtered f '''+join+''' GROUP BY f.instrument,reason,f.evaluation->>'decision',
                    linked.signal_id IS NOT NULL,linked.publish_status''',tuple(args+link_args))
                groups=cur.fetchall();totals=Counter();rejections=Counter();by_instrument={}
                for symbol,reason,decision,canonical,published,count,last in groups:
                    count=int(count);item=by_instrument.setdefault(symbol,{'instrument':symbol,'evaluations':0,'reasons':Counter(),'last_evaluated_at':None})
                    item['evaluations']+=count;item['reasons'][reason]+=count
                    if not item['last_evaluated_at'] or timestamp(last)>timestamp(item['last_evaluated_at']):item['last_evaluated_at']=iso(last)
                    totals['evaluations']+=count
                    if reason in {'RESEARCH_PROPOSAL_ONLY','PROPOSAL'}:totals['setups']+=count
                    elif reason in {'ENTRY_OBSERVED_RESEARCH_ONLY','ENTRY_ACCEPTED'} or decision=='SIGNAL':
                        totals['accepted_entries']+=count
                        if not canonical or published!='PUBLISHED':totals['pending_handoff']+=count
                    else:rejections[reason]+=count
                    if canonical:totals['canonical_created']+=count
                    if published=='PUBLISHED':totals['published']+=count
                cur.execute(cte+'''SELECT f.event_id,f.instrument,f.observed_at,f.evaluation,
                    linked.signal_id,linked.publish_status FROM filtered f '''+join+
                    ' ORDER BY f.observed_at DESC,f.event_id DESC LIMIT %s OFFSET %s',tuple(args+link_args+[limit,offset]))
                evaluations=[self.evaluation_row(row) for row in cur.fetchall()]
                runtime=state.get('runtime') or state.get('health') or {}
                last_cycle=runtime.get('last_cycle_at') or runtime.get('observed_at')
                parsed=timestamp(last_cycle)
                health='UNKNOWN' if parsed is None else ('STALE' if not 0 <= (now-parsed).total_seconds() <= 330 else
                    'DEGRADED' if runtime.get('status')=='DEGRADED' or runtime.get('degraded_instrument_count',0)>0 else 'RUNNING')
                for symbol in ([instrument] if instrument else active):
                    by_instrument.setdefault(symbol,{'instrument':symbol,'evaluations':0,'reasons':Counter(),'last_evaluated_at':None})
                historical=set(self.retained_instruments(state,key))-set(active)
                cur.execute("SELECT canonical_instrument,asset_class FROM platform.instrument_provider_mapping WHERE provider='MT5' AND state='ACTIVE' AND canonical_instrument=ANY(%s)",
                            (sorted(set(active)|set(by_instrument)),))
                assets=dict(cur.fetchall())
                if source=='LIBRARY_OBSERVER':
                    from .library_observation import observation_status
                    health=observation_status(state,key,now=now.timestamp(),asset_classes=assets)['status']
                instruments=[]
                for symbol,item in sorted(by_instrument.items()):
                    item['reasons']=dict(item['reasons']);item['active']=symbol in active if active else None
                    item['market_status']=self.market_status(state,symbol,now,assets.get(symbol),key)
                    instruments.append(item)
                issues=[]
                if source=='PA_OBSERVER' and state and state.get('market_input_policy')!='pa-provider-tick.v1':
                    issues.append({'code':'UNVERIFIED_INPUT_CONTRACT','message':'Provider tick metadata is not bound to the corrected input contract in this run. Review this evidence before assessing strategy performance.'})
                if coverage=='PARTIAL':issues.append({'code':'PARTIAL_TELEMETRY','message':'Only evaluations explicitly linked to this instance are available. Missing setup/rejection telemetry is not a zero count.'})
                return {'strategy_id':strategy_id,'instance_id':instance_id,'run_id':run_id,'runs':[{'id':run_id,'label':str(run_id)}],
                    'coverage':coverage,'source':source,'range':{'from':iso(start),'to':iso(end),'preset':preset,'time_basis':'OBSERVATION_TIME','timezone':'UTC'},
                    'current':{'status':health,'last_cycle_at':iso(parsed),'instrument_count':len(active),'scope_basis':'RECENT_CYCLE_COVERAGE' if source=='PA_OBSERVER' else 'RUN_MANIFEST'},
                    'counts':{name:(int(totals[name]) if coverage!='PARTIAL' or name in {'evaluations','published','canonical_created'} else None)
                        for name in ('evaluations','setups','accepted_entries','canonical_created','published','pending_handoff')},
                    'rejections':dict(rejections),'active_instruments':active,'instruments':instruments,'historical_instruments':sorted(historical),
                    'evaluations':evaluations,'pagination':{'offset':offset,'limit':limit,'total':int(totals['evaluations'])},
                    'issues':issues,'provenance':{'parameter_set':owner['parameter_set'],'market_input_policy':state.get('market_input_policy'),
                        'market_input_binding':state.get('market_input_binding'),'execution_eligibility':'NOT_ASSERTED_BY_OBSERVATION'}}

    @staticmethod
    def retained_instruments(state,key):
        if state.get('evaluators'):return [k.split('|',1)[-1] for k in state['evaluators'] if k.startswith(str(key)+'|')]
        return [k.split(':',1)[-1] for k in state.get('streams',{}) if k.startswith(str(key)+':')]

    @staticmethod
    def active_instruments(state,owner,key):
        health=state.get('health') or {};runtime=state.get('runtime') or {}
        cycle=timestamp(runtime.get('last_cycle_at'))
        if cycle and health:
            observed=[symbol for symbol,h in health.items() if timestamp(h.get('last_observed_at')) and abs((cycle-timestamp(h['last_observed_at'])).total_seconds())<=330]
            if observed:return sorted(observed)
        symbols=((state.get('manifest') or {}).get('config') or {}).get('symbols')
        if symbols:return sorted(symbols)
        return sorted(x if isinstance(x,str) else x.get('canonical_instrument','') for x in owner['instruments'])

    @staticmethod
    def market_status(state,symbol,now,asset,key):
        from .library_market_schedule import expected_weekend
        stored=(state.get('streams') or {}).get(str(key)+':'+symbol)
        if stored:
            stream=stored
        else:
            health=(state.get('health') or {}).get(symbol) or {}
            bars=(state.get('bars') or {}).get(symbol) or {}
            series={tf:[{'close_timestamp':int(b['time'])+step} for b in values] for tf,step in [('H1',3600),('M15',900)] if (values:=bars.get(tf))}
            observed=timestamp(health.get('last_observed_at'))
            stream={'last_observed_at':observed.timestamp() if observed else None,'series':series}
        if expected_weekend(asset,now.timestamp(),stream):return 'EXPECTED_CLOSED'
        return 'CONTINUOUS_MARKET' if asset=='CRYPTO' else 'UNKNOWN'

    @staticmethod
    def evaluation_row(row):
        identity,instrument,observed,evaluation,signal_id,published=row[:6]
        p=evaluation.get('provenance') or {}
        return {'id':identity,'instrument':instrument,'observed_at':iso(observed),
            'decision_time':iso(timestamp(evaluation.get('decision_time'))),
            'reason':reason_of(evaluation),'result':lifecycle(evaluation,signal_id,published),
            'signal_id':signal_id,'publication_state':published,'trace_fidelity':evaluation.get('trace_fidelity'),
            'entry_timing':p.get('entry_timing')}

    def detail(self,row,state,source):
        item=self.evaluation_row(row);evaluation=row[3];p=evaluation.get('provenance') or {}
        item.update({'trace':evaluation.get('trace'),'provenance':{k:p[k] for k in SAFE_PROVENANCE if k in p},
            'strategy_version':evaluation.get('strategy_version'),'parameter_set_id':evaluation.get('parameter_set_id'),
            'timeline':{'decision_time':item['decision_time'],'observed_at':item['observed_at'],
                        'signal_emitted_at':iso(row[6]),'ingested_at':iso(row[7]),'published_at':iso(row[8])},
            'execution':{'eligible':(row[9] or {}).get('execution_eligible',p.get('execution_eligible')),'research_only':(row[9] or {}).get('research_only',p.get('research_only'))},
            'evidence_note':'Only recorded stages are shown. Absent checks and market evidence are unavailable, not passed.',
            'candles':[],'derived_evidence':None})
        if source=='PA_OBSERVER':
            key=PA.get(evaluation.get('strategy_id'));record=(state.get('evaluators') or {}).get(str(key)+'|'+item['instrument']) or {}
            events=(record.get('snapshot') or {}).get('events') or [];cutoff=timestamp(evaluation.get('decision_time'))
            if cutoff:
                eligible=[e for e in events if e.get('timeframe')=='M15' and e.get('close_timestamp',math.inf)<=cutoff.timestamp()]
                matched=next((e for e in reversed(eligible) if e.get('close_timestamp')==cutoff.timestamp() and
                    (e.get('provenance') or {}).get('snapshot_id')==p.get('data_snapshot_id')),None)
                if matched:
                    item['candles']=[{k:e[k] for k in ('open_timestamp','close_timestamp','open','high','low','close')} for e in eligible[-30:]]
                if matched and item['reason']=='ENGULFING_BODY_TOO_SMALL':
                    tick=(matched.get('provenance') or {}).get('tick_size');minimum=(p.get('parameter_values') or {}).get('minimum_engulfing_body_ticks')
                    if isinstance(tick,(int,float)) and isinstance(minimum,(int,float)):
                        item['derived_evidence']={'label':'Body-size check','observed':abs(matched['close']-matched['open']),
                            'required_minimum':tick*minimum,'tick_size_used':tick,
                            'basis':'Derived from matching retained input event and recorded parameters; not a persisted stage result.'}
        return item
