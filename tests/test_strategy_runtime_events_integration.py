"""Opt-in real JetStream and isolated PostgreSQL transport tests."""
import asyncio
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from infrastructure.messaging.runtime_events import CACHE, READY, STREAM, Wakeup, notify_ready
from infrastructure.messaging.runtime_event_service import provision


@unittest.skipUnless(os.environ.get('NATS_SERVER_TEST_BINARY'), 'isolated NATS server binary unavailable')
class RealWakeupTests(unittest.TestCase):
    def test_market_and_ready_notifications_wake_same_authority_and_survive_restart(self):
        import nats
        with tempfile.TemporaryDirectory() as directory:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            server = subprocess.Popen([os.environ['NATS_SERVER_TEST_BINARY'], '-js', '-a', '127.0.0.1',
                '-p', str(port), '-sd', directory, '--user', 'ci-only', '--pass', 'ci-only'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(lambda: (server.terminate(), server.wait(timeout=5)))
            deadline = time.monotonic()+5
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                        break
                except OSError:
                    if server.poll() is not None or time.monotonic() >= deadline:
                        self.fail('isolated NATS failed to start')
                    time.sleep(0.01)
            url = 'nats://127.0.0.1:'+str(port)

            async def provision_stream():
                nc = await nats.connect(url, user='ci-only', password='ci-only')
                try:
                    await provision(nc.jetstream())
                finally:
                    await nc.close()
            asyncio.run(provision_stream())

            async def publish(subject, event_id):
                nc = await nats.connect(url, user='ci-only', password='ci-only')
                try:
                    await nc.jetstream().publish(subject, json.dumps({'event_id': event_id}).encode())
                finally:
                    await nc.close()
            env = {'STRATEGY_EVENT_MODE': 'JETSTREAM', 'STRATEGY_EVENT_RUNTIME_ID': 'authority-ci',
                   'MARKET_DATA_SOURCE': 'REDIS', 'NATS_URL': url,
                   'P2_NATS_USER': 'ci-only', 'P2_NATS_PASSWORD': 'ci-only'}
            with patch.dict(os.environ, env, clear=True):
                wake = Wakeup(orchestrator=True).start()
                try:
                    for subject, event_id in ((CACHE+'.'+b'BTCUSDm'.hex(), 'a'*64), (READY+'.observer', 'b'*64)):
                        started = time.monotonic()
                        asyncio.run(publish(subject, event_id))
                        self.assertEqual(wake.wait(60), 'EVENT')
                        self.assertLess(time.monotonic()-started, 1)
                        self.assertEqual(wake.last_event_id, event_id)
                finally:
                    wake.close()
                # Reuse durable subscriptions; do not reset their configuration.
                restarted = Wakeup(orchestrator=True).start()
                try:
                    asyncio.run(publish(READY+'.observer', 'c'*64))
                    self.assertEqual(restarted.wait(60), 'EVENT')
                finally:
                    restarted.close()


@unittest.skipUnless(os.environ.get('OUTCOME_RESOLVER_TEST_DSN'), 'isolated PostgreSQL unavailable')
class ReadyOutboxPostgresTests(unittest.TestCase):
    def test_checkpoint_and_notification_commit_or_rollback_together(self):
        import psycopg
        conn = psycopg.connect(os.environ['OUTCOME_RESOLVER_TEST_DSN'])
        self.addCleanup(conn.close)
        database = conn.execute('SELECT current_database()').fetchone()[0]
        conn.rollback()
        self.assertTrue('test' in database.lower() or database.lower().endswith('_ci'),
                        'refusing fixture writes outside isolated test database')
        runtime = 'event-ci-'+uuid.uuid4().hex
        env = {'STRATEGY_EVENT_MODE': 'JETSTREAM', 'STRATEGY_EVENT_RUNTIME_ID': runtime}
        with patch.dict(os.environ, env, clear=True):
            try:
                with self.assertRaisesRegex(RuntimeError, 'rollback'):
                    with conn.transaction():
                        failed = notify_ready({'entry': 'rollback'}, conn=conn)
                        raise RuntimeError('rollback')
                self.assertIsNone(conn.execute('SELECT event_id FROM platform.strategy_runtime_outbox WHERE event_id=%s', (failed,)).fetchone())
                conn.rollback()
                with conn.transaction():
                    committed = notify_ready({'entry': 'commit'}, conn=conn)
                    self.assertEqual(committed, notify_ready({'entry': 'commit'}, conn=conn))
                self.assertEqual(conn.execute('SELECT count(*) FROM platform.strategy_runtime_outbox WHERE runtime_id=%s', (runtime,)).fetchone()[0], 1)
                conn.rollback()
                with self.assertRaises(psycopg.errors.RaiseException):
                    with conn.transaction():
                        conn.execute("UPDATE platform.strategy_runtime_outbox SET payload='{}'::jsonb WHERE event_id=%s", (committed,))
                with conn.transaction():
                    conn.execute('UPDATE platform.strategy_runtime_outbox SET published_at=clock_timestamp() WHERE event_id=%s', (committed,))
            finally:
                conn.rollback()
                conn.execute('DELETE FROM platform.strategy_runtime_outbox WHERE runtime_id=%s', (runtime,))
                conn.commit()


if __name__ == '__main__':
    unittest.main()
