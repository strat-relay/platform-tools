import copy
import json
import os
import time
import unittest
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.bounded_outcome_backfill import (apply, digest, fetch, main, read_page, validate_plan, validate_rows)

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT/'docs/research/bounded_backfill_20261020/plan.json'


class BoundedBackfillTests(unittest.TestCase):
    def plan(self):
        return json.loads(PLAN.read_text())

    def row(self, t=0, **kw):
        return dict(time=t,open=100,high=102,low=99,close=101,**kw)

    def test_frozen_plan_covers_only_approved_budget(self):
        p = self.plan()
        self.assertEqual(validate_plan(p,p['plan_sha256'],time.time()),
                         dict(streams=6,intervals=18,slots=4979,requests=35))
        self.assertEqual(len(p['signal_ids']),26)

    def test_changed_window_and_larger_budget_fail(self):
        p = self.plan()
        p['intervals'][0]['end'] += 900
        with self.assertRaisesRegex(ValueError,'hash'):
            validate_plan(p,p['plan_sha256'],time.time())
        p['intervals'][0]['end'] += 900*5000
        p['plan_sha256'] = digest({k:v for k,v in p.items() if k!='plan_sha256'})
        with self.assertRaises(ValueError):
            validate_plan(p,p['plan_sha256'],time.time()+86400*100)

    def test_valid_rows_and_exclusive_upper_boundary(self):
        rows = validate_rows([self.row(),self.row(900)],0,900,900)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['time'],0)

    def test_invalid_price_order_geometry_and_forming_bars_fail(self):
        examples = [[self.row(),self.row()], [self.row(1)], [self.row(1800)],
                    [dict(self.row(),high=98)], [dict(self.row(),close='NaN')],
                    [dict(self.row(),open='100.000000001')], [self.row(is_closed=False)]]
        for rows in examples:
            with self.subTest(rows=rows),self.assertRaises((ValueError,ArithmeticError)):
                validate_rows(rows,0,900,900)

    def test_empty_bridge_results_remain_empty_and_budget_is_exact(self):
        calls = []
        def read(url,item,a,b,timeout):
            calls.append((item['symbol'],item['timeframe'],a,b))
            return []
        pages = fetch(self.plan(),'unused',reader=read,sleep=lambda _:None,clock=lambda:0)
        self.assertEqual(len(calls),35)
        self.assertTrue(all(not p['rows'] for p in pages))

    def test_invalid_page_stops_without_any_later_request(self):
        calls = []
        def read(url,item,a,b,timeout):
            calls.append(a)
            return [self.row(a-900)]
        with self.assertRaises(ValueError):
            fetch(self.plan(),'unused',reader=read,sleep=lambda _:None,clock=lambda:0)
        self.assertEqual(len(calls),1)

    def test_global_time_budget_stops_before_bridge(self):
        clock = iter((0,301))
        with self.assertRaises(TimeoutError):
            fetch(self.plan(),'unused',reader=lambda *args:self.fail('bridge called'),
                  sleep=lambda _:None,clock=lambda:next(clock))

    def test_rpc_is_completed_historical_rates_only(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'result':{'content':[{'text':json.dumps({'rates':[]})}]}}).encode()
        with patch('scripts.bounded_outcome_backfill.urlopen',return_value=response) as request:
            self.assertEqual(read_page('http://example.test/mcp',
                             dict(symbol='BTCUSDm',timeframe='M15'),0,900,5),[])
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['params']['name'],'mt5_rates_range')
        self.assertTrue(payload['params']['arguments']['completed_only'])

    def test_without_apply_never_opens_a_write_connection(self):
        plan = self.plan()
        conn = MagicMock()
        cfg = MagicMock()
        with patch('sys.argv',['bounded','--expected-plan-sha',plan['plan_sha256']]), \
             patch('sys.stdin',io.StringIO(json.dumps(plan))), \
             patch('sys.stdout',io.StringIO()), \
             patch.dict(os.environ,{'MARKET_DATA_BRIDGE_URL':'http://unused'}), \
             patch('postgres.config.PostgresConfig.from_env',return_value=cfg), \
             patch('postgres.db.connect',return_value=conn) as connect, \
             patch('scripts.bounded_outcome_backfill.preflight'), \
             patch('scripts.bounded_outcome_backfill.fetch',return_value=[]), \
             patch('scripts.bounded_outcome_backfill.apply') as writer:
            main()
        self.assertEqual(connect.call_count,1)
        self.assertTrue(conn.__enter__.return_value.read_only)
        writer.assert_not_called()


@unittest.skipUnless(os.getenv('OUTCOME_RESOLVER_TEST_DSN'),'isolated PostgreSQL unavailable')
class BoundedBackfillPostgresTests(unittest.TestCase):
    def test_idempotency_partial_runs_and_conflict_rollback(self):
        import psycopg
        with psycopg.connect(os.environ['OUTCOME_RESOLVER_TEST_DSN']) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT current_database()')
                self.assertIn('ci',cur.fetchone()[0].lower(),'test database required')
            plan = dict(plan_sha256='0'*64,intervals=[dict(symbol='BOUNDEDBACKFILLTEST',timeframe='M15',start=0,end=1800)])
            pages = [dict(interval=0,rows=[dict(time=0,open='100',high='102',low='99',close='101')])]
            try:
                first = apply(conn,plan,pages)
                second = apply(conn,plan,pages)
                self.assertEqual(first,dict(inserted=1,unchanged=0))
                self.assertEqual(second,dict(inserted=0,unchanged=1))
                with conn.cursor() as cur:
                    cur.execute("SELECT status,bars_fetched,gaps_detected FROM market_data.backfill_runs WHERE run_id=%s",('bounded-'+('0'*20)+'-0',))
                    self.assertEqual(cur.fetchone(),('PARTIAL',1,1))
                changed = copy.deepcopy(pages)
                changed[0]['rows'][0]['close'] = '100.5'
                with self.assertRaisesRegex(ValueError,'OHLC conflict'):
                    apply(conn,plan,changed)
            finally:
                conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM market_data.candle_archive WHERE provider_symbol='BOUNDEDBACKFILLTEST'")
                self.assertEqual(cur.fetchone()[0],0)
