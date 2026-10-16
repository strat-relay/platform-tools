from copy import deepcopy
from datetime import datetime, timezone
import unittest

from platform_api.library_observation import observation_status
from platform_api.library_market_schedule import expected_weekend


def epoch(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp())


def state_at(close, now):
    stored={'last_close':close-900,'last_observed_at':close,
            'series':{'M15':[{'close_timestamp':close-900}],
                      'H1':[{'close_timestamp':close-3600}]}}
    result={'health':{'observed_at':now,'status':'DEGRADED','streams':[]},'streams':{}}
    for symbol in ('XAUUSD','USDJPY','BTCUSD'):
        result['health']['streams'].append({'key':'k','instrument':symbol,
            'status':'OK' if symbol=='BTCUSD' else 'BLOCKED','error_type':'ValueError',
            **({'last_finalized_close':now//900*900} if symbol=='BTCUSD' else {})})
        result['streams']['k:'+symbol]=deepcopy(stored)
    return result


class WeekendDisplayTests(unittest.TestCase):
    def test_friday_closure_does_not_degrade_healthy_btc(self):
        now=epoch('2026-10-16T22:47:00');close=epoch('2026-10-16T21:00:00')
        state=state_at(close,now);before=deepcopy(state)
        result=observation_status(state,'k',now=now)
        self.assertEqual(result['status'],'RUNNING')
        self.assertEqual(result['closed_instruments'],['USDJPY','XAUUSD'])
        self.assertEqual(result['raw_status'],'DEGRADED')
        self.assertEqual(result['last_finalized_close'],now//900*900)
        self.assertEqual(state,before)

    def test_all_closed_is_explicit_not_evaluating(self):
        now=epoch('2026-10-17T12:00:00');close=epoch('2026-10-16T21:00:00')
        state=state_at(close,now)
        state['health']['streams']=state['health']['streams'][:2]
        self.assertEqual(observation_status(state,'k',now=now)['status'],'MARKET_CLOSED')

    def test_crypto_staleness_still_degrades_on_weekend(self):
        now=epoch('2026-10-17T12:00:00');close=epoch('2026-10-16T21:00:00')
        state=state_at(close,now)
        state['health']['streams'][-1]['status']='BLOCKED'
        self.assertEqual(observation_status(state,'k',now=now)['status'],'DEGRADED')
        self.assertIsNone(expected_weekend('BTCUSD',now,state['streams']['k:BTCUSD']))

    def test_preclose_outage_and_incomplete_coverage_are_not_hidden(self):
        now=epoch('2026-10-16T22:47:00');close=epoch('2026-10-16T21:00:00')
        for mutation in ('old_observation','missing_bar','missing_series','different_error'):
            state=state_at(close,now)
            stored=state['streams']['k:XAUUSD']
            if mutation=='old_observation': stored['last_observed_at']=close-3600
            if mutation=='missing_bar': stored['series']['M15'][-1]['close_timestamp']-=900
            if mutation=='missing_series': stored['series'].pop('H1')
            if mutation=='different_error': state['health']['streams'][0]['error_type']='KeyError'
            with self.subTest(mutation=mutation):
                self.assertEqual(observation_status(state,'k',now=now)['status'],'DEGRADED')

    def test_dead_observer_remains_stale_during_closure(self):
        now=epoch('2026-10-17T12:00:00');close=epoch('2026-10-16T21:00:00')
        state=state_at(close,now);state['health']['observed_at']=now-331
        self.assertEqual(observation_status(state,'k',now=now)['status'],'STALE')

    def test_open_session_and_sunday_reopening_do_not_waive_staleness(self):
        close=epoch('2026-10-16T21:00:00')
        for stamp in ('2026-10-16T20:59:59','2026-10-18T21:00:00','2026-10-19T09:00:00'):
            now=epoch(stamp)
            state=state_at(close,now)
            with self.subTest(stamp=stamp):
                self.assertEqual(observation_status(state,'k',now=now)['status'],'DEGRADED')

    def test_winter_dst_and_unknown_symbol_fail_closed(self):
        now=epoch('2026-12-11T23:00:00');close=epoch('2026-12-11T22:00:00')
        state=state_at(close,now)
        self.assertEqual(observation_status(state,'k',now=now)['status'],'RUNNING')
        self.assertIsNone(expected_weekend('UNKNOWN',now,state['streams']['k:XAUUSD']))

    def test_dst_transition_weekend_uses_sunday_offset(self):
        close=epoch('2026-03-06T22:00:00')
        before=epoch('2026-03-08T20:59:59')
        after=epoch('2026-03-08T21:00:00')
        stored=state_at(close,before)['streams']['k:XAUUSD']
        self.assertIsNotNone(expected_weekend('XAUUSD',before,stored))
        self.assertIsNone(expected_weekend('XAUUSD',after,stored))
