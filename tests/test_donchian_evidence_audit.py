from copy import deepcopy
from datetime import datetime, timezone
import unittest

from core.strategies.evaluation import canonical_hash
from scripts.audit_donchian_replay import audit, combine, normalise, replay, timing_audit
from strategy_backtest.library.catalog import BY_KEY


def fixture():
    bars = {i*900:dict(time=i*900,open=100+i,high=101.1+i,low=99.8+i,close=101+i)
            for i in range(41)}
    # Signal confirms at the end of bar 39; bar 40 supplies the actual next open.
    from strategy_backtest.library.compositions import detect
    from strategy_backtest.models import MarketEvent
    events = [MarketEvent('TEST','M15',i*900,(i+1)*900,100+i,101.1+i,99.8+i,101+i) for i in range(40)]
    proposal = detect(BY_KEY['donchian_breakout'],events,(),.01).proposal
    signal = dict(signal_id='SIG-test',instrument='TEST',direction='LONG',
                  decision_time=datetime.fromtimestamp(36000,timezone.utc).isoformat(),
                  entry_price=bars[36000]['open'],stop_price=proposal.stop,target_price=proposal.target)
    return signal,bars


class AuditTests(unittest.TestCase):
    def test_valid_frozen_geometry(self):
        signal,bars=fixture()
        result=replay(signal,bars,tick=.01,parameter_hash=canonical_hash(BY_KEY['donchian_breakout'].manifest()))
        for name in ('direction_matches_breakout','entry_matches_reference_open','stop_matches','target_matches','parameter_identity_matches'):
            self.assertTrue(result[name],name)
        self.assertEqual(result['shared_resolver_replay']['status'],'OPEN_AT_DATASET_END')

    def test_wrong_direction_detected(self):
        signal,bars=fixture()
        signal.update(direction='SHORT',stop_price=160,target_price=120)
        result=replay(signal,bars,tick=None,parameter_hash=None)
        self.assertFalse(result['direction_matches_breakout'])

    def test_missing_confirmation_blocks_geometry(self):
        signal,bars=fixture()
        bars.pop(38*900)
        result=replay(signal,bars,tick=.01,parameter_hash=None)
        self.assertFalse(result['confirmation_window_complete'])
        self.assertNotIn('expected_stop',result)

    def test_missing_entry_remains_gap(self):
        signal,bars=fixture()
        bars.pop(36000)
        result=replay(signal,bars,tick=None,parameter_hash=None)
        self.assertEqual(result['shared_resolver_replay']['status'],'DATA_GAP_UNRESOLVED')

    def test_no_tick_or_parameter_match_does_not_invent_stop_verification(self):
        signal,bars=fixture()
        for tick,identity in ((None,None),(.01,'different')):
            result=replay(signal,bars,tick=tick,parameter_hash=identity)
            self.assertNotIn('stop_matches',result)

    def test_conflicting_sources_explicit(self):
        _,bars=fixture()
        cached=deepcopy(bars)
        cached[0]['close']+=.01
        _,conflicts=combine(bars,cached)
        self.assertEqual(conflicts,[0])
        self.assertEqual(bars[0]['close'],101)

    def test_validation_and_forming_filter(self):
        _,bars=fixture()
        valid=list(bars.values())
        normal,forming=normalise(valid,now=36000)
        self.assertEqual(len(normal),40)
        self.assertEqual(forming,1)
        for bad in ([valid[0],valid[0]], [dict(valid[0],open=float('nan'))], [dict(valid[0],time=1)]):
            with self.assertRaises(ValueError):normalise(bad,now=36000)

    def test_late_prospective_conflict(self):
        signal,_=fixture()
        signal.update(signal_emitted_at=datetime.fromtimestamp(36930,timezone.utc).isoformat(),
                      source_provenance={'classification':'PROSPECTIVE_ORCHESTRATOR_SIGNAL'})
        result=timing_audit(signal,{'evidence_class':'FORWARD_COMPLETED_CANDLE_RECONSTRUCTION'})
        self.assertTrue(result['prospective_label_conflict'])
        self.assertEqual(result['current_policy_expectation'],'RETROSPECTIVE_RESEARCH_EVIDENCE')

    def test_fast_reconstruction_still_research(self):
        signal,_=fixture()
        signal.update(signal_emitted_at=datetime.fromtimestamp(36001,timezone.utc).isoformat(),
                      source_provenance={'signal_emission_class':'PROSPECTIVE'})
        result=timing_audit(signal,{'evidence_class':'FORWARD_COMPLETED_CANDLE_RECONSTRUCTION'})
        self.assertTrue(result['prospective_label_conflict'])

    def test_refuses_writable_transaction_before_evidence_reads(self):
        class Cursor:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def execute(self,query):self.query=query
            def fetchone(self):return ('off',)
        class Connection:
            def cursor(self):return Cursor()
        with self.assertRaisesRegex(RuntimeError,'read-only'):
            audit(Connection(),None,now=40000)


if __name__=='__main__':unittest.main()
