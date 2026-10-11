from __future__ import annotations

import os
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.messaging.outbox_relay import OutboxRelay
from infrastructure.messaging.contracts import EventEnvelope
from infrastructure.messaging.jetstream import JetStreamPublisher
from migration.flags import SignalAuthorityFlags, SignalAuthorityMode
from migration.signal import CanonicalSignalIdentityConflict, ingest_signal
from orchestration.canonical_signal_publisher import CanonicalSignalPublisher
from orchestration.models import StrategySignal
from orchestration.storage import OrchestrationStore
from postgres.config import PostgresConfig
import signal_orchestrator as so


def accepted_signal(signal_id: str = "sig-canonical") -> StrategySignal:
    return StrategySignal(signal_id, "strategy-signal-v1", "CONTEXT_STRUCTURE_RETRACE_V1", "V1", "phase6",
        "source-event-1", "market-event-1", "setup-1", "opportunity-1", "position-1",
        "2026-09-22T10:00:00Z", "2026-09-22T09:59:00Z", "XAUUSDm", "XAUUSD", "XAUUSDm", "LONG",
        "MARKET_PAPER_OBSERVATION", 100.0, 99.0, 103.0, 1.0, 3.0, 3.0, "M15", "M5", ("H1", "H4"),
        ("REJECTION_WICK", "DEPTH_ONLY"), {"pattern": "test"}, {"classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL"},
        "2026-09-22T09:59:00Z", "2026-09-22T10:00:00Z")


class FakeSignalCursor:
    def __init__(self, conn):
        self.conn = conn
        self.last_sql = ""
        self.last_args = None

    def execute(self, sql, args=None):
        self.last_sql, self.last_args = sql, args
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("simulated database failure")
        if "INSERT INTO" in sql:
            self.conn.pending.append((sql, args))

    def fetchone(self):
        if "SELECT entry_signal_hash" in self.last_sql:
            signal_id = self.last_args[0]
            value = self.conn.entry_signals.get(signal_id)
            return (value,) if value is not None else None
        if "SELECT EXISTS" in self.last_sql and "entry_signal_mechanisms" in self.last_sql:
            return (True, True, True, True)
        return None

    def fetchall(self):
        if "SELECT signal_id FROM strategy.entry_signals" in self.last_sql:
            return [(signal_id,) for signal_id in self.conn.entry_signals]
        if "SELECT mechanism FROM strategy.entry_signal_mechanisms" in self.last_sql:
            signal_id = self.last_args[0]
            return [(mechanism,) for _, mechanism in sorted(self.conn.mechanisms.get(signal_id, []))]
        return []

    def __enter__(self): return self
    def __exit__(self, *_): return False


class FakeSignalDB:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.pending = []
        self.entry_signals = {}
        self.mechanisms = {}
        self.outbox = []
        self.statements = []
        self.rollbacks = 0

    def cursor(self): return FakeSignalCursor(self)

    def commit(self):
        self.statements.extend(self.pending)
        for sql, args in self.pending:
            if "INSERT INTO strategy.entry_signals" in sql:
                self.entry_signals[args["signal_id"]] = args["entry_signal_hash"]
            elif "INSERT INTO strategy.entry_signal_mechanisms" in sql:
                self.mechanisms.setdefault(args[0], []).append((args[2], args[1]))
            elif "INSERT INTO platform.outbox_events" in sql:
                self.outbox.append(args[0])
        self.pending.clear()

    def rollback(self):
        self.pending.clear()
        self.rollbacks += 1


class CanonicalSignalAuthorityTests(unittest.TestCase):
    def test_authoritative_writers_require_explicit_postgres_target(self):
        with self.assertRaisesRegex(ValueError, "explicit PostgreSQL target"):
            PostgresConfig().require_explicit_target()
        PostgresConfig(dsn="postgresql://configured-target/db").require_explicit_target()
        PostgresConfig(host="db.internal", port="5432", database="trading_platform",
                       user="signal_writer", password="configured-secret").require_explicit_target()

    def test_authority_modes_are_explicit_and_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIs(SignalAuthorityFlags.mode_from_env(), SignalAuthorityMode.LEGACY_FILE)
        with patch.dict(os.environ, {"SIGNAL_AUTHORITY_MODE": "DB_SHADOW"}, clear=True):
            self.assertIs(SignalAuthorityFlags.mode_from_env(), SignalAuthorityMode.DB_SHADOW)
        with patch.dict(os.environ, {"SIGNAL_AUTHORITY_MODE": "DB_PRIMARY",
                                    "SIGNAL_DB_PRIMARY_ENABLED": "true",
                                    "SIGNAL_JETSTREAM_PRIMARY_ENABLED": "true"}, clear=True):
            self.assertIs(SignalAuthorityFlags.mode_from_env(), SignalAuthorityMode.DB_PRIMARY)
        with patch.dict(os.environ, {"SIGNAL_AUTHORITY_MODE": "DB_PRIMARY",
                                    "SIGNAL_DB_PRIMARY_ENABLED": "true",
                                    "SIGNAL_JETSTREAM_PRIMARY_ENABLED": "false"}, clear=True):
            with self.assertRaises(ValueError): SignalAuthorityFlags.mode_from_env()
        SignalAuthorityFlags(True, True).validate(db_available=True, nats_available=False)

    def test_accepted_signal_commits_parent_mechanisms_evaluation_and_outbox_atomically(self):
        from unittest.mock import patch as mock_patch

        db = FakeSignalDB()
        publisher = CanonicalSignalPublisher(db, cutoff_id="cutoff-1", cutoff_utc="2026-09-22T09:58:00Z")
        publisher.require_schema()
        with mock_patch("migration.signal.persist_evaluation", return_value="eval-1"):
            canonical, inserted = publisher.publish(accepted_signal())
        self.assertTrue(inserted)
        self.assertEqual(canonical.fields["entry_mechanisms"], ("DEPTH_ONLY", "REJECTION_WICK"))
        self.assertEqual(canonical.evaluation.runtime_version, "signal-orchestrator.v1")
        self.assertEqual(canonical.evaluation.evaluator_version, "canonical-signal-publisher.v1")
        self.assertEqual(db.entry_signals, {"sig-canonical": canonical.entry_signal_hash})
        parent_args = next(args for sql, args in db.statements if "INSERT INTO strategy.entry_signals" in sql)
        self.assertEqual(parent_args["source_id"], "signal-orchestrator")
        self.assertEqual(parent_args["cutoff_id"], "cutoff-1")
        self.assertEqual(db.mechanisms["sig-canonical"], [(0, "DEPTH_ONLY"), (1, "REJECTION_WICK")])
        self.assertEqual(len(db.outbox), 2)
        self.assertEqual(db.pending, [])
        self.assertEqual(publisher.existing_signal_ids(), {"sig-canonical"})

    def test_publisher_requires_explicit_cutoff_and_rejects_pre_cutoff_signal(self):
        db = FakeSignalDB()
        with self.assertRaises(ValueError): CanonicalSignalPublisher(db, cutoff_id="", cutoff_utc="2026-09-22T09:58:00Z")
        with self.assertRaises(ValueError): CanonicalSignalPublisher(db, cutoff_id="cutoff-1", cutoff_utc="2026-09-22T09:58:00")
        publisher = CanonicalSignalPublisher(db, cutoff_id="cutoff-1", cutoff_utc="2026-09-22T10:00:00Z")
        with self.assertRaisesRegex(RuntimeError, "pre-cutoff"):
            publisher.publish(accepted_signal())
        self.assertEqual(db.pending, [])
        self.assertEqual(db.entry_signals, {})

    def test_database_failure_rolls_back_all_signal_and_outbox_writes(self):
        from unittest.mock import patch as mock_patch

        db = FakeSignalDB(fail_on="entry_signal_mechanisms")
        signal = __import__("migration.signal", fromlist=["canonical_signal"]).canonical_signal(
            accepted_signal().to_dict(), source_reference={"source_id": "signal-orchestrator", "cutoff_id": "cutoff-1"})
        with mock_patch("migration.signal.persist_evaluation", return_value="eval-1"):
            with self.assertRaisesRegex(RuntimeError, "simulated database failure"):
                ingest_signal(db, signal)
        self.assertEqual(db.entry_signals, {})
        self.assertEqual(db.mechanisms, {})
        self.assertEqual(db.outbox, [])
        self.assertEqual(db.rollbacks, 1)

    def test_duplicate_identity_is_idempotent_and_conflicting_identity_fails_closed(self):
        from unittest.mock import patch as mock_patch

        db = FakeSignalDB()
        publisher = CanonicalSignalPublisher(db, cutoff_id="cutoff-1", cutoff_utc="2026-09-22T09:58:00Z")
        with mock_patch("migration.signal.persist_evaluation", return_value="eval-1"):
            canonical, inserted = publisher.publish(accepted_signal())
            duplicate, inserted_again = publisher.publish(accepted_signal())
            with self.assertRaises(CanonicalSignalIdentityConflict):
                publisher.publish(StrategySignal(**{**accepted_signal().to_dict(), "entry_mechanism": ("DEPTH_ONLY",)}))
        self.assertTrue(inserted)
        self.assertFalse(inserted_again)
        self.assertEqual(canonical.entry_signal_hash, duplicate.entry_signal_hash)
        self.assertEqual(len(db.outbox), 2)
        self.assertEqual(len(db.mechanisms["sig-canonical"]), 2)

    def test_db_primary_poll_uses_database_not_signals_file_or_tailer(self):
        from migration.signal import LegacySignalTailer

        class FileForbiddenStore(OrchestrationStore):
            def rows(self, stream):
                if stream == "signals": raise AssertionError("DB_PRIMARY read legacy signals.jsonl")
                return super().rows(stream)

        class Adapter:
            strategy_id = "TEST_STRATEGY"
            def discover_new_signals(self, seen):
                self.seen = set(seen)
                return [accepted_signal()]

        class Publisher:
            def __init__(self): self.ids = set(); self.calls = []
            def existing_signal_ids(self): return set(self.ids)
            def publish(self, signal):
                self.calls.append(signal)
                if signal.signal_id in self.ids: return None, False
                self.ids.add(signal.signal_id)
                return object(), True

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = FileForbiddenStore(root)
            store.save_state({"processed_signal_ids": ["pre-cutoff-file-signal"]})
            adapter, publisher = Adapter(), Publisher()
            with patch.object(so, "load_adapters", return_value=[adapter]), \
                 patch.object(so, "route_signal") as route, \
                 patch.object(so, "event") as legacy_event, \
                 patch.object(LegacySignalTailer, "run_once", side_effect=AssertionError("shadow tailer invoked")):
                count = so.poll_once(store, {"mcp_url": "http://unused"}, {"freeze_timestamp": "2026-09-22T00:00:00Z"},
                    signal_authority_mode=SignalAuthorityMode.DB_PRIMARY, canonical_publisher=publisher)
            self.assertEqual(count, 1)
            self.assertEqual(adapter.seen, set())
            self.assertEqual(len(publisher.calls), 1)
            self.assertTrue(route.called)
            legacy_event.assert_not_called()
            self.assertFalse((root / "signals.jsonl").exists())
            self.assertNotIn("pre-cutoff-file-signal", store.load_state()["processed_signal_ids"])

    def test_jetstream_message_id_is_stable_outbox_event_id(self):
        class Client:
            def __init__(self): self.kwargs = None
            async def publish(self, subject, payload, **kwargs): self.kwargs = kwargs
        client = Client()
        event = EventEnvelope("evt-stable", "signal.entry.created.v1", "signal", "sig-canonical", 1,
                              "2026-09-22T10:00:00Z", {"entry_mechanisms": ["DEPTH_ONLY"]})
        import asyncio
        asyncio.run(JetStreamPublisher(client).publish(event))
        self.assertEqual(client.kwargs["headers"]["Nats-Msg-Id"], "evt-stable")

    def test_db_primary_database_failure_neither_routes_nor_falls_back_to_jsonl(self):
        class Adapter:
            strategy_id = "TEST_STRATEGY"
            def discover_new_signals(self, seen): return [accepted_signal()]
        class FailedPublisher:
            def existing_signal_ids(self): return set()
            def publish(self, signal): raise RuntimeError("DB unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); store = OrchestrationStore(root)
            with patch.object(so, "load_adapters", return_value=[Adapter()]), \
                 patch.object(so, "route_signal") as route:
                with self.assertRaisesRegex(RuntimeError, "DB unavailable"):
                    so.poll_once(store, {"mcp_url": "http://unused"}, {"freeze_timestamp": "2026-09-22T00:00:00Z"},
                        signal_authority_mode=SignalAuthorityMode.DB_PRIMARY,
                        canonical_publisher=FailedPublisher())
            route.assert_not_called()
            self.assertFalse((root / "signals.jsonl").exists())
            self.assertFalse((root / "events.jsonl").exists())

    def test_legacy_and_shadow_modes_keep_file_authority(self):
        class Adapter:
            strategy_id = "TEST_STRATEGY"
            def discover_new_signals(self, seen): return [accepted_signal()]
        for mode in (SignalAuthorityMode.LEGACY_FILE, SignalAuthorityMode.DB_SHADOW):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as td:
                store = OrchestrationStore(Path(td))
                with patch.object(so, "load_adapters", return_value=[Adapter()]), patch.object(so, "route_signal"):
                    so.poll_once(store, {"mcp_url": "http://unused"}, {"freeze_timestamp": "2026-09-22T00:00:00Z"}, signal_authority_mode=mode)
                self.assertEqual(store.rows("signals")[0]["signal_id"], "sig-canonical")

    def test_legacy_tailer_is_explicitly_migration_tooling(self):
        from migration.signal import LegacySignalTailer
        publisher_source = Path(inspect.getsourcefile(CanonicalSignalPublisher)).read_text()
        self.assertFalse(LegacySignalTailer.PRODUCTION_PRIMARY_COMPONENT)
        self.assertTrue(LegacySignalTailer.MIGRATION_TOOLING)
        self.assertNotIn("signals.jsonl", publisher_source)
        self.assertNotIn("JetStreamPublisher", publisher_source)


class OutboxRelayRestartTests(unittest.TestCase):
    def test_pending_events_are_selected_oldest_first(self):
        event_row = ("evt-1", "signal.entry.created.v1", "signal", "sig-1", 1,
                     "event-envelope.v1", {"signal_id": "sig-1"},
                     "2026-09-22T10:00:00Z", None, None)

        class Cursor:
            def __init__(self, conn): self.conn = conn; self.sql = ""
            def execute(self, sql, args=None):
                self.sql = sql
                self.conn.queries.append(sql)
                if "SELECT event_id,event_type" in sql:
                    self.rows = [event_row]
            def fetchall(self): return getattr(self, "rows", [])
            def __enter__(self): return self
            def __exit__(self, *_): return False

        class Conn:
            def __init__(self): self.queries = []
            def cursor(self):
                return Cursor(self)
            def commit(self): pass

        class Publisher:
            async def publish(self, envelope): pass

        import asyncio
        conn = Conn()
        asyncio.run(OutboxRelay(conn, Publisher()).publish_batch(limit=1))
        self.assertTrue(any("ORDER BY created_at, aggregate_id, aggregate_version NULLS LAST, event_id" in query
                            for query in conn.queries))

    def test_nats_failure_is_retryable_after_restart_without_signal_file(self):
        event_row = ("evt-1", "signal.entry.created.v1", "signal", "sig-1", 1,
                     "event-envelope.v1", {"signal_id": "sig-1", "entry_mechanisms": ["DEPTH_ONLY"]},
                     "2026-09-22T10:00:00Z", None, None)

        class Cursor:
            def __init__(self, conn): self.conn = conn; self.rowcount = 1
            def execute(self, sql, args=None):
                self.sql = sql
                if "SELECT event_id,event_type" in sql:
                    self.rows = [event_row] if self.conn.status != "PUBLISHED" else []
                elif "SET publish_status='PUBLISHED'" in sql:
                    self.conn.status = "PUBLISHED"
                elif "SET publish_status='FAILED'" in sql:
                    self.conn.status = "FAILED"
            def fetchall(self): return getattr(self, "rows", [])
            def __enter__(self): return self
            def __exit__(self, *_): return False
        class Conn:
            def __init__(self): self.status = "PENDING"
            def cursor(self): return Cursor(self)
            def commit(self): pass
        class FailOncePublisher:
            def __init__(self, fail): self.fail = fail; self.calls = []
            async def publish(self, envelope):
                self.calls.append(envelope.event_id)
                if self.fail: self.fail = False; raise RuntimeError("NATS unavailable")

        import asyncio
        conn = Conn(); first_pub = FailOncePublisher(True)
        first = asyncio.run(OutboxRelay(conn, first_pub).publish_batch())
        self.assertEqual(first, {"published": 0, "failed": 1})
        self.assertEqual(conn.status, "FAILED")
        self.assertEqual(first_pub.calls, ["evt-1"])
        restarted_pub = FailOncePublisher(False)
        retried = asyncio.run(OutboxRelay(conn, restarted_pub).publish_batch())
        self.assertEqual(retried, {"published": 1, "failed": 0})
        self.assertEqual(conn.status, "PUBLISHED")
        self.assertEqual(restarted_pub.calls, ["evt-1"])


if __name__ == "__main__":
    unittest.main()
