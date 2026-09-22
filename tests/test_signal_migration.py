from __future__ import annotations

import json
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from migration.signal import canonical_signal, ingest_signal, load_entry_mechanisms
from migration.tailer import QuarantinedRecordError
from migration.signal_shadow import SignalShadowConsumer
from migration.tailer import AppendOnlyTailer
from migration.flags import SignalAuthorityFlags


class SignalMigrationTests(unittest.TestCase):
    def raw(self):
        return {
            "signal_id": "sig-1", "strategy_id": "STRAT", "strategy_version": "V1",
            "strategy_instance_id": "inst", "source_event_id": "event-1", "symbol": "XAUUSDm",
            "canonical_symbol": "XAUUSD", "broker_symbol_hint": "XAUUSDm", "direction": "LONG",
            "signal_timestamp": "2026-09-21T00:00:00Z", "created_at": "2026-09-21T00:00:01Z",
            "entry_mechanisms": [],
            "entry_price": 100, "stop_price": 99, "target_price": 102,
            "provenance": {"source": "frozen", "outcome": "WIN", "as_of": "2026-09-21T00:00:00Z"},
        }

    def test_canonical_mapping_hash_and_future_exclusion(self):
        signal = canonical_signal(self.raw(), source_reference="events.jsonl:1")
        self.assertEqual(signal.evaluation.strategy_id, "STRAT")
        self.assertEqual(signal.evaluation.trace_fidelity.value, "L1")
        self.assertEqual(signal.source_reference["source_reference"], "events.jsonl:1")
        self.assertNotIn("outcome", signal.evaluation.provenance)
        self.assertEqual(signal.source_provenance["outcome"], "WIN")
        self.assertEqual(signal.canonical_hash, signal.evaluation.evaluation_hash)
        self.assertEqual(signal.canonical_hash, canonical_signal(self.raw(), source_reference="events.jsonl:1").canonical_hash)

    def test_identity_excludes_source_location_and_ingest_only_fields(self):
        first = canonical_signal(self.raw(), source_reference={"source_id": "a", "source_offset": 1})
        second = canonical_signal({**self.raw(), "as_of": "2026-09-22T00:00:00Z"}, source_reference={"source_id": "b", "source_offset": 999})
        self.assertEqual(first.evaluation.evaluation_hash, second.evaluation.evaluation_hash)
        self.assertEqual(first.entry_signal_hash, second.entry_signal_hash)

    def test_full_source_provenance_is_retained_without_hashing_outcomes(self):
        original = self.raw()
        changed_outcome = {**original, "provenance": {**original["provenance"], "outcome": "LOSS"}}
        first = canonical_signal(original)
        second = canonical_signal(changed_outcome)
        self.assertEqual(first.source_provenance["outcome"], "WIN")
        self.assertEqual(second.source_provenance["outcome"], "LOSS")
        self.assertEqual(first.entry_signal_hash, second.entry_signal_hash)

    def test_semantic_geometry_changes_entry_signal_hash(self):
        first = canonical_signal(self.raw())
        second = canonical_signal({**self.raw(), "target_price": 103})
        self.assertNotEqual(first.entry_signal_hash, second.entry_signal_hash)

    def test_entry_mechanisms_are_strict_sorted_set_like_domain_data(self):
        mechanisms = ("DEPTH_ONLY", "REJECTION_WICK", "LOWER_TF_ENGULFING", "MORNING_EVENING_STAR",
                      "LIQUIDITY_RECLAIM", "COMPLETED_CANDLE_RETRACEMENT")
        for mechanism in mechanisms:
            with self.subTest(mechanism=mechanism):
                signal = canonical_signal({**self.raw(), "entry_mechanisms": [mechanism]})
                self.assertEqual(signal.fields["entry_mechanisms"], (mechanism,))
        raw = {**self.raw(), "entry_mechanisms": ["DEPTH_ONLY", "REJECTION_WICK"]}
        reversed_raw = {**raw, "entry_mechanisms": ["REJECTION_WICK", "DEPTH_ONLY"]}
        signal = canonical_signal(raw, source_reference={"source_id": "a", "source_offset": 1})
        same = canonical_signal(reversed_raw, source_reference={"source_id": "b", "source_offset": 90})
        self.assertEqual(signal.fields["entry_mechanisms"], ("DEPTH_ONLY", "REJECTION_WICK"))
        self.assertEqual(signal.entry_signal_hash, same.entry_signal_hash)
        self.assertEqual(signal.canonical_hash, same.canonical_hash)
        changed = canonical_signal({**raw, "entry_mechanisms": ["DEPTH_ONLY", "LOWER_TF_ENGULFING"]})
        self.assertNotEqual(signal.entry_signal_hash, changed.entry_signal_hash)
        self.assertEqual(signal.signal_id, changed.signal_id)
        self.assertEqual(signal.candidate_id, changed.candidate_id)
        self.assertEqual(signal.evaluation.evaluation_hash, changed.evaluation.evaluation_hash)
        producer_raw = {key: value for key, value in self.raw().items() if key != "entry_mechanisms"}
        producer_wire = canonical_signal({**producer_raw, "entry_mechanism": ["DEPTH_ONLY", "LOWER_TF_ENGULFING"]})
        self.assertEqual(producer_wire.fields["entry_mechanisms"], ("DEPTH_ONLY", "LOWER_TF_ENGULFING"))
        with self.assertRaises(QuarantinedRecordError):
            canonical_signal({**self.raw(), "entry_mechanism": "DEPTH_ONLY"})

    def test_mechanism_collection_empty_allowed_null_and_invalid_values_rejected(self):
        self.assertEqual(canonical_signal({**self.raw(), "entry_mechanisms": []}).fields["entry_mechanisms"], ())
        for bad in (None, "DEPTH_ONLY", ["DEPTH_ONLY", 4], ["DEPTH_ONLY", {}], ["DEPTH_ONLY", "DEPTH_ONLY"], [""]):
            with self.subTest(bad=bad), self.assertRaises(QuarantinedRecordError):
                canonical_signal({**self.raw(), "entry_mechanisms": bad})
        with self.assertRaises(QuarantinedRecordError):
            canonical_signal({k: v for k, v in self.raw().items() if k != "entry_mechanisms"})

    def test_canonical_mechanism_serialization_is_stable_and_order_normalized(self):
        import hashlib
        from core.strategies.evaluation import canonical_bytes
        first = canonical_signal({**self.raw(), "entry_mechanisms": ["DEPTH_ONLY", "REJECTION_WICK"]})
        second = canonical_signal({**self.raw(), "entry_mechanisms": ["REJECTION_WICK", "DEPTH_ONLY"]})
        payload1 = {"signal_id": first.signal_id, "entry_mechanisms": first.fields["entry_mechanisms"]}
        payload2 = {"signal_id": second.signal_id, "entry_mechanisms": second.fields["entry_mechanisms"]}
        self.assertEqual(canonical_bytes(payload1), canonical_bytes(payload2))
        self.assertEqual(hashlib.sha256(canonical_bytes(payload1)).hexdigest(),
                         hashlib.sha256(canonical_bytes(payload2)).hexdigest())

    def test_parent_children_and_outbox_are_written_atomically_and_round_trip(self):
        class Cursor:
            def __init__(self, conn): self.conn, self.rowcount = conn, 1
            def execute(self, sql, args=None):
                if self.conn.fail_on_child and "entry_signal_mechanisms" in sql:
                    raise RuntimeError("child insert failed")
                self.conn.pending.append((sql, args))
            def fetchone(self): return None
            def fetchall(self): return [("DEPTH_ONLY",), ("REJECTION_WICK",)]
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Conn:
            def __init__(self, fail_on_child=False):
                self.pending, self.committed, self.fail_on_child, self.rollbacks = [], [], fail_on_child, 0
            def cursor(self): return Cursor(self)
            def commit(self): self.committed.extend(self.pending); self.pending.clear()
            def rollback(self): self.pending.clear(); self.rollbacks += 1
        signal = canonical_signal({**self.raw(), "entry_mechanisms": ["DEPTH_ONLY", "REJECTION_WICK"]})
        conn = Conn()
        with patch("migration.signal.persist_evaluation", return_value=signal.evaluation.evaluation_hash):
            self.assertTrue(ingest_signal(conn, signal))
        child_rows = [args for sql, args in conn.committed if "INSERT INTO strategy.entry_signal_mechanisms" in sql]
        self.assertEqual(child_rows, [("sig-1", "DEPTH_ONLY", 0), ("sig-1", "REJECTION_WICK", 1)])
        parent_sql = next(sql for sql, _ in conn.committed if "INSERT INTO strategy.entry_signals" in sql)
        self.assertNotIn("entry_mechanisms", parent_sql)
        event_args = [args for sql, args in conn.committed if "INSERT INTO platform.outbox_events" in sql]
        entry_event = next(args for args in event_args if args[1] == "signal.entry.created.v1")
        self.assertEqual(json.loads(entry_event[4])["entry_mechanisms"], ["DEPTH_ONLY", "REJECTION_WICK"])
        self.assertEqual(load_entry_mechanisms(conn, "sig-1"), ("DEPTH_ONLY", "REJECTION_WICK"))
        failed = Conn(fail_on_child=True)
        with patch("migration.signal.persist_evaluation", return_value=signal.evaluation.evaluation_hash):
            with self.assertRaisesRegex(RuntimeError, "child insert failed"):
                ingest_signal(failed, signal)
        self.assertEqual(failed.committed, [])
        self.assertEqual(failed.rollbacks, 1)

    def test_relational_mechanism_migration_has_order_and_integrity_constraints(self):
        migration = Path(__file__).resolve().parents[1] / "postgres/migrations/012_entry_mechanisms_relational.sql"
        text = migration.read_text()
        self.assertIn("CREATE TABLE strategy.entry_signal_mechanisms", text)
        self.assertIn("REFERENCES strategy.entry_signals(signal_id) ON DELETE CASCADE", text)
        self.assertIn("PRIMARY KEY (entry_signal_id, mechanism)", text)
        self.assertIn("UNIQUE (entry_signal_id, position)", text)
        self.assertIn("CHECK (position >= 0)", text)

    def test_decision_time_is_normalized_and_epoch_rejected(self):
        self.assertEqual(canonical_signal({**self.raw(), "decision_time": "2026-09-21T02:00:00-02:00"}).evaluation.decision_time, "2026-09-21T04:00:00.000000Z")
        with self.assertRaises(ValueError):
            canonical_signal({**self.raw(), "decision_time": 1758412800})

    def test_authority_flags_default_closed_and_validate_dependencies(self):
        old_db, old_js = os.environ.pop("SIGNAL_DB_PRIMARY_ENABLED", None), os.environ.pop("SIGNAL_JETSTREAM_PRIMARY_ENABLED", None)
        try:
            self.assertEqual(SignalAuthorityFlags.from_env(), SignalAuthorityFlags())
            with self.assertRaises(RuntimeError):
                SignalAuthorityFlags(True, False).validate(db_available=False)
            os.environ["SIGNAL_JETSTREAM_PRIMARY_ENABLED"] = "true"
            with self.assertRaises(ValueError):
                SignalAuthorityFlags.from_env()
        finally:
            if old_db is not None: os.environ["SIGNAL_DB_PRIMARY_ENABLED"] = old_db
            if old_js is not None: os.environ["SIGNAL_JETSTREAM_PRIMARY_ENABLED"] = old_js

    def test_s0_tailer_restart_partial_malformed_and_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "signals.jsonl"; checkpoint = root / "checkpoint.json"
            source.write_text(json.dumps(self.raw())[:-1])
            first = AppendOnlyTailer(source, checkpoint).run_once(); self.assertEqual(first.records, [])
            with source.open("a") as fh: fh.write("}\nnot-json\n")
            second = AppendOnlyTailer(source, checkpoint).run_once()
            self.assertEqual(len(second.records), 1); self.assertEqual(len(second.malformed), 1)
            self.assertEqual(AppendOnlyTailer(source, checkpoint).run_once().records, [])
            source.write_text(json.dumps({**self.raw(), "signal_id": "sig-rotated"}) + "\n")
            rotated = AppendOnlyTailer(source, checkpoint).run_once()
            self.assertTrue(rotated.rotated); self.assertEqual(rotated.records[0]["signal_id"], "sig-rotated")

    def test_shadow_consumer_dedupes_redelivery_without_execution(self):
        class Cursor:
            rowcount = 1
            def execute(self, sql, args): self.sql, self.args = sql, args
            def fetchone(self): return (1,)
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Conn:
            def __init__(self): self.claimed = set(); self.cursor_obj = Cursor()
            def cursor(self):
                c = self
                class C(Cursor):
                    def execute(self, sql, args):
                        self.args = args
                        if "INSERT INTO platform.inbox_events" in sql:
                            self.rowcount = int(args[1] not in c.claimed); c.claimed.add(args[1])
                        else: self.rowcount = 1
                return C()
            def commit(self): pass
            def rollback(self): pass
        conn = Conn(); consumer = SignalShadowConsumer(conn)
        payload = json.dumps({"event_id": "evt-1", "event_type": "signal.entry.created.v1"}).encode()
        self.assertTrue(consumer.handle(payload)); self.assertFalse(consumer.handle(payload, redelivered=True))
        self.assertEqual(consumer.metrics.processed, 1); self.assertEqual(consumer.metrics.duplicate_hits, 1)


if __name__ == "__main__": unittest.main()
