from copy import deepcopy
from dataclasses import replace
import unittest

from core.strategies.entry_timing import CandleOpenObservation
from core.strategies.evaluation import canonical_hash
from strategy_backtest.library.catalog import BY_KEY
from strategy_backtest.library.donchian_v2 import DEFINITION, DonchianV2Runtime
from strategy_backtest.library.breakout_retest import transition
from strategy_backtest.models import MarketEvent

BASE=1800000000


def bar(at,o,h,l,c,instrument='TEST',tf='M15'):
    return MarketEvent(instrument,tf,at,at+(900 if tf=='M15' else 3600),o,h,l,c)


def context():
    wave=(0,2,4,2,0,1)
    return tuple(bar(BASE+(i-48)*3600,80+i*.3+wave[i%6],81+i*.3+wave[i%6],
                     79+i*.3+wave[i%6],80.2+i*.3+wave[i%6],tf='H1') for i in range(48))


def initial():
    warm=tuple(bar(BASE+(i-41)*900,100,101,99,100) for i in range(40))
    return warm+(bar(BASE-900,101,104.5,100.5,104),)


def run_initial(runtime=None):
    r=runtime or DonchianV2Runtime(dataset_id='synthetic-only',artifact_digest='test-artifact')
    bars=initial(); ctx=context()
    first=r.advance(bars,ctx,tick_size=.01)
    bars+=(bar(BASE,103,104,100.9,103.8),)
    retest=r.advance(bars,ctx,tick_size=.01)
    return r,bars,ctx,first,retest


class DonchianV2Tests(unittest.TestCase):
    def test_shared_synthetic_replay_reconciles(self):
        from research.replay_donchian_v2 import synthetic, run
        bars,ctx=synthetic()
        result=run(bars,ctx,tick=.01,dataset='synthetic',artifact='test',version='V2')
        self.assertEqual(result['reason_counts']['PROPOSAL'],2)
        self.assertEqual(result['accepted_entries'],2)
        self.assertEqual(result['pending_entries'],0)
        self.assertEqual(sum(result['outcome_counts'].values()),2)
        self.assertEqual([x['proposal']['evidence']['kind'] for x in result['evaluations'] if x['proposal']],['INITIAL','LAYER'])

    def test_v1_checkpoint_and_parameter_changes_cannot_resume_v2(self):
        from strategy_backtest.library.runtime import ComposedResearchRuntime
        old=ComposedResearchRuntime(BY_KEY['donchian_breakout'],dataset_id='same',artifact_digest='same')
        new=DonchianV2Runtime(dataset_id='same',artifact_digest='same')
        with self.assertRaises(ValueError):new.restore_state(old.snapshot_state())
        changed=DonchianV2Runtime(dataset_id='same',artifact_digest='same',definition=replace(DEFINITION,min_breakout_atr=.3))
        with self.assertRaises(ValueError):changed.restore_state(new.snapshot_state())

    def test_first_break_never_enters_and_later_retest_proposes(self):
        r,b,c,first,retest=run_initial()
        self.assertEqual(first[-1].provenance['reason'],'AWAITING_SEQUENCE_CONFIRMATION')
        self.assertNotIn('proposal',first[-1].provenance)
        self.assertEqual(retest[-1].provenance['reason'],'PROPOSAL')
        self.assertEqual(retest[-1].provenance['proposal']['evidence']['kind'],'INITIAL')
        self.assertEqual(len(r.accepted_entries()),0)

    def test_entry_candle_only_uses_open(self):
        r,b,c,_,_=run_initial()
        checkpoint=r.snapshot_state()
        opening=bar(BASE+900,103.8,105,103.4,104.5)
        records=r.advance(b+(opening,),c,tick_size=.01)
        self.assertEqual(records[0].provenance['reason'],'ENTRY_ACCEPTED')
        other=DonchianV2Runtime(dataset_id='synthetic-only',artifact_digest='test-artifact')
        other.restore_state(checkpoint)
        different=replace(opening,high=120,low=95,close=96)
        other.advance(b+(different,),c,tick_size=.01)
        self.assertEqual(r.accepted_entries()[0].entry_price,other.accepted_entries()[0].entry_price)
        self.assertEqual(r.accepted_entries()[0].stop_price,other.accepted_entries()[0].stop_price)

    def test_fresh_pullback_then_progress_allows_layer(self):
        r,b,c,_,_=run_initial()
        for x in (bar(BASE+900,103.8,105,103.4,104.5),
                  bar(BASE+1800,104.5,106,104.2,105.8)):
            b+=(x,);out=r.advance(b,c,tick_size=.01)
            self.assertNotIn('proposal',out[-1].provenance)
        c+=(bar(BASE,95,96,94,95.2,tf='H1'),)
        b+=(bar(BASE+2700,105.8,105.9,104.8,105.0),)
        out=r.advance(b,c,tick_size=.01)
        self.assertEqual(r.snapshot_state()['state']['breakout_episodes']['TEST']['phase'],'PULLBACK')
        self.assertNotIn('proposal',out[-1].provenance)
        b+=(bar(BASE+3600,105,107,104.9,106.8),)
        out=r.advance(b,c,tick_size=.01)
        self.assertEqual(out[-1].provenance['proposal']['evidence']['kind'],'LAYER')
        b+=(bar(BASE+4500,106.8,107.5,106.5,107.2),)
        out=r.advance(b,c,tick_size=.01)
        self.assertEqual(out[0].provenance['reason'],'ENTRY_ACCEPTED')
        self.assertEqual(len(r.accepted_entries()),2)
        self.assertGreater(r.accepted_entries()[1].entry_price,r.accepted_entries()[0].entry_price)

    def test_continuous_advance_does_not_layer(self):
        r,b,c,_,_=run_initial()
        for x in (bar(BASE+900,103.8,105,103.4,104.5),bar(BASE+1800,104.5,106,104.2,105.8)):
            b+=(x,);out=r.advance(b,c,tick_size=.01)
            self.assertNotIn('proposal',out[-1].provenance)
        self.assertEqual(len(r.accepted_entries()),1)

    def test_restart_idempotency_and_identity(self):
        r,b,c,_,_=run_initial()
        saved=r.snapshot_state()
        other=DonchianV2Runtime(dataset_id='synthetic-only',artifact_digest='test-artifact')
        other.restore_state(saved)
        new=b+(bar(BASE+900,103.8,105,103.4,104.5),)
        a=r.advance(new,c,tick_size=.01);z=other.advance(new,c,tick_size=.01)
        self.assertEqual([v.to_dict() for v in a],[v.to_dict() for v in z])
        self.assertEqual(other.advance(new,c,tick_size=.01),())
        wrong=DonchianV2Runtime(dataset_id='other',artifact_digest='test-artifact')
        with self.assertRaises(ValueError):wrong.restore_state(saved)

    def test_invalid_revision_is_atomic(self):
        r,b,c,_,_=run_initial();saved=r.snapshot_state()
        altered=(replace(b[0],high=102),)+b[1:]+(bar(BASE+900,103.8,105,103.4,104.5),)
        with self.assertRaises(ValueError):r.advance(altered,c,tick_size=.01)
        self.assertEqual(r.snapshot_state(),saved)

    def test_missing_context_does_not_fabricate_bias(self):
        r=DonchianV2Runtime(dataset_id='data',artifact_digest='artifact')
        result=r.advance(initial(),context()[:-1],tick_size=.01)
        self.assertEqual(result[-1].provenance['reason'],'CONTEXT_UNAVAILABLE')
        self.assertEqual(r.snapshot_state()['state']['breakout_episodes']['TEST'],{})

    def test_countertrend_breakout_rejected(self):
        c=tuple(replace(x,open=200-x.open,high=200-x.low,low=200-x.high,close=200-x.close) for x in context())
        r=DonchianV2Runtime(dataset_id='data',artifact_digest='artifact')
        self.assertEqual(r.advance(initial(),c,tick_size=.01)[-1].provenance['reason'],'NO_SETUP')

    def test_first_failed_retest_cannot_be_cherry_picked(self):
        r=DonchianV2Runtime(dataset_id='data',artifact_digest='artifact')
        b=initial();c=context();r.advance(b,c,tick_size=.01)
        b+=(bar(BASE,102,103,100.9,101.1),)
        self.assertEqual(r.advance(b,c,tick_size=.01)[-1].provenance['reason'],'FIRST_RETEST_REJECTED')
        b+=(bar(BASE+900,101.1,104,100.9,103.8),)
        self.assertNotIn('proposal',r.advance(b,c,tick_size=.01)[-1].provenance)

    def test_expiry_is_bounded(self):
        state={'phase':'WAIT_RETEST','direction':1,'boundary':101,'atr':2,'break_at':BASE,'extreme':100.5}
        b=initial()+(bar(BASE+8*900,105,106,104,105.5),)
        new,reason,_=transition(state,b,bias=1,atr=2,tick=.01,policy=DEFINITION)
        self.assertEqual(reason,'SEQUENCE_EXPIRED')
        self.assertEqual(new['phase'],'WAIT_RESET')
        self.assertEqual(state['phase'],'WAIT_RETEST')

    def test_tick_change_and_future_data_rejected(self):
        r,b,c,_,_=run_initial();saved=r.snapshot_state()
        with self.assertRaises(ValueError):r.advance(b+(bar(BASE+900,103.8,105,103.4,104.5),),c,tick_size=.1)
        self.assertEqual(saved,r.snapshot_state())
        with self.assertRaises(ValueError):r.advance(b,c+(bar(BASE,95,96,94,95.2,tf='H1'),),tick_size=.01)
        self.assertEqual(saved,r.snapshot_state())

    def test_manifest_contains_policy_and_is_distinct_from_v1(self):
        self.assertIn('min_layer_progress_atr',DEFINITION.manifest())
        self.assertNotEqual(canonical_hash(DEFINITION.manifest()),canonical_hash(BY_KEY['donchian_breakout'].manifest()))
        self.assertEqual(BY_KEY['donchian_breakout'].version,'V1')
        with self.assertRaises(ValueError):replace(DEFINITION,breakout_body_fraction=1.1)

    def test_short_is_symmetric_and_missing_candle_cancels(self):
        def mirror(x):return replace(x,open=200-x.open,high=200-x.low,low=200-x.high,close=200-x.close)
        c=tuple(map(mirror,context()));b=tuple(map(mirror,initial()))
        r=DonchianV2Runtime(dataset_id='short',artifact_digest='test')
        r.advance(b,c,tick_size=.01)
        b+=(mirror(bar(BASE,103,104,100.9,103.8)),)
        out=r.advance(b,c,tick_size=.01)
        self.assertEqual(out[-1].provenance['proposal']['direction'],'SHORT')
        b+=(mirror(bar(BASE+900,103.8,105,103.4,104.5)),)
        self.assertEqual(r.advance(b,c,tick_size=.01)[0].provenance['reason'],'ENTRY_ACCEPTED')
        a,bb,cc,_,_=run_initial()
        bb+=(bar(BASE+1800,103.8,105,103.4,104.5),)
        self.assertEqual(a.advance(bb,cc,tick_size=.01)[0].provenance['reason'],'MISSING_NEXT_CANDLE')
        self.assertEqual(len(a.accepted_entries()),0)

    def test_open_gap_cancel_and_weak_breakout(self):
        r,b,c,_,_=run_initial()
        b+=(bar(BASE+900,110,111,109,110.5),)
        self.assertEqual(r.advance(b,c,tick_size=.01)[0].provenance['reason'],'ENTRY_GAP_CANCELLED')
        self.assertEqual(len(r.accepted_entries()),0)
        r=DonchianV2Runtime(dataset_id='weak',artifact_digest='test')
        b=initial()[:-1]+(bar(BASE-900,101,104.5,100.5,101.5),)
        self.assertEqual(r.advance(b,c,tick_size=.01)[-1].provenance['reason'],'BREAKOUT_QUALITY_REJECTED')

    def test_layer_actual_open_must_preserve_progress(self):
        r,b,c,_,_=run_initial()
        pending=deepcopy(r.snapshot_state()['state']['pending']['TEST'])
        pending.update(entry_reference=104.7,stop=102,target=110)
        pending['evidence'].update(kind='LAYER',previous_entry_price=103.8,episode_atr=2,prior_atr=2)
        self.assertEqual(r._entry_reason(pending,BASE+900,104.7),'ENTRY_LAYER_PROGRESS_REJECTED')
        self.assertEqual(r._entry_reason(pending,BASE+900,104.9),'ENTRY_ACCEPTED')

    def test_instruments_are_isolated(self):
        r,b,c,_,_=run_initial();saved=deepcopy(r.snapshot_state()['state']['breakout_episodes']['TEST'])
        other=tuple(replace(x,canonical_instrument='OTHER') for x in initial())
        ctx=tuple(replace(x,canonical_instrument='OTHER') for x in context())
        r.advance(other,ctx,tick_size=.01)
        self.assertEqual(r.snapshot_state()['state']['breakout_episodes']['TEST'],saved)
        self.assertNotEqual(r.snapshot_state()['state']['pending'].get('OTHER'),r.snapshot_state()['state']['pending'].get('TEST'))

    def test_observed_and_reconstructed_lifecycle_parity(self):
        a,b,c,_,_=run_initial()
        clone=DonchianV2Runtime(dataset_id='synthetic-only',artifact_digest='test-artifact')
        clone.restore_state(a.snapshot_state())
        stamp=BASE+900
        opening=CandleOpenObservation('TEST',900,stamp,103.8,stamp+1,stamp+2,'open-test','MT5_TEST')
        result=clone.observe_open(opening,setup_available_at=stamp+1,now=stamp+2)
        self.assertEqual(result[0].provenance['reason'],'ENTRY_ACCEPTED')
        b+=(bar(stamp,103.8,105,103.4,104.5),)
        a.advance(b,c,tick_size=.01);clone.advance(b,c,tick_size=.01)
        self.assertEqual(a.snapshot_state()['state']['breakout_episodes'],clone.snapshot_state()['state']['breakout_episodes'])


if __name__=='__main__':unittest.main()
