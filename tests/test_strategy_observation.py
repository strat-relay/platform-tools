from copy import deepcopy
from datetime import datetime,timedelta,timezone
from unittest.mock import MagicMock,patch
import pytest

from platform_api.strategy_observation import ObservationRepository,period,lifecycle,reason_of
from platform_api.control import PlatformControlApi

NOW=datetime(2026,10,17,16,30,tzinfo=timezone.utc)
IID='new-pa-instance'
SID='PRICE_ACTION_ENGULFING_CONTINUATION'


class Cursor:
    def __init__(self,state=None,groups=None,rows=None,detail=None,owned=True):
        self.state=state or {};self.groups=groups or [];self.rows=rows or [];self.detail=detail
        self.owned=owned;self.calls=[];self.result=[]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql,args=()):
        self.calls.append((sql,args));self.result=[]
        if 'FROM strategy_mgmt.strategy_instance_v2' in sql:
            if self.owned:self.result=[(IID,'price_action_engulfing_continuation_v1',{},['BTCUSD','XAUUSD','USDJPY'],'frozen-params','PRICE_ACTION_ENGULFING_CONTINUATION_V1')]
        elif 'FROM platform.strategy_instance' in sql:self.result=[]
        elif 'SELECT state,last_observed_at' in sql:self.result=[(self.state,NOW)]
        elif 'SELECT min(observed_at)' in sql:self.result=[(NOW-timedelta(days=3),)]
        elif 'GROUP BY f.instrument' in sql:self.result=self.groups
        elif 'LIMIT %s OFFSET %s' in sql:self.result=self.rows
        elif 'WHERE f.event_id=%s' in sql:self.result=[self.detail] if self.detail else []
        elif "candidate_id'=%s" in sql:self.result=[]
        elif 'SELECT canonical_instrument,asset_class' in sql:self.result=[('BTCUSD','CRYPTO'),('XAUUSD','METAL'),('USDJPY','FX')]
    def fetchall(self):return self.result
    def fetchone(self):return self.result[0] if self.result else None


def repository(cursor):
    conn=MagicMock();conn.__enter__.return_value=conn;conn.cursor.return_value=cursor
    connect=MagicMock(return_value=conn)
    return ObservationRepository(connect,clock=lambda:NOW),connect


def test_dates_are_utc_aware_half_open_and_custom_is_strict():
    start,end,_=period({'period':['TODAY']},NOW)
    assert start==NOW.replace(hour=0,minute=0) and end==NOW
    for query in [{'period':['CUSTOM'],'from':['2026-10-17'],'to':['2026-10-18']},
                  {'period':['CUSTOM'],'from':['2026-10-18T00:00:00Z'],'to':['2026-10-17T00:00:00Z']},
                  {'period':['UNKNOWN']}]:
        with pytest.raises(ValueError):period(query,NOW)
    start,end,_=period({'period':['CUSTOM'],'from':['2026-10-17T08:00:00-06:00'],'to':['2026-10-17T09:00:00-06:00']},NOW)
    assert start.hour==14 and end.hour==15


def test_instance_ownership_is_required_before_any_ledger_read():
    cursor=Cursor(owned=False);repo,connect=repository(cursor)
    assert repo.read(SID,'foreign-instance',{}) is None
    assert not any('observer_evaluation' in sql for sql,_ in cursor.calls)
    connect.assert_called_once_with(readonly=True)


def test_full_range_counts_do_not_come_from_capped_page_and_historical_streams_are_separate():
    state={'runtime':{'last_cycle_at':NOW.isoformat(),'instrument_count':3},
           'health':{symbol:{'last_observed_at':(NOW if symbol!='EURUSD' else NOW-timedelta(days=2)).isoformat()} for symbol in ('BTCUSD','XAUUSD','USDJPY','EURUSD')},
           'evaluators':{'engulfing_continuation|'+symbol:{} for symbol in ('BTCUSD','XAUUSD','USDJPY','EURUSD')}}
    groups=[('BTCUSD','NO_CONFIRMED_TREND','NO_CANDIDATE',False,None,500,NOW),
            ('BTCUSD','ENTRY_OBSERVED_RESEARCH_ONLY','REJECT',True,'PUBLISHED',1,NOW)]
    evaluation={'decision':'NO_CANDIDATE','decision_time':NOW.isoformat(),'provenance':{'rejection_reason':'NO_CONFIRMED_TREND'}}
    cursor=Cursor(state,groups,[('one-row','BTCUSD',NOW,evaluation,None,None)]);repo,_=repository(cursor)
    report=repo.read(SID,IID,{'period':['24H'],'limit':['1']})
    assert report['counts']['evaluations']==501 and len(report['evaluations'])==1
    assert report['counts']['accepted_entries']==1 and report['counts']['published']==1
    assert report['historical_instruments']==['EURUSD']
    assert report['active_instruments']==['BTCUSD','USDJPY','XAUUSD']
    assert report['current']['status']=='RUNNING'
    assert report['issues'][0]['code']=='UNVERIFIED_INPUT_CONTRACT'
    sql,args=next((sql,args) for sql,args in cursor.calls if 'GROUP BY f.instrument' in sql)
    assert 'observed_at >= %s AND observed_at < %s' in sql
    assert args[0:2]==(IID,'engulfing_continuation')
    assert NOW-timedelta(hours=24) in args and NOW in args
    assert 'PUBLISHED' not in sql # publication status is read, never assumed
    assert all(sql.lstrip().startswith(('SELECT','WITH','SET')) for sql,_ in cursor.calls)


def test_date_filter_does_not_turn_current_health_into_historical_health():
    cursor=Cursor({'runtime':{'last_cycle_at':(NOW-timedelta(hours=2)).isoformat()}});repo,_=repository(cursor)
    report=repo.read(SID,IID,{'period':['CUSTOM'],'from':['2026-10-14T00:00:00Z'],'to':['2026-10-15T00:00:00Z']})
    assert report['current']['status']=='STALE' and report['range']['preset']=='CUSTOM'


def test_run_boundary_pagination_and_foreign_detail_are_enforced():
    cursor=Cursor();repo,_=repository(cursor)
    for query in ({'run_id':['other-run']},{'limit':['101']},{'offset':['-1']}):
        with pytest.raises(ValueError):repo.read(SID,IID,query)
    assert repo.read(SID,IID,{},event_id='foreign-evaluation') is None
    sql,args=next((sql,args) for sql,args in cursor.calls if 'WHERE f.event_id=%s' in sql)
    assert args[-1]=='foreign-evaluation' and IID in args


def test_detail_does_not_fabricate_missing_stages_or_future_candles():
    at=int(NOW.timestamp());p={'rejection_reason':'ENGULFING_BODY_TOO_SMALL','data_snapshot_id':'original',
       'parameter_values':{'minimum_engulfing_body_ticks':4},'research_only':True,'execution_eligible':False}
    evaluation={'strategy_id':SID,'decision_time':str(at),'provenance':p,'trace':{'stages':[]}}
    events=[{'timeframe':'M15','open_timestamp':at-900,'close_timestamp':at,'open':100,'high':101,'low':99,'close':100.002,
             'provenance':{'snapshot_id':'original','tick_size':.001}},
            {'timeframe':'M15','open_timestamp':at,'close_timestamp':at+900,'open':999,'high':999,'low':999,'close':999}]
    state={'evaluators':{'engulfing_continuation|BTCUSD':{'snapshot':{'events':events}}}}
    repo,_=repository(Cursor());before=deepcopy(state)
    row=('e','BTCUSD',NOW,evaluation,None,None,None,None,None,None)
    detail=repo.detail(row,state,'PA_OBSERVER')
    assert detail['trace']['stages']==[] and len(detail['candles'])==1
    assert detail['derived_evidence']['required_minimum']==.004
    assert detail['execution']['eligible'] is False
    assert state==before
    evaluation['provenance']['data_snapshot_id']='different'
    assert repo.detail(row,state,'PA_OBSERVER')['derived_evidence'] is None
    assert repo.detail(row,state,'PA_OBSERVER')['candles'] == []


def test_strategy_qualification_and_publication_are_not_conflated():
    accepted={'decision':'REJECT','provenance':{'rejection_reason':'ENTRY_OBSERVED_RESEARCH_ONLY'}}
    assert lifecycle(accepted)=='ACCEPTED_AWAITING_PUBLICATION'
    assert lifecycle(accepted,'signal','PENDING')=='CANONICAL_CREATED'
    assert lifecycle(accepted,'signal','PUBLISHED')=='PUBLISHED'
    assert lifecycle({'provenance':{'entry_cancelled':True}})=='CANCELLED'


def test_api_routes_are_read_only_and_do_not_expose_internal_failure_details():
    api=PlatformControlApi(repository=MagicMock(),environ={})
    path=f'/api/v1/strategies/{SID}/instances/{IID}/observation'
    assert api.execute('POST',path)[0]==405
    with patch('platform_api.strategy_observation.ObservationRepository.read',return_value={'counts':{'evaluations':0}}):
        assert api.execute('GET',path)[0]==200
    with patch('platform_api.strategy_observation.ObservationRepository.read',side_effect=RuntimeError('secret connection details')):
        status,body=api.execute('GET',path)
        assert status==503 and 'secret connection' not in str(body)


def test_library_and_generic_sources_keep_ownership_and_coverage_distinct():
    repo,_=repository(Cursor())
    cur=Cursor()
    owner={'id':IID,'canonical_id':'legacy-instance','attributes':{},'instruments':[]}
    _,run,_,sql,args,coverage,source=repo.source(cur,owner,'OTHER')
    assert coverage=='PARTIAL' and source=='CANONICAL_EVALUATIONS'
    assert args==['OTHER','legacy-instance','legacy-instance']
    assert "e.provenance->>'strategy_instance_id'=%s" in sql
    owner['attributes']={'library_key':'ema_pullback','orchestrator_run_id':'frozen-run'}
    _,run,key,sql,args,coverage,source=repo.source(cur,owner,'TREND_FOLLOWING_EMA_PULLBACK')
    assert run=='frozen-run' and key=='ema_pullback' and coverage=='COMPLETE_LEDGER'
    assert args==['frozen-run','ema_pullback'] and "kind='EVALUATION'" in sql
    assert lifecycle({'decision':'REJECT','provenance':{'reason':'ENTRY_ACCEPTED'}})=='ACCEPTED_AWAITING_PUBLICATION'


def test_library_source_recovers_missing_run_id_from_authoritative_forward_ledger():
    class LibraryCursor(Cursor):
        def execute(self, sql, args=()):
            if 'SELECT run_id' in sql and 'strategy_library_forward_event' in sql:
                self.result=[('donchian-run',)]
                self.calls.append((sql,args))
                return
            if 'SELECT state FROM research.strategy_library_forward_state' in sql:
                self.result=[({'health': {}},)]
                self.calls.append((sql,args))
                return
            super().execute(sql,args)

    repo,_=repository(LibraryCursor())
    owner={'id': 'library-instance', 'canonical_id': 'library-instance',
           'attributes': {'library_key': 'donchian_breakout'}, 'instruments': []}
    state,run,key,sql,args,coverage,source=repo.source(LibraryCursor(),owner,'BREAKOUT_DONCHIAN_BREAKOUT')
    assert run=='donchian-run'
    assert key=='donchian_breakout'
    assert coverage=='COMPLETE_LEDGER'
    assert source=='LIBRARY_OBSERVER'
    assert args==['donchian-run','donchian_breakout']
