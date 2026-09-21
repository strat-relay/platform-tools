from __future__ import annotations

import json
import tempfile
import unittest
import os
from pathlib import Path

from migration.signal import canonical_signal
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
            "entry_price": 100, "stop_price": 99, "target_price": 102,
            "provenance": {"source": "frozen", "outcome": "WIN", "as_of": "2026-09-21T00:00:00Z"},
        }

    def test_canonical_mapping_hash_and_future_exclusion(self):
        signal = canonical_signal(self.raw(), source_reference="events.jsonl:1")
        self.assertEqual(signal.evaluation.strategy_id, "STRAT")
        self.assertEqual(signal.evaluation.trace_fidelity.value, "L1")
        self.assertEqual(signal.source_reference["source_reference"], "events.jsonl:1")
        self.assertNotIn("outcome", signal.evaluation.provenance)
        self.assertEqual(signal.canonical_hash, signal.evaluation.evaluation_hash)
        self.assertEqual(signal.canonical_hash, canonical_signal(self.raw(), source_reference="events.jsonl:1").canonical_hash)

    def test_identity_excludes_source_location_and_ingest_only_fields(self):
        first = canonical_signal(self.raw(), source_reference={"source_id": "a", "source_offset": 1})
        second = canonical_signal({**self.raw(), "as_of": "2026-09-22T00:00:00Z"}, source_reference={"source_id": "b", "source_offset": 999})
        self.assertEqual(first.evaluation.evaluation_hash, second.evaluation.evaluation_hash)
        self.assertEqual(first.entry_signal_hash, second.entry_signal_hash)

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
