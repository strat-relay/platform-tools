from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from migration.signal import canonical_signal
from migration.signal_shadow import SignalShadowConsumer
from migration.tailer import AppendOnlyTailer


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
        self.assertEqual(signal.evaluation.provenance["legacy_source_reference"], "events.jsonl:1")
        self.assertNotIn("outcome", signal.evaluation.provenance)
        self.assertEqual(signal.canonical_hash, signal.evaluation.evaluation_hash)
        self.assertEqual(signal.canonical_hash, canonical_signal(self.raw(), source_reference="events.jsonl:1").canonical_hash)

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
