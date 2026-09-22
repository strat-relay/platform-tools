from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from migration.cutoff import establish_cutoff
from migration.signal_reconcile import reconcile_legacy_signals
from migration.tailer import AppendOnlyTailer


class CutoffTests(unittest.TestCase):
    def test_first_start_captures_eof_and_restart_resumes_durable_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, marker, checkpoint = root / "signals.jsonl", root / "marker.json", root / "checkpoint.json"
            source.write_text('{"signal_id":"old","signal_timestamp":"t-old"}\n', encoding="utf-8")
            boundary, created = establish_cutoff(source, marker, checkpoint, provenance={"commit": "test"})
            initial_offset = source.stat().st_size
            self.assertTrue(created)
            self.assertEqual(boundary["source_start_cursor"], initial_offset)
            self.assertEqual(boundary["source_file"]["last_complete_pre_cutoff_signal"]["signal_id"], "old")

            ingested = []
            with source.open("a") as handle:
                handle.write('{"signal_id":"new-1"}\n')
            result = AppendOnlyTailer(source, checkpoint, ingest=lambda row: ingested.append(row["signal_id"]),
                                      reject_rotation=True).run_once()
            self.assertEqual([row["signal_id"] for row in result.records], ["new-1"])
            self.assertEqual(ingested, ["new-1"])
            advanced_checkpoint = json.loads(checkpoint.read_text())

            # Simulate a later append while the process is down. Reopening the
            # cutoff must not resample EOF or move the durable checkpoint.
            with source.open("a") as handle:
                handle.write('{"signal_id":"new-2"}\n')
            resumed, created_again = establish_cutoff(source, marker, checkpoint, provenance={"commit": "changed"})
            self.assertFalse(created_again)
            self.assertEqual(resumed["cutoff_id"], boundary["cutoff_id"])
            self.assertEqual(json.loads(checkpoint.read_text()), advanced_checkpoint)
            next_result = AppendOnlyTailer(source, checkpoint, ingest=lambda row: ingested.append(row["signal_id"]),
                                           reject_rotation=True).run_once()
            self.assertEqual([row["signal_id"] for row in next_result.records], ["new-2"])
            self.assertEqual(ingested, ["new-1", "new-2"])

    def test_cutoff_refuses_partial_eof_and_post_cutoff_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, marker, checkpoint = root / "signals.jsonl", root / "marker.json", root / "checkpoint.json"
            source.write_text('{"signal_id":"partial"', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "incomplete record"):
                establish_cutoff(source, marker, checkpoint, provenance={})
            source.write_text('{"signal_id":"old"}\n', encoding="utf-8")
            establish_cutoff(source, marker, checkpoint, provenance={})
            source.write_text('{"signal_id":"replacement"}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed after cutoff"):
                AppendOnlyTailer(source, checkpoint, reject_rotation=True).run_once()

    def test_reconciliation_excludes_pre_cutoff_legacy_and_database_rows(self):
        class Cursor:
            def __init__(self, conn): self.conn = conn
            def execute(self, sql, args=None): self.conn.executed.append((sql, args))
            def fetchall(self): return []
            def __enter__(self): return self
            def __exit__(self, *args): return False
        class Conn:
            def __init__(self): self.executed = []
            def cursor(self): return Cursor(self)
            def commit(self): pass

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "signals.jsonl"
            pre = '{"signal_id":"old","not":"canonical"}\n'
            post = {"signal_id": "new", "strategy_id": "S", "strategy_version": "V1",
                    "symbol": "XAUUSDm", "direction": "LONG", "decision_time": "2026-09-21T00:00:00Z",
                    "signal_emitted_at": "2026-09-21T00:00:00Z", "entry_mechanisms": ["DEPTH_ONLY"]}
            source.write_text(pre + json.dumps(post) + "\n", encoding="utf-8")
            conn = Conn()
            result = reconcile_legacy_signals(conn, source, run_id="test-cutoff",
                source_id=str(source), cutoff_offset=len(pre.encode()),
                as_of=datetime(2026, 9, 21, 0, 1, tzinfo=timezone.utc))
            self.assertEqual(len(result["findings"]), 1)
            self.assertEqual(result["findings"][0]["identity"], "new")
            selection_sql, selection_args = next((sql, args) for sql, args in conn.executed if "FROM strategy.entry_signals" in sql)
            self.assertIn("source_offset", selection_sql)
            self.assertIn(str(source), selection_args)
            self.assertIn(len(pre.encode()), selection_args)


if __name__ == "__main__":
    unittest.main()
