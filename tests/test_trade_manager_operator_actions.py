from datetime import datetime, timedelta, timezone
import hashlib
import json
from unittest.mock import MagicMock, patch
import unittest
from trade_management.operator_actions import ActionConflict, breakeven_price, request_action, validate_request
from platform_api.trade_manager_actions import save_action
from test_management_execution_safety import RecordingManager
NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
REQUEST = dict(requestId='operator-1', managedTradeId='MT1', action='EXIT', expectedObservationSeq=4, reason='Operator reviewed the signal', allowLiveExecution=False)
TRADE = dict(state='OPEN', direction='LONG', reference_entry_price=100, initial_stop=90, initial_target=120, entry_signal_published=True)

def setup_conn(replies=None):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = replies or [('V1', 4, 'SIG1'), None, None]
    return (conn, cur)

def act(conn, request=None, **changes):
    with patch('trade_management.operator_actions.current_mode', return_value='LIVE'), patch('trade_management.operator_actions._load_trade', return_value={**TRADE, **changes}), patch('trade_management.operator_actions.load_signal_state', return_value={'strategy_id': 'TEST'}):
        return request_action(conn, request or REQUEST, now=NOW)

class OperatorActionTests(unittest.TestCase):

    def test_exit_records_auditable_decision_and_both_outbox_events_in_one_transaction(self):
        conn, cur = setup_conn()
        result = act(conn)
        assert result['broker_confirmed'] is False
        assert result['publication_outcome'] == 'PUBLISHED'
        conn.commit.assert_called_once()
        commands = cur.execute.call_args_list
        events = [args.args[1] for args in commands if 'INSERT INTO platform.outbox_events' in args.args[0]]
        assert len(events) == 2
        payload = json.loads(events[0][4])
        assert payload['parameters'] == {'operator_action': True, 'execution_scope': 'THEORETICAL_ONLY', 'exit_reason': 'OPERATOR_EXIT'}
        decision = next((args.args[1] for args in commands if 'INSERT INTO trade_management.trade_manager_decision' in args.args[0]))
        assert json.loads(decision[8])['reason'] == REQUEST['reason']

    def test_same_request_replays_without_another_decision_or_outbox_event(self):
        digest = hashlib.sha256(json.dumps(validate_request(REQUEST), sort_keys=True).encode()).hexdigest()
        conn, cur = setup_conn([('V1', 20, 'SIG1'), ({'request_hash': digest},)])
        assert act(conn)['replayed'] is True
        assert not any(('INSERT' in call.args[0] for call in cur.execute.call_args_list))

    def test_request_id_reuse_with_different_payload_rolls_back(self):
        conn, _ = setup_conn([('V1', 4, 'SIG1'), ({'request_hash': 'different'},)])
        with self.assertRaisesRegex(ActionConflict, 'different request'):
            act(conn)
        conn.rollback.assert_called_once()

    def test_management_change_since_read_requires_review(self):
        conn, _ = setup_conn([('V1', 5, 'SIG1'), None, ('new-decision',)])
        with self.assertRaisesRegex(ActionConflict, 'Management changed'):
            act(conn)
        conn.commit.assert_not_called()

    def test_new_quote_or_hold_does_not_needlessly_block_action(self):
        conn, _ = setup_conn([('V1', 5, 'SIG1'), None, None, None])
        assert act(conn)['status'] == 'RECORDED'

    def test_pending_entry_never_creates_an_action(self):
        conn, cur = setup_conn()
        with patch('trade_management.operator_actions.monitoring_state', return_value='PENDING_ENTRY'):
            with self.assertRaisesRegex(ActionConflict, 'activated'):
                act(conn)
        assert not any(('INSERT' in call.args[0] for call in cur.execute.call_args_list))

    def test_unpublished_entry_is_withheld_from_subscribers_and_execution(self):
        conn, cur = setup_conn()
        assert act(conn, entry_signal_published=False)['publication_outcome'] == 'WITHHELD'
        assert not any(('INSERT INTO platform.outbox_events' in call.args[0] for call in cur.execute.call_args_list))

    def test_outbox_failure_rolls_back_decision_and_sequence_together(self):
        conn, cur = setup_conn()

        def execute(sql, params=None):
            if 'INSERT INTO platform.outbox_events' in sql:
                raise RuntimeError('database unavailable')
        cur.execute.side_effect = execute
        with self.assertRaises(RuntimeError):
            act(conn)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_live_scope_requires_live_mode(self):
        for mode in ['OFF', 'SHADOW']:
            conn, _ = setup_conn()
            with patch('trade_management.operator_actions.current_mode', return_value=mode), self.assertRaisesRegex(ActionConflict, 'mode'):
                request_action(conn, {**REQUEST, 'allowLiveExecution': True}, now=NOW)

    def test_breakeven_uses_theoretical_entry_only_with_safe_close_side(self):
        for direction, bid, ask, stop in [('LONG', 101, 102, 90), ('SHORT', 98, 99, 110)]:
            assert breakeven_price({**TRADE, 'direction': direction}, {'bid': bid, 'ask': ask, 'source_timestamp': NOW}, stop, NOW) == 100

    def test_unsafe_breakeven_rejected(self):
        for quote, stop in [({'bid': 101, 'ask': 102, 'source_timestamp': NOW - timedelta(seconds=121)}, 90), ({'bid': 99, 'ask': 100, 'source_timestamp': NOW}, 90), ({'bid': 101, 'ask': 102, 'source_timestamp': NOW}, 101), ({'bid': float('nan'), 'ask': 102, 'source_timestamp': NOW}, 90)]:
            with self.assertRaises(ActionConflict):
                breakeven_price(TRADE, quote, stop, NOW)

    def test_invalid_http_request_never_opens_a_connection(self):
        connect = MagicMock()
        assert save_action(b'{"action":"EXIT"}', connect)[0] == 400
        connect.assert_not_called()

    def test_theoretical_scope_cannot_reach_broker_even_in_live_mode(self):
        manager = RecordingManager()
        result = manager.process_decision({'action': 'EXIT', 'decision_id': 'D', 'managed_trade_id': 'MT', 'parameters': {'execution_scope': 'THEORETICAL_ONLY'}, 'decision_time': NOW.isoformat()}, now_utc=NOW)
        assert result.reason == 'THEORETICAL_ONLY'
        assert not manager.submissions

    def test_operator_broker_breakeven_uses_actual_fill_price(self):
        manager = RecordingManager()
        manager.position['price_open'] = 103
        result = manager.process_decision({'action': 'MOVE_TO_BREAKEVEN', 'decision_id': 'D', 'managed_trade_id': 'MT', 'parameters': {'execution_scope': 'LINKED_LIVE', 'operator_action': True, 'new_stop': 100}, 'decision_time': NOW.isoformat()}, now_utc=NOW)
        assert result.status == 'APPLIED'
        assert manager.rows[0]['requested_stop'] == 103

    def test_operator_exit_stops_price_actions_but_preserves_theoretical_deadline(self):
        from trade_management.fakes import FakeConnection
        from trade_management.decision_engine import record_decision
        from test_trade_management_decision_engine import make_breakeven_trail_trade, observe, NOW as TICK
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)
        first = observe(conn, trade_id, bid=101, ask=101.1)
        result = record_decision(conn, observation_id=first, event_id='first', now_utc=TICK)
        decision = conn.tables['trade_management.trade_manager_decision'][result.decision_id]
        decision['action'] = 'EXIT'
        decision['parameters'] = json.dumps({'exit_reason': 'OPERATOR_EXIT'})
        second = observe(conn, trade_id, bid=120, ask=120.1, ts=(TICK + timedelta(seconds=1)).isoformat())
        result = record_decision(conn, observation_id=second, event_id='second', now_utc=TICK + timedelta(seconds=1))
        assert result.status == 'OPERATOR_EXIT_ALREADY_RECORDED'
        conn.tables['trade_management.managed_trade'][trade_id]['time_exit_at'] = TICK
        result = record_decision(conn, observation_id=second, event_id='deadline', now_utc=TICK + timedelta(seconds=1))
        assert result.action == 'EXIT'
        latest = conn.tables['trade_management.trade_manager_decision'][result.decision_id]
        assert json.loads(latest['parameters'])['exit_reason'] == 'TIME_EXIT'


import test_trade_manager_live_real_postgres as live_fixtures


def operator_database_available():
    import os
    from postgres.config import PostgresConfig
    from postgres.db import connect
    target = os.environ.get('TRADING_POSTGRES_DSN') or os.environ.get('OUTCOME_RESOLVER_TEST_DSN')
    if not target:
        return False
    try:
        with connect(PostgresConfig(dsn=target)):
            return True
    except Exception:
        return False


@unittest.skipUnless(operator_database_available(), 'PostgreSQL is not available; set TRADING_POSTGRES_DSN')
class OperatorActionPostgresTests(unittest.TestCase):
    _signal = live_fixtures.LiveTradeManagerRealPostgresTests._signal
    _managed_trade = live_fixtures.LiveTradeManagerRealPostgresTests._managed_trade

    def setUp(self):
        import os
        target = os.environ.get('TRADING_POSTGRES_DSN') or os.environ['OUTCOME_RESOLVER_TEST_DSN']
        environment = patch.dict(os.environ, {'TRADING_POSTGRES_DSN': target})
        environment.start()
        self.addCleanup(environment.stop)
        from test_integration_tm_membership_runner import FreshDatabase
        from postgres.db import apply_migrations
        self.db = FreshDatabase()
        self.addCleanup(self.db.drop)
        self.conn = self.db.connect()
        self.addCleanup(self.conn.close)
        apply_migrations(self.conn)
        self.now = datetime.now(timezone.utc)
        self.signal_id = self._signal()
        self.trade_id = self._managed_trade(self.signal_id)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE trade_management.trade_manager_mode SET mode='SHADOW' WHERE mode_id='current'")
            cur.execute("""INSERT INTO platform.outbox_events
                (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,
                 occurred_at,correlation_id,causation_id,publish_status)
                VALUES (%s,'signal.entry.created.v1','signal',%s,1,'event-envelope.v1','{}',%s,%s,%s,'PUBLISHED')""",
                ('entry-'+self.signal_id,self.signal_id,self.now,self.signal_id,self.signal_id))
        self.conn.commit()
        self.request = {**REQUEST,'managedTradeId':self.trade_id,'expectedObservationSeq':0}

    def test_quote_free_exit_and_retry_are_atomic_and_deduplicated(self):
        first = request_action(self.conn,self.request,now=self.now)
        self.assertFalse(first['broker_confirmed'])
        self.assertEqual(first['publication_outcome'],'PUBLISHED')
        replay = request_action(self.conn,self.request,now=self.now+timedelta(seconds=1))
        self.assertTrue(replay['replayed'])
        self.assertEqual(first['decision_id'],replay['decision_id'])
        with self.conn.cursor() as cur:
            cur.execute('SELECT count(*),count(market_snapshot_id) FROM trade_management.trade_observation WHERE managed_trade_id=%s',(self.trade_id,))
            self.assertEqual(cur.fetchone(),(1,0))
            cur.execute("SELECT count(*) FROM platform.outbox_events WHERE aggregate_id=%s AND event_type IN ('trade.decision.made.v1','signal.management.published.v1')",(self.trade_id,))
            self.assertEqual(cur.fetchone()[0],2)

    def test_breakeven_without_quote_leaves_no_partial_records(self):
        with self.assertRaises(ActionConflict):
            request_action(self.conn,{**self.request,'action':'MOVE_TO_BREAKEVEN'},now=self.now)
        with self.conn.cursor() as cur:
            cur.execute('SELECT last_observation_seq FROM trade_management.managed_trade WHERE managed_trade_id=%s',(self.trade_id,))
            self.assertEqual(cur.fetchone()[0],0)
            cur.execute('SELECT count(*) FROM trade_management.trade_manager_decision WHERE managed_trade_id=%s',(self.trade_id,))
            self.assertEqual(cur.fetchone()[0],0)
