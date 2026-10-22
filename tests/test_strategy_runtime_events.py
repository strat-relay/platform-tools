"""Scheduling/durability regressions; no broker or production connections."""
import asyncio
import ast
from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from infrastructure.messaging.runtime_events import Wakeup, CacheNotifier, notify_ready, notify_cache_ready, CACHE, READY, STREAM
from infrastructure.messaging.runtime_event_service import relay_batch, check_stream

ENV = {'STRATEGY_EVENT_MODE': 'JETSTREAM', 'STRATEGY_EVENT_RUNTIME_ID': 'test-observer',
       'STRATEGY_EVENT_DATABASE': 'trading_platform_ci', 'MARKET_DATA_SOURCE': 'REDIS',
       'NATS_URL': 'nats://127.0.0.1:4222', 'P2_NATS_USER': 'ci-only', 'P2_NATS_PASSWORD': 'ci-only'}


class WakeupTests(unittest.TestCase):
    def event_wakeup(self):
        wake = Wakeup()
        wake.healthy = True
        wake.start = lambda: wake
        self.addCleanup(wake.close)
        return wake

    def test_legacy_mode_has_no_network_dependency(self):
        with patch.dict(os.environ, {}, clear=True), patch('time.sleep') as sleep:
            wake = Wakeup().start()
            self.assertEqual(wake.wait(7), 'POLL')
            sleep.assert_called_once_with(7)
            self.assertIsNone(notify_ready({'entry': 'a'}))
            self.assertIsNone(notify_cache_ready('BTCUSDm', {'quote': 1}))
            self.assertIsNone(wake.thread)

    def test_event_mode_requires_explicit_identity_credentials_and_canonical_cache(self):
        for missing in ('STRATEGY_EVENT_RUNTIME_ID', 'NATS_URL', 'P2_NATS_USER', 'P2_NATS_PASSWORD', 'MARKET_DATA_SOURCE'):
            with self.subTest(missing=missing), patch.dict(os.environ, ENV, clear=True):
                os.environ.pop(missing)
                with self.assertRaises(ValueError):
                    Wakeup()

    def test_bad_modes_and_nonfinite_reconciliation_are_rejected(self):
        for value in ('nan', 'inf', '0', '61'):
            with self.subTest(value=value), patch.dict(os.environ, {**ENV, 'STRATEGY_EVENT_RECONCILE_SECONDS': value}, clear=True):
                with self.assertRaises(ValueError):
                    Wakeup()
        with patch.dict(os.environ, {'STRATEGY_EVENT_MODE': 'typo'}, clear=True):
            with self.assertRaises(ValueError):
                Wakeup()

    @patch.dict(os.environ, ENV, clear=True)
    def test_event_before_wait_and_events_during_evaluation_are_not_lost(self):
        wake = self.event_wakeup()
        wake.receive('a'*64, 100)
        self.assertEqual(wake.wait(60), 'EVENT')
        # Evaluation happens here; a second message arrives before the next wait.
        wake.receive('b'*64, 101)
        self.assertEqual(wake.wait(60), 'EVENT')
        self.assertEqual(wake.consumed, 2)

    @patch.dict(os.environ, ENV, clear=True)
    def test_blocked_wait_wakes_promptly_without_configured_poll_interval(self):
        wake = self.event_wakeup()
        result = []
        waiting = threading.Event()
        def cycle():
            waiting.set()
            result.append(wake.wait(60))
        thread = threading.Thread(target=cycle)
        thread.start(); waiting.wait(1)
        wake.receive('a'*64, time.time())
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ['EVENT'])

    @patch.dict(os.environ, ENV, clear=True)
    def test_burst_notifications_coalesce_without_evaluator_threads(self):
        wake = self.event_wakeup()
        for _ in range(10000):
            wake.receive('a'*64, 100)
        self.assertEqual(wake.wait(60), 'EVENT')
        self.assertEqual(wake.consumed, 10000)
        self.assertIsNone(wake.thread)

    @patch.dict(os.environ, ENV, clear=True)
    def test_disconnected_transport_does_not_silently_poll(self):
        wake = self.event_wakeup()
        wake.healthy = False
        with self.assertRaisesRegex(RuntimeError, 'no silent polling fallback'):
            wake.wait(0)

    @patch.dict(os.environ, ENV, clear=True)
    def test_stop_interrupts_reconciliation_wait(self):
        wake = self.event_wakeup()
        self.assertEqual(wake.wait(60, stopping=lambda: True), 'RECONCILIATION')

    @patch.dict(os.environ, ENV, clear=True)
    def test_revision_retries_have_same_id_and_join_callers_transaction(self):
        conn = MagicMock()
        first = notify_ready({'entry': 'a'}, conn=conn)
        second = notify_ready({'entry': 'a'}, conn=conn)
        third = notify_ready({'entry': 'b'}, conn=conn)
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        conn.commit.assert_not_called()
        conn.rollback.assert_not_called()
        query, params = conn.cursor.return_value.__enter__.return_value.execute.call_args.args
        self.assertIn('ON CONFLICT(event_id) DO NOTHING', query)
        self.assertEqual(json.loads(params[2])['runtime_id'], 'test-observer')

    def test_collector_notifies_only_after_cache_is_visible(self):
        import fakeredis
        from test_market_data_cache import START, FakeBridge
        from market_data_cache.store import MarketDataStore
        from market_data_cache.collector import MarketDataCollector
        store = MarketDataStore(fakeredis.FakeRedis())
        clock = lambda: float(START+3)
        collector = MarketDataCollector(store, FakeBridge(clock), clock=clock,
                                        bar_symbols=lambda: ['BTCUSDm'], quote_symbols=lambda: ['BTCUSDm'])
        hints = []
        def assert_visible(provider, revision):
            if revision['kind'] == 'SNAPSHOT':
                self.assertTrue(store.state(provider)['healthy'])
                self.assertTrue(store.snapshot(provider))
                self.assertTrue(store.bars(provider, 'M5'))
            else:
                self.assertEqual(store.quote(provider)['quote'], revision['quote'])
            hints.append(revision['kind'])
        with patch('infrastructure.messaging.runtime_events.notify_cache_ready', side_effect=assert_visible):
            collector.tick()
        self.assertEqual(hints, ['SNAPSHOT', 'QUOTE'])

    def test_slow_hint_database_cannot_block_collector_and_pending_memory_is_bounded(self):
        writing = threading.Event()
        release = threading.Event()
        def slow_write(*args, **kwargs):
            writing.set()
            release.wait(2)
        with patch('infrastructure.messaging.runtime_events.notify_ready', side_effect=slow_write):
            notifier = CacheNotifier()
            try:
                notifier.submit('first', {'revision': 1})
                self.assertTrue(writing.wait(1))
                # The database worker is blocked; collector submissions still finish.
                for i in range(256):
                    notifier.submit(str(i), {'revision': 1})
                notifier.submit('last', {'revision': 1})
                notifier.submit('0', {'revision': 2})
                with notifier.condition:
                    self.assertEqual(len(notifier.pending), 256)
                    self.assertEqual(notifier.pending['0'], {'revision': 2})
                    self.assertNotIn('last', notifier.pending)
            finally:
                release.set()
                notifier.close()

    def test_every_operational_runner_uses_shared_scheduler(self):
        # A future independent runner must be enrolled here or use an existing
        # shared runner. Library compositions all inherit the generic runner.
        root = Path(__file__).resolve().parents[1]
        for module in ('context_structure_retrace_forward.py', 'context_structure_retrace_v2_forward.py',
                       'liquidity_live_service.py', 'research/price_action_forward_runtime.py',
                       'research/strategy_library_forward.py', 'signal_orchestrator.py'):
            with self.subTest(module=module):
                source = (root/module).read_text()
                self.assertIn('infrastructure.messaging.runtime_events import Wakeup', source)
                self.assertIn('wakeup.wait(', source)

    def test_context_transport_preserves_frozen_rules_and_all_nonloop_helpers(self):
        import context_structure_retrace_forward as runner
        self.assertEqual(runner.decision_code_hash(),
                         '6dda2523e15edbc0e2d123878367f21ffaec70219272aa409193c2fc45b7c9bc')
        self.assertEqual(runner.config_hash(),
                         '1f1da2a63d69ac79e4aca21d0de33c860e76f4c33d9bd321cb50b20353114e1e')
        tree = ast.parse(Path(runner.__file__).read_text())
        helpers = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name not in {'run', 'poll'}]
        digest = hashlib.sha256('\n'.join(ast.dump(node, include_attributes=False) for node in helpers).encode()).hexdigest()
        # Captured from main 344dc99 before the transport-only edit.
        self.assertEqual(digest, 'af4fd7c34b729daafcb7d1fa2b1248afd2af743c34ec8b754637c3209c03fd6f')


class HintAcknowledgementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.wake = Wakeup()

    def message(self, event='a', ack_error=None):
        return SimpleNamespace(data=json.dumps({'event_id': event*64}).encode(),
                               ack_sync=AsyncMock(side_effect=ack_error))

    async def test_idle_timeouts_and_lost_ack_response_do_not_destroy_subscription(self):
        from nats.errors import TimeoutError as NatsTimeoutError
        first=self.message(ack_error=NatsTimeoutError())
        second=self.message('b')
        steps=iter([NatsTimeoutError(), asyncio.TimeoutError(), [first], [second]])
        async def fetch(*args, **kwargs):
            item=next(steps)
            if isinstance(item,Exception):raise item
            if item==[second]:self.wake.stopped.set()
            return item
        with patch('infrastructure.messaging.runtime_events.report') as log:
            await self.wake._consume(SimpleNamespace(fetch=fetch))
        self.assertEqual(self.wake.generation,2)
        self.assertEqual(self.wake.last_event_id,'b'*64)
        self.assertEqual([c.args[0] for c in log.call_args_list],
                         ['strategy_event_ack_timeout','strategy_event_ack_recovered'])
        first.ack_sync.assert_awaited_once_with(timeout=2)

    async def test_repeated_ack_timeouts_fail_closed(self):
        msg=self.message(ack_error=asyncio.TimeoutError())
        sub=SimpleNamespace(fetch=AsyncMock(return_value=[msg]))
        with patch('infrastructure.messaging.runtime_events.report'),self.assertRaisesRegex(RuntimeError,'repeated'):
            await self.wake._consume(sub)
        self.assertEqual(sub.fetch.await_count,3)
        self.assertEqual(self.wake.generation,3)

    async def test_malformed_hint_is_never_acknowledged(self):
        for payload in ([], {'event_id':123}, {'event_id':'bad'}, {'event_id':'a'*64,'extra':True}):
            msg=SimpleNamespace(data=json.dumps(payload).encode(),ack_sync=AsyncMock())
            with self.subTest(payload=payload),self.assertRaises(ValueError):
                await self.wake._consume(SimpleNamespace(fetch=AsyncMock(return_value=[msg])))
            msg.ack_sync.assert_not_awaited()
        self.assertEqual(self.wake.generation,0)

    async def test_non_timeout_transport_failure_remains_fatal(self):
        msg=self.message(ack_error=ConnectionError('private connection'))
        with self.assertRaises(ConnectionError):
            await self.wake._consume(SimpleNamespace(fetch=AsyncMock(return_value=[msg])))


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_outbox_commit_during_scan_is_not_lost(self):
        from infrastructure.messaging.outbox_relay import OutboxRelay
        stop, wake = asyncio.Event(), asyncio.Event()
        relay = OutboxRelay(MagicMock(), MagicMock())
        async def batch(**kwargs):
            if relay.publish_batch.await_count == 1:
                wake.set()  # transaction commits while the scan is in flight
                return {'published': 0, 'failed': 0}
            stop.set()
            return {'published': 1, 'failed': 0}
        relay.publish_batch = AsyncMock(side_effect=batch)
        await asyncio.wait_for(relay.run_forever(stop=stop, idle_seconds=30, wake=wake), timeout=0.5)
        self.assertEqual(relay.publish_batch.await_count, 2)

    async def test_publish_acceptance_precedes_bookkeeping_and_uses_stable_message_id(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [('a'*64, 'observer', {'provider_symbol': None})]
        order = []
        cur.execute.side_effect = lambda sql, *args: order.append('mark' if sql.startswith('UPDATE') else 'select')
        js = SimpleNamespace(publish=AsyncMock(side_effect=lambda *a, **k: order.append('publish')))
        self.assertEqual(await relay_batch(conn, js), 1)
        self.assertEqual(order, ['select', 'publish', 'mark'])
        js.publish.assert_awaited_once_with(READY+'.observer', json.dumps({'event_id': 'a'*64}).encode(),
                                           headers={'Nats-Msg-Id': 'a'*64})

    async def test_failed_publish_leaves_transaction_uncommitted(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [('b'*64, 'collector', {'provider_symbol': 'BTCUSDm'})]
        js = SimpleNamespace(publish=AsyncMock(side_effect=ConnectionError('ci')))
        with self.assertRaises(ConnectionError):
            await relay_batch(conn, js)
        self.assertFalse(any(call.args[0].startswith('UPDATE') for call in cur.execute.call_args_list))
        self.assertEqual(js.publish.call_args.args[0], CACHE+'.'+b'BTCUSDm'.hex())

    async def test_stream_conflicts_are_rejected_without_reconfiguration(self):
        cfg = SimpleNamespace(subjects=[READY+'.*', CACHE+'.*'], storage='file', max_age=86400,
                              max_bytes=64*1024*1024, discard='new', max_msg_size=1024)
        js = SimpleNamespace(stream_info=AsyncMock(return_value=SimpleNamespace(config=cfg)))
        await check_stream(js)
        for field, bad in (('subjects', ['>']), ('storage', 'memory'), ('discard', 'old'),
                           ('max_bytes', -1), ('max_age', -1), ('max_msg_size', -1)):
            old = getattr(cfg, field)
            setattr(cfg, field, bad)
            with self.subTest(field=field), self.assertRaises(ValueError):
                await check_stream(js)
            setattr(cfg, field, old)


if __name__ == '__main__':
    unittest.main()
