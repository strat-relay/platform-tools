import ast
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from research.strategy_library_forward import (ForwardObserver, PostgresResearchLedger,
                                              RunConfig, readiness)
from research.strategy_library_fixtures import snapshot
from strategy_backtest.library import BY_KEY
from strategy_backtest.library.runtime import STEPS


def cached(d, bars, context, now):
    series = {d.context: context, d.execution: bars}
    return SimpleNamespace(
        symbol_info={'tick_size': .01}, cache_age_seconds=1,
        gap_status='NONE', recovery_status='NONE', continuity_status='CONTINUOUS',
        bars={tf: SimpleNamespace(bars=[{'time': b.open_timestamp, 'open': b.open,
              'high': b.high, 'low': b.low, 'close': b.close, 'tick_volume': 10}
             for b in rows if b.close_timestamp <= now]) for tf, rows in series.items()})


class LibraryForwardTests(unittest.TestCase):
    def observer(self, key='donchian_breakout'):
        d = BY_KEY[key]
        bars, context = snapshot(d)
        symbol = bars[-1].canonical_instrument
        config = RunConfig('test-run', bars[-2].close_timestamp, (key,), {symbol: symbol+'m'})
        return ForwardObserver(config, artifact='frozen'), d, bars, context, symbol

    def bootstrap(self, observer, d, bars, context, symbol):
        now = bars[-2].close_timestamp + 1
        return observer.process(d.key, symbol, cached(d, bars[:-1], context, now), observed_at=now)

    def test_bootstrap_is_warmup_only_then_forward_proposal(self):
        observer, d, bars, context, symbol = self.observer()
        self.assertEqual(self.bootstrap(observer, d, bars, context, symbol), [])
        now = bars[-1].close_timestamp + 1
        result = observer.process(d.key, symbol, cached(d, bars, context, now), observed_at=now)
        self.assertTrue(any(r['payload']['provenance'].get('proposal') for r in result))
        self.assertTrue(all(r['evidence_class']=='PROSPECTIVE_SETUP_OBSERVATION' for r in result))
        self.assertTrue(all(r['execution_eligible'] is False for r in result))

    def test_restart_and_repeated_snapshot_are_idempotent(self):
        observer, d, bars, context, symbol = self.observer()
        self.bootstrap(observer, d, bars, context, symbol)
        now = bars[-1].close_timestamp+1
        view = cached(d, bars, context, now)
        checkpoint = deepcopy(observer.state)
        first = observer.process(d.key, symbol, view, observed_at=now)
        other = ForwardObserver(observer.config, artifact='frozen')
        other.restore(checkpoint)
        self.assertEqual(first, other.process(d.key, symbol, view, observed_at=now))
        self.assertEqual(other.process(d.key, symbol, view, observed_at=now+5), [])

    def test_revisions_fail_before_state_mutation(self):
        observer, d, bars, context, symbol = self.observer()
        self.bootstrap(observer, d, bars, context, symbol)
        before = deepcopy(observer.state)
        view = cached(d, bars, context, bars[-1].close_timestamp+1)
        view.bars[d.execution].bars[0]['close'] += .01
        with self.assertRaisesRegex(ValueError, 'revision'):
            observer.process(d.key, symbol, view, observed_at=bars[-1].close_timestamp+1)
        self.assertEqual(before, observer.state)

    def test_context_arriving_after_execution_is_buffered(self):
        observer, d, bars, context, symbol = self.observer()
        self.bootstrap(observer, d, bars, context, symbol)
        now = bars[-1].close_timestamp+1
        view = cached(d, bars, context, now)
        view.bars[d.context].bars = view.bars[d.context].bars[:-1]
        self.assertEqual(observer.process(d.key, symbol, view, observed_at=now), [])
        self.assertEqual(observer.state['streams'][d.key+':'+symbol]['last_close'], bars[-2].close_timestamp)
        expected = ForwardObserver(observer.config, artifact='frozen')
        self.bootstrap(expected, d, bars, context, symbol)
        complete = cached(d, bars, context, now)
        self.assertEqual(observer.process(d.key, symbol, complete, observed_at=now+5),
                         expected.process(d.key, symbol, complete, observed_at=now+5))

    def test_late_setup_is_recovery_evidence(self):
        observer, d, bars, context, symbol = self.observer()
        self.bootstrap(observer, d, bars, context, symbol)
        now = bars[-1].close_timestamp+200
        records = observer.process(d.key, symbol, cached(d, bars, context, now), observed_at=now)
        self.assertTrue(records)
        self.assertTrue(all(r['evidence_class']=='RECOVERY_RESEARCH_EVIDENCE' for r in records))

    def test_entry_is_reconstructed_and_outcome_stays_theoretical(self):
        observer, d, bars, context, symbol = self.observer()
        self.bootstrap(observer, d, bars, context, symbol)
        now = bars[-1].close_timestamp+1
        observer.process(d.key, symbol, cached(d, bars, context, now), observed_at=now)
        start = bars[-1].close_timestamp
        entry = replace(bars[-1], open_timestamp=start, close_timestamp=start+900,
                        open=bars[-1].close, high=bars[-1].close+.1,
                        low=bars[-1].close-.1, close=bars[-1].close)
        now = entry.close_timestamp+1
        records = observer.process(d.key, symbol, cached(d, bars+(entry,), context, now), observed_at=now)
        entries = [r for r in records if r['kind']=='EVALUATION' and r['payload']['provenance'].get('research_entry')]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['evidence_class'], 'FORWARD_COMPLETED_CANDLE_RECONSTRUCTION')
        self.assertTrue(any(r['kind']=='THEORETICAL_OUTCOME' for r in records))

    def test_all_m15_definitions_use_the_same_observer(self):
        for key, d in BY_KEY.items():
            if d.execution != 'M15':
                continue
            with self.subTest(key=key):
                observer, d, bars, context, symbol = self.observer(key)
                self.bootstrap(observer, d, bars, context, symbol)
                now = bars[-1].close_timestamp+1
                records = observer.process(key, symbol, cached(d, bars, context, now), observed_at=now)
                self.assertTrue(records)

    def test_readiness_blocks_daily_timeframes(self):
        config = RunConfig('all', 1, tuple(BY_KEY), {'XAUUSD':'XAUUSDm'})
        report = readiness(config)
        self.assertEqual(len(report), 44)
        self.assertFalse(next(r for r in report if r['key']=='daily_ema_pullback')['cache_supported'])

    def test_run_identity_is_immutable(self):
        observer, *_ = self.observer()
        other = ForwardObserver(replace(observer.config, run_id='another'), artifact='frozen')
        with self.assertRaises(ValueError): other.restore(observer.state)

    def test_no_execution_or_signal_publication_dependencies(self):
        import research.strategy_library_forward as module
        tree = ast.parse(Path(module.__file__).read_text())
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertFalse(any(s and s.startswith(('execution', 'orchestration', 'broker')) for s in imports))
        source = Path(module.__file__).read_text()
        self.assertNotIn('INSERT INTO strategy.', source)
        self.assertNotIn('INSERT INTO execution', source)

    def test_ledger_saves_records_without_committing_separately(self):
        conn = MagicMock()
        ledger = PostgresResearchLedger(conn, 'run')
        ledger.save({'binding':'frozen'}, [{'event_id':'id','kind':'EVALUATION',
                    'strategy_key':'donchian_breakout','instrument':'XAUUSD',
                    'observed_at':1,'execution_eligible':False}])
        self.assertEqual(conn.cursor.return_value.__enter__.return_value.execute.call_count, 2)
        conn.commit.assert_not_called()

    def test_stale_or_unhealthy_cache_and_tick_metadata_fail_atomically(self):
        for change in ('stale', 'gap', 'tick', 'future'):
            with self.subTest(change=change):
                observer, d, bars, context, symbol = self.observer()
                self.bootstrap(observer, d, bars, context, symbol)
                before = deepcopy(observer.state)
                now = bars[-1].close_timestamp+1
                view = cached(d, bars, context, now)
                if change == 'stale': view.cache_age_seconds = 331
                if change == 'gap': view.gap_status = 'GAP_DETECTED'
                if change == 'tick': view.symbol_info = {'point': .01}
                if change == 'future': view.bars[d.execution].bars[-1]['time'] += 900
                with self.assertRaises(ValueError):
                    observer.process(d.key, symbol, view, observed_at=now)
                self.assertEqual(before, observer.state)

    def test_prefix_retention_cap_fails_instead_of_reseeding(self):
        observer, d, bars, context, symbol = self.observer()
        self.bootstrap(observer, d, bars, context, symbol)
        # Model an explicitly configured cap reached after a long run.
        observer.config = replace(observer.config, max_retained_bars=320)
        extended = tuple(replace(bars[0], open_timestamp=bars[0].open_timestamp-900*i,
                                 close_timestamp=bars[0].close_timestamp-900*i)
                         for i in range(70, 0, -1)) + bars
        now = bars[-1].close_timestamp+1
        with self.assertRaisesRegex(ValueError, 'history limit'):
            observer.process(d.key, symbol, cached(d, extended, context, now), observed_at=now)

    def test_activation_and_artifact_changes_reject_saved_state(self):
        observer, *_ = self.observer()
        for other in (ForwardObserver(observer.config, artifact='changed'),
                      ForwardObserver(replace(observer.config, activation_time=1), artifact='frozen')):
            with self.assertRaises(ValueError): other.restore(observer.state)

    def test_canonical_tick_size_does_not_use_display_point(self):
        observer, d, bars, context, symbol = self.observer()
        now = bars[-2].close_timestamp+1
        view = cached(d, bars[:-1], context, now)
        view.symbol_info = {'tick_size': .01, 'point': .001, 'trade_tick_size': None}
        observer.process(d.key, symbol, view, observed_at=now)
        runtime = observer.state['streams'][d.key+':'+symbol]['runtime']
        self.assertEqual(runtime['state']['tick_sizes'][symbol], .01)
