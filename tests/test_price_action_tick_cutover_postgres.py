"""Rollback-only cutover SQL checks against the isolated CI authority database."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from research.price_action_forward_observer import Observer
from research.price_action_next_open import mode_binding
from research.price_action_metadata import POLICY
from research.price_action_tick_cutover import MODE
from test_price_action_tick_cutover import bundle

DSN = os.environ.get('OUTCOME_RESOLVER_TEST_DSN')


def within_transaction(sql):
    return sql.removeprefix('BEGIN;').removesuffix('COMMIT;\n')


@unittest.skipUnless(DSN, 'isolated PostgreSQL test DSN unavailable')
class PriceActionTickCutoverPostgresTests(unittest.TestCase):
    def test_both_registrations_and_activation_preserve_frozen_evidence(self):
        import psycopg
        conn = psycopg.connect(DSN)
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT current_database()')
                name = cur.fetchone()[0].lower()
                self.assertTrue('test' in name or name.endswith('_ci'), 'isolated test DB required')
                result = bundle()
                originals = [i['original_instance_id'] for i in result['run.json']['instances'].values()]
                cur.execute('SELECT instance_id,state FROM research.price_action_observer_state WHERE instance_id=ANY(%s) ORDER BY instance_id', (originals,))
                before = cur.fetchall()
                cur.execute(within_transaction(result['register.sql']))
                cur.execute(within_transaction(result['register.sql']))
                manifests = result['workload.json']['items'][0]['data']
                # Synthetic durable bootstrap fixtures exercise activation gates, not live evidence.
                for key, info in result['run.json']['instances'].items():
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / 'manifest.json'; path.write_text(manifests[key + '.json'])
                        observer = Observer(Path(directory) / 'state', path, strategy_filter=key)
                        state = observer.state
                        state.update(status='RUNNING', entry_timing_mode=MODE,
                            entry_timing_binding=mode_binding(path, info['instance_id'], key),
                            instrument_tick_sizes={'XAUUSD': .001, 'BTCUSD': .01, 'USDJPY': .001},
                            runtime={'successful_instrument_count': 3})
                        cur.execute('INSERT INTO research.price_action_observer_state (instance_id,strategy_key,state,last_observed_at) VALUES (%s,%s,%s::jsonb,clock_timestamp())',
                                    (info['instance_id'], key, json.dumps(state)))
                cur.execute(within_transaction(result['activate.sql']))
                cur.execute(within_transaction(result['activate.sql']))
                for info in result['run.json']['instances'].values():
                    cur.execute('SELECT online,execution_eligible,execution_mode FROM strategy_mgmt.strategy_instance_v2 WHERE id=%s', (info['instance_id'],))
                    self.assertEqual(cur.fetchone(), (True, False, 'OFF'))
                cur.execute('SELECT instance_id,state FROM research.price_action_observer_state WHERE instance_id=ANY(%s) ORDER BY instance_id', (originals,))
                self.assertEqual(cur.fetchall(), before)
        finally:
            conn.rollback(); conn.close()
