"""Real transaction/immutability tests, restricted to the isolated CI database."""
import os
import unittest
import uuid

DSN=os.environ.get('OUTCOME_RESOLVER_TEST_DSN')


@unittest.skipUnless(DSN,'isolated PostgreSQL DSN unavailable')
class LibraryStreamPostgresTests(unittest.TestCase):
    def test_committed_evidence_replay_and_outbox_are_idempotent(self):
        import psycopg
        from psycopg.errors import RaiseException
        from tests.test_library_event_stream import frames
        from research.library_event_store import EventStore, MARKET, ENTRY, encode
        from research.library_event_consumer import process_market, reconcile_market
        from research.strategy_library_forward import ForwardObserver
        from dataclasses import replace
        provider='STREAM_TEST_'+uuid.uuid4().hex
        cfg,_,_,items=frames(provider)
        cfg=replace(cfg,run_id='event-ci-'+uuid.uuid4().hex)
        conn=psycopg.connect(DSN)
        with conn.cursor() as cur:
            cur.execute('SELECT current_database()');db=cur.fetchone()[0]
        conn.rollback()
        if not ('test' in db.lower() or db.lower().endswith('_ci')):
            conn.close()
            self.fail('refusing fixture writes outside isolated test database')
        try:
            with conn.cursor() as cur:
                state=ForwardObserver(cfg).state
                from research.library_event_store import SCHEMA
                state['activation_attestation']={'schema':SCHEMA,'run_id':cfg.run_id,'binding':state['binding'],
                    'activation_time':cfg.activation_time,'transport':'JETSTREAM','execution_eligible':False}
                cur.execute('INSERT INTO research.strategy_library_forward_state(run_id,state) VALUES (%s,%s::jsonb)',
                            (cfg.run_id,encode(state)))
            conn.commit();store=EventStore(conn);ids=[]
            for event,snapshot,now in items:
                ids.append(store.market(provider,now,snapshot=snapshot,quote=event['quote']))
            # Receiving the later notification first recovers the retained immutable prefix.
            result=reconcile_market(conn,cfg,ids[-1],now=items[-1][2])
            self.assertEqual(result,'PROCESSED')
            self.assertEqual(process_market(conn,cfg,ids[-1],now=items[-1][2]),'DUPLICATE')
            with conn.cursor() as cur:
                cur.execute('SELECT count(*) FROM research.library_stream_outbox WHERE provider_symbol=%s AND subject=%s',(provider,ENTRY))
                self.assertEqual(cur.fetchone()[0],1)
                cur.execute('SELECT count(*) FROM research.library_stream_inbox WHERE consumer_id=%s',('library-observer:'+cfg.run_id,))
                self.assertEqual(cur.fetchone()[0],2)
            conn.commit()
            with self.assertRaises(RaiseException):
                with conn.transaction():
                    conn.execute("UPDATE research.market_event_snapshot SET payload='{}'::jsonb WHERE provider_symbol=%s",(provider,))
            with self.assertRaises(RaiseException):
                with conn.transaction():
                    conn.execute("UPDATE research.library_stream_outbox SET payload='{}'::jsonb WHERE event_id=%s",(ids[0],))
            with conn.transaction():
                conn.execute('UPDATE research.library_stream_outbox SET published_at=clock_timestamp() WHERE event_id=%s',(ids[0],))
            # Source sequence and snapshot/outbox insert all roll back together.
            before=conn.execute('SELECT sequence FROM research.market_event_head WHERE provider_symbol=%s',(provider,)).fetchone()[0]
            conn.rollback()
            with self.assertRaisesRegex(ValueError,'backwards'):
                store.market(provider,items[0][2]-1,quote=items[0][0]['quote'])
            after=conn.execute('SELECT sequence FROM research.market_event_head WHERE provider_symbol=%s',(provider,)).fetchone()[0]
            self.assertEqual(before,after);conn.rollback()
        finally:
            conn.rollback()
            # Only per-test unique identities are removed, never shared strategy/catalog rows.
            with conn.cursor() as cur:
                cur.execute('DELETE FROM research.library_stream_inbox WHERE consumer_id=%s',('library-observer:'+cfg.run_id,))
                cur.execute('DELETE FROM research.library_stream_cursor WHERE consumer_id=%s',('library-observer:'+cfg.run_id,))
                cur.execute('DELETE FROM research.strategy_library_forward_event WHERE run_id=%s',(cfg.run_id,))
                cur.execute('DELETE FROM research.strategy_library_forward_state WHERE run_id=%s',(cfg.run_id,))
                cur.execute('DELETE FROM research.library_stream_outbox WHERE provider_symbol=%s',(provider,))
                cur.execute('DELETE FROM research.market_event_head WHERE provider_symbol=%s',(provider,))
                cur.execute('DELETE FROM research.market_event_snapshot WHERE provider_symbol=%s',(provider,))
            conn.commit();conn.close()
