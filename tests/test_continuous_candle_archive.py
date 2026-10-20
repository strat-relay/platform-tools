import copy
import os
import unittest
from decimal import Decimal

from market_data_cache.archive import ArchiveConflict, ArchiveUnavailable, PostgresCandleArchive, normalise
from market_data_cache.collector import MarketDataCollector
from market_data_cache.store import MarketDataStore
from tests.test_market_data_cache import FakeBridge, START, _bar
from tests.test_market_data_recovery import MemoryRedis, RangeBridge


class ArchiveValidationTests(unittest.TestCase):
    def bar(self, stamp=0):
        return dict(time=stamp,open='100',high='102',low='99',close='101')

    def test_repeated_prices_are_idempotent_without_hiding_revisions(self):
        a = self.bar()
        self.assertEqual(len(normalise({'M5':[a,copy.deepcopy(a)]},300)),1)
        revised = dict(a,close='100.5')
        with self.assertRaises(ArchiveConflict):
            normalise({'M5':[a,revised]},300)

    def test_forming_off_grid_nonfinite_geometry_and_precision_rejected(self):
        bars = [dict(self.bar(),time=1),dict(self.bar(),is_closed=False),
                dict(self.bar(),high='NaN'),dict(self.bar(),low='103'),
                dict(self.bar(),close='100.000000001'),dict(self.bar(),time=300)]
        for bar in bars:
            with self.subTest(bar=bar),self.assertRaises(ArchiveUnavailable):
                normalise({'M5':[bar]},300)

    def test_archive_input_is_not_mutated(self):
        batches = {'M5':[self.bar()]}
        original = copy.deepcopy(batches)
        result = normalise(batches,300)
        self.assertEqual(batches,original)
        self.assertIsNone(result[0]['spread'])


class ArchiveCollectorTests(unittest.TestCase):
    def setUp(self):
        self.now = START+3
        self.store = MarketDataStore(MemoryRedis())
        self.bridge = FakeBridge(lambda:self.now)
        self.batches = []
        self.failure = None
        def sink(symbol,batches,*,observed_at):
            if self.failure:
                raise self.failure
            # The first store mutation must still be after this callback.
            before = copy.deepcopy(self.store.bars(symbol,'M5'))
            self.batches.append((copy.deepcopy(batches),before))
            normalise(batches,observed_at)
            return dict(status='VERIFIED',checked_at=observed_at)
        self.collector = MarketDataCollector(self.store,self.bridge,bar_symbols=lambda:['BTCUSDm'],
                                            clock=lambda:self.now,archive_sink=sink)

    def test_cold_start_archives_only_completed_candles_before_publication(self):
        result = self.collector.tick()
        self.assertTrue(result['bars'][0]['ok'])
        batches,before = self.batches[0]
        self.assertEqual(before,[])
        self.assertEqual(len(batches['M5']),320)
        self.assertTrue(all(r['time']+300<=self.now for r in batches['M5']))
        self.assertEqual(self.store.state('BTCUSDm')['archive_status'],'VERIFIED')
        self.assertTrue(self.store.health()['archive_enabled'])

    def test_evicted_bars_are_in_archive_batch_and_retry_after_restart_is_safe(self):
        self.collector.tick()
        for i in range(81):
            self.now += 300
            self.collector.tick()
        previous = self.store.bars('BTCUSDm','M5')
        self.now += 300
        self.collector.tick()
        batch,before = self.batches[-1]
        self.assertEqual(before,previous)
        self.assertIn(previous[0],batch['M5'])
        self.assertNotIn(previous[0],self.store.bars('BTCUSDm','M5'))
        # A new process can bootstrap against an existing populated cache.
        self.now += 300
        restarted = MarketDataCollector(self.store,self.bridge,bar_symbols=lambda:['BTCUSDm'],
            clock=lambda:self.now,archive_sink=self.collector.archive_sink)
        self.assertTrue(restarted.tick()['bars'][0]['ok'])

    def test_database_failure_keeps_cache_and_snapshot_then_recovers(self):
        self.collector.tick()
        bars = copy.deepcopy(self.store.bars('BTCUSDm','M5'))
        snapshot = copy.deepcopy(self.store.snapshot('BTCUSDm'))
        self.now += 300
        self.failure = ArchiveUnavailable('database archive not committed')
        self.collector.tick()
        self.assertEqual(self.store.bars('BTCUSDm','M5'),bars)
        self.assertEqual(self.store.snapshot('BTCUSDm'),snapshot)
        self.assertEqual(self.store.state('BTCUSDm')['health_state'],'ARCHIVE_UNAVAILABLE')
        count = len(self.bridge.calls)
        self.now += 1
        self.collector.tick()
        self.assertEqual(len(self.bridge.calls),count)  # backoff, no extra snapshot
        self.failure = None
        self.now += 5
        self.assertTrue(self.collector.tick()['bars'][0]['ok'])

    def test_conflict_is_visible_and_quote_polling_continues(self):
        self.collector.tick()
        self.now += 300
        self.failure = ArchiveConflict('immutable candle differs')
        self.collector.quote_symbols = lambda:['BTCUSDm']
        result = self.collector.tick()
        self.assertEqual(self.store.state('BTCUSDm')['health_state'],'ARCHIVE_CONFLICT')
        self.assertIn('BTCUSDm',result['quotes'])
        self.assertEqual(self.collector.bridge_breaker.state,'CLOSED')

    def test_enabled_archival_preserves_cache_contents_and_bridge_command_count(self):
        baseline_store = MarketDataStore(MemoryRedis())
        baseline_bridge = FakeBridge(lambda:self.now)
        baseline = MarketDataCollector(baseline_store,baseline_bridge,bar_symbols=lambda:['BTCUSDm'],
                                       clock=lambda:self.now)
        for _ in range(3):
            self.collector.tick()
            baseline.tick()
            for tf in ('M5','M15','H1','H4'):
                self.assertEqual(self.store.bars('BTCUSDm',tf),baseline_store.bars('BTCUSDm',tf))
            self.now += 300
        self.assertEqual(self.bridge.calls,baseline_bridge.calls)

    def test_archive_commit_precedes_a_redis_publication_failure(self):
        original = self.store.set_bars
        def fail(*args,**kw):
            raise RuntimeError('redis publication failed')
        self.store.set_bars = fail
        self.assertFalse(self.collector.tick()['bars'][0]['ok'])
        self.assertEqual(len(self.batches),1)
        self.assertEqual(self.store.bars('BTCUSDm','M5'),[])
        self.store.set_bars = original
        self.now += 5
        self.assertTrue(self.collector.tick()['bars'][0]['ok'])
        self.assertEqual(self.batches[0][0],self.batches[1][0])

    def test_large_range_recovery_archives_beyond_the_trimmed_cache(self):
        clock = {'now':1_800_000_003}
        bridge = RangeBridge(lambda:clock['now'])
        store = MarketDataStore(MemoryRedis())
        saved = []
        def sink(symbol,batches,**kw):
            saved.append(copy.deepcopy(batches))
            return {'status':'VERIFIED'}
        collector = MarketDataCollector(store,bridge,bar_symbols=lambda:['XAUUSDm'],
                                        clock=lambda:clock['now'],archive_sink=sink)
        collector.tick()
        clock['now'] += 2*86400
        self.assertTrue(collector.tick()['bars'][0]['ok'])
        self.assertGreater(len({r['time'] for r in saved[-1]['M5']}),400)
        self.assertEqual(len(store.bars('XAUUSDm','M5')),400)


@unittest.skipUnless(os.environ.get('OUTCOME_RESOLVER_TEST_DSN'),'isolated PostgreSQL unavailable')
class ContinuousArchivePostgresTests(unittest.TestCase):
    def test_restart_idempotency_and_atomic_conflict_rollback(self):
        import psycopg
        dsn = os.environ['OUTCOME_RESOLVER_TEST_DSN']
        with psycopg.connect(dsn) as conn,conn.cursor() as cur:
            cur.execute('SELECT current_database()')
            database = cur.fetchone()[0]
            self.assertIn('ci',database.lower())
        symbol = 'CONTINUOUSARCHIVETEST'
        first = {'M5':[dict(time=0,open=100,high=102,low=99,close=101)]}
        sink = PostgresCandleArchive(lambda:psycopg.connect(dsn),source_run_id='collector:test',expected_database=database)
        try:
            a = sink(symbol,first,observed_at=900)
            restarted = PostgresCandleArchive(lambda:psycopg.connect(dsn),
                source_run_id='collector:test-restarted',expected_database=database)
            b = restarted(symbol,first,observed_at=900)
            self.assertEqual((a['inserted_bars'],b['inserted_bars']),(1,0))
            wrong_target = PostgresCandleArchive(lambda:psycopg.connect(dsn),source_run_id='wrong',
                                                 expected_database='wrong-target')
            with self.assertRaises(ArchiveUnavailable):
                wrong_target(symbol,first,observed_at=900)
            # An additional new bar must roll back if another bar conflicts.
            conflict = {'M5':[dict(first['M5'][0],close=100.5),
                              dict(time=300,open=100,high=102,low=99,close=101)]}
            with self.assertRaises(ArchiveConflict):
                sink(symbol,conflict,observed_at=900)
            with psycopg.connect(dsn) as conn,conn.cursor() as cur:
                cur.execute('SELECT candle_time,close_price FROM market_data.candle_archive WHERE provider_symbol=%s ORDER BY candle_time',(symbol,))
                self.assertEqual(cur.fetchall(),[(0,Decimal('101'))])
                cur.execute('SELECT source_run_id FROM market_data.candle_archive WHERE provider_symbol=%s',(symbol,))
                self.assertEqual(cur.fetchone()[0],'collector:test')
        finally:
            with psycopg.connect(dsn) as conn,conn.cursor() as cur:
                cur.execute('DELETE FROM market_data.candle_archive WHERE provider_symbol=%s',(symbol,))
