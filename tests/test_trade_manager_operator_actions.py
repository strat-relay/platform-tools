from datetime import datetime, timedelta, timezone
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest
from trade_management.operator_actions import ActionConflict, breakeven_price, request_action, validate_request
from platform_api.trade_manager_actions import save_action
from test_management_execution_safety import RecordingManager

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
REQUEST = dict(requestId='operator-1', managedTradeId='MT1', action='EXIT',
               expectedObservationSeq=4, reason='Operator reviewed the signal', allowLiveExecution=False)
TRADE = dict(state='OPEN', direction='LONG', reference_entry_price=100, initial_stop=90,
             initial_target=120, entry_signal_published=True)


def setup_conn(replies=None):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.side_effect = replies or [('V1', 4, 'SIG1'), None, None]
    return conn, cur


def act(conn, request=None, **changes):
    with patch('trade_management.operator_actions.current_mode', return_value='LIVE'), \
         patch('trade_management.operator_actions._load_trade', return_value={**TRADE, **changes}), \
         patch('trade_management.operator_actions.load_signal_state', return_value={'strategy_id':'TEST'}):
        return request_action(conn, request or REQUEST, now=NOW)


def test_exit_records_auditable_decision_and_both_outbox_events_in_one_transaction():
    conn, cur = setup_conn()
    result = act(conn)
    assert result['broker_confirmed'] is False
    assert result['publication_outcome'] == 'PUBLISHED'
    conn.commit.assert_called_once()
    commands = cur.execute.call_args_list
    events = [args.args[1] for args in commands if 'INSERT INTO platform.outbox_events' in args.args[0]]
    assert len(events) == 2
    payload = json.loads(events[0][4])
    assert payload['parameters'] == {'operator_action':True, 'execution_scope':'THEORETICAL_ONLY', 'exit_reason':'OPERATOR_EXIT'}
    decision = next(args.args[1] for args in commands if 'INSERT INTO trade_management.trade_manager_decision' in args.args[0])
    assert json.loads(decision[8])['reason'] == REQUEST['reason']


def test_same_request_replays_without_another_decision_or_outbox_event():
    digest = hashlib.sha256(json.dumps(validate_request(REQUEST), sort_keys=True).encode()).hexdigest()
    conn, cur = setup_conn([('V1', 20, 'SIG1'), ({'request_hash':digest},)])
    assert act(conn)['replayed'] is True
    assert not any('INSERT' in call.args[0] for call in cur.execute.call_args_list)


def test_request_id_reuse_with_different_payload_rolls_back():
    conn, _ = setup_conn([('V1', 4, 'SIG1'), ({'request_hash':'different'},)])
    with pytest.raises(ActionConflict, match='different request'):
        act(conn)
    conn.rollback.assert_called_once()


def test_management_change_since_read_requires_review():
    conn, _ = setup_conn([('V1', 5, 'SIG1'), None, ('new-decision',)])
    with pytest.raises(ActionConflict, match='Management changed'):
        act(conn)
    conn.commit.assert_not_called()


def test_new_quote_or_hold_does_not_needlessly_block_action():
    conn, _ = setup_conn([('V1', 5, 'SIG1'), None, None, None])
    assert act(conn)['status'] == 'RECORDED'


def test_pending_entry_never_creates_an_action():
    conn, cur = setup_conn()
    with patch('trade_management.operator_actions.monitoring_state', return_value='PENDING_ENTRY'):
        with pytest.raises(ActionConflict, match='activated'):
            act(conn)
    assert not any('INSERT' in call.args[0] for call in cur.execute.call_args_list)


def test_unpublished_entry_is_withheld_from_subscribers_and_execution():
    conn, cur = setup_conn()
    assert act(conn, entry_signal_published=False)['publication_outcome'] == 'WITHHELD'
    assert not any('INSERT INTO platform.outbox_events' in call.args[0] for call in cur.execute.call_args_list)


def test_outbox_failure_rolls_back_decision_and_sequence_together():
    conn, cur = setup_conn()
    def execute(sql, params=None):
        if 'INSERT INTO platform.outbox_events' in sql:
            raise RuntimeError('database unavailable')
    cur.execute.side_effect = execute
    with pytest.raises(RuntimeError):
        act(conn)
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.parametrize('mode', ['OFF', 'SHADOW'])
def test_live_scope_requires_live_mode(mode):
    conn, _ = setup_conn()
    with patch('trade_management.operator_actions.current_mode', return_value=mode), \
         pytest.raises(ActionConflict, match='mode'):
        request_action(conn, {**REQUEST,'allowLiveExecution':True}, now=NOW)


@pytest.mark.parametrize('direction,bid,ask,stop', [('LONG',101,102,90), ('SHORT',98,99,110)])
def test_breakeven_uses_theoretical_entry_only_with_safe_close_side(direction,bid,ask,stop):
    assert breakeven_price({**TRADE,'direction':direction}, {'bid':bid,'ask':ask,'source_timestamp':NOW}, stop, NOW) == 100


@pytest.mark.parametrize('quote,stop', [
    ({'bid':101,'ask':102,'source_timestamp':NOW-timedelta(seconds=121)},90),
    ({'bid':99,'ask':100,'source_timestamp':NOW},90),
    ({'bid':101,'ask':102,'source_timestamp':NOW},101),
    ({'bid':float('nan'),'ask':102,'source_timestamp':NOW},90),
])
def test_unsafe_breakeven_rejected(quote,stop):
    with pytest.raises(ActionConflict):
        breakeven_price(TRADE,quote,stop,NOW)


def test_invalid_http_request_never_opens_a_connection():
    connect = MagicMock()
    assert save_action(b'{"action":"EXIT"}',connect)[0] == 400
    connect.assert_not_called()


def test_theoretical_scope_cannot_reach_broker_even_in_live_mode():
    manager = RecordingManager()
    result = manager.process_decision({'action':'EXIT','decision_id':'D','managed_trade_id':'MT',
        'parameters':{'execution_scope':'THEORETICAL_ONLY'}, 'decision_time':NOW.isoformat()},now_utc=NOW)
    assert result.reason == 'THEORETICAL_ONLY'
    assert not manager.submissions


def test_operator_broker_breakeven_uses_actual_fill_price():
    manager = RecordingManager()
    manager.position['price_open'] = 103
    result = manager.process_decision({'action':'MOVE_TO_BREAKEVEN','decision_id':'D','managed_trade_id':'MT',
        'parameters':{'execution_scope':'LINKED_LIVE','operator_action':True,'new_stop':100},
        'decision_time':NOW.isoformat()},now_utc=NOW)
    assert result.status == 'APPLIED'
    assert manager.rows[0]['requested_stop'] == 103


def test_operator_exit_stops_price_actions_but_preserves_theoretical_deadline():
    from trade_management.fakes import FakeConnection
    from trade_management.decision_engine import record_decision
    from test_trade_management_decision_engine import make_breakeven_trail_trade, observe, NOW as TICK
    conn = FakeConnection()
    trade_id = make_breakeven_trail_trade(conn)
    first = observe(conn, trade_id, bid=101, ask=101.1)
    result = record_decision(conn, observation_id=first, event_id='first', now_utc=TICK)
    decision = conn.tables['trade_management.trade_manager_decision'][result.decision_id]
    decision['action'] = 'EXIT'
    decision['parameters'] = json.dumps({'exit_reason':'OPERATOR_EXIT'})
    second = observe(conn, trade_id, bid=120, ask=120.1, ts=(TICK+timedelta(seconds=1)).isoformat())
    result = record_decision(conn, observation_id=second, event_id='second', now_utc=TICK+timedelta(seconds=1))
    assert result.status == 'OPERATOR_EXIT_ALREADY_RECORDED'
    conn.tables['trade_management.managed_trade'][trade_id]['time_exit_at'] = TICK
    result = record_decision(conn, observation_id=second, event_id='deadline', now_utc=TICK+timedelta(seconds=1))
    assert result.action == 'EXIT'
    latest = conn.tables['trade_management.trade_manager_decision'][result.decision_id]
    assert json.loads(latest['parameters'])['exit_reason'] == 'TIME_EXIT'
