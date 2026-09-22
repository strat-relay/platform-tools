from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from migration.reconcile import DEFAULT_RECONCILIATION_DELTA, ReconciliationStatus, reconcile
from migration.signal import LegacySignalTailer
from migration.signal_reconcile import reconciliation_delta


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, statement, args):
        self.conn.statements.append((statement, args))
        if "signal_ingest_quarantine" in statement:
            identity = tuple(args[:3])
            if identity not in self.conn.quarantine_keys:
                self.conn.quarantine_keys.add(identity)
                self.conn.quarantine_records.append(args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Connection:
    def __init__(self):
        self.statements = []
        self.quarantine_keys = set()
        self.quarantine_records = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _signal(signal_id: str, **overrides):
    return {
        "signal_id": signal_id,
        "strategy_id": "TEST_STRATEGY",
        "strategy_version": "V1",
        "symbol": "XAUUSDm",
        "direction": "LONG",
        "decision_time": "2026-09-20T12:00:00Z",
        "signal_emitted_at": "2026-09-20T12:00:01Z",
        "entry_mechanism": ["DEPTH_ONLY"],
        "entry_price": 100,
        "stop_price": 99,
        "target_price": 102,
        **overrides,
    }


class P2A11TailerHardeningTests(unittest.TestCase):
    def test_valid_bad_content_valid_quarantines_and_restart_advances(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, checkpoint = root / "signals.jsonl", root / "checkpoint.json"
            rows = [
                _signal("valid-1"),
                _signal("bad-time", decision_time=1758412800),
                _signal("bad-canonical", strategy_metadata=["not", "an", "object"]),
                _signal("valid-2"),
            ]
            # The epoch-only value is intentionally rejected by the P2-A1 contract.
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            conn = _Connection()
            accepted = []

            def accept(_conn, signal, **_kwargs):
                accepted.append(signal.signal_id)

            tailer = LegacySignalTailer(source, checkpoint, conn)
            with patch("migration.signal.ingest_signal", side_effect=accept):
                result = tailer.run_once()
                restarted = LegacySignalTailer(source, checkpoint, conn).run_once()

            self.assertEqual(accepted, ["valid-1", "valid-2"])
            self.assertEqual([row["signal_id"] for row in result.records], ["valid-1", "valid-2"])
            self.assertEqual([row["failure_class"] for row in result.malformed], ["CANONICAL_VALIDATION", "CANONICAL_VALIDATION"])
            self.assertEqual(restarted.records, [])
            self.assertEqual(restarted.malformed, [])
            quarantine_rows = [args for sql, args in conn.statements if "signal_ingest_quarantine" in sql]
            self.assertEqual(len(quarantine_rows), 2)
            self.assertTrue(all(args[0] == str(source) for args in quarantine_rows))
            self.assertEqual([args[1] for args in quarantine_rows], [len((json.dumps(rows[0]) + "\n").encode()),
                                                                    len((json.dumps(rows[0]) + "\n" + json.dumps(rows[1]) + "\n").encode())])
            self.assertTrue(all(args[4].startswith("CANONICAL_VALIDATION:") for args in quarantine_rows))
            self.assertEqual(conn.commits, 2)
            self.assertIn('"signal_id": "bad-time"', quarantine_rows[0][3])
            checkpoint_state = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertEqual(checkpoint_state["offset"], source.stat().st_size)
            self.assertEqual(checkpoint_state["source_inode"], source.stat().st_ino)

    def test_malformed_canonical_content_is_quarantined(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, checkpoint = root / "signals.jsonl", root / "checkpoint.json"
            source.write_text(json.dumps(_signal("bad", provenance="not-an-object")) + "\n", encoding="utf-8")
            conn = _Connection()
            tailer = LegacySignalTailer(source, checkpoint, conn)
            result = tailer.run_once()
            self.assertEqual(len(result.malformed), 1)
            self.assertEqual(result.malformed[0]["failure_class"], "CANONICAL_VALIDATION")
            self.assertEqual(len([1 for sql, _ in conn.statements if "signal_ingest_quarantine" in sql]), 1)

    def test_decode_error_is_classified_and_durable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, checkpoint = root / "signals.jsonl", root / "checkpoint.json"
            source.write_bytes(b"\xff\n" + (json.dumps(_signal("valid")) + "\n").encode())
            conn = _Connection()
            tailer = LegacySignalTailer(source, checkpoint, conn)
            result = tailer.run_once()
            self.assertEqual(result.malformed[0]["failure_class"], "DECODE_ERROR")
            self.assertEqual([row["signal_id"] for row in result.records], ["valid"])
            self.assertEqual(conn.quarantine_records[0][4].split(":", 1)[0], "DECODE_ERROR")

    def test_interrupt_after_quarantine_restarts_without_duplicate_effect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, checkpoint = root / "signals.jsonl", root / "checkpoint.json"
            rows = [_signal("valid-1"), _signal("bad", decision_time=1758412800), _signal("valid-2")]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            conn = _Connection()
            first_pass = []

            def fail_late(_conn, signal, **_kwargs):
                first_pass.append(signal.signal_id)
                if signal.signal_id == "valid-2":
                    raise RuntimeError("temporary database outage")

            with patch("migration.signal.ingest_signal", side_effect=fail_late):
                with self.assertRaisesRegex(RuntimeError, "temporary database outage"):
                    LegacySignalTailer(source, checkpoint, conn).run_once()
            self.assertFalse(checkpoint.exists())
            self.assertEqual(len(conn.quarantine_records), 1)

            accepted = []
            with patch("migration.signal.ingest_signal", side_effect=lambda _conn, signal, **_kwargs: accepted.append(signal.signal_id)):
                result = LegacySignalTailer(source, checkpoint, conn).run_once()
            self.assertEqual(accepted, ["valid-1", "valid-2"])
            self.assertEqual([row["signal_id"] for row in result.records], ["valid-1", "valid-2"])
            self.assertEqual(len(conn.quarantine_records), 1)

    def test_infrastructure_and_programming_failures_are_not_quarantined(self):
        for failure in (RuntimeError("database unavailable"), ValueError("unexpected programming defect")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source, checkpoint = root / "signals.jsonl", root / "checkpoint.json"
                source.write_text(json.dumps(_signal("valid")) + "\n", encoding="utf-8")
                conn = _Connection()
                tailer = LegacySignalTailer(source, checkpoint, conn)
                with patch("migration.signal.ingest_signal", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        tailer.run_once()
                self.assertFalse(checkpoint.exists())
                self.assertFalse(any("signal_ingest_quarantine" in sql for sql, _ in conn.statements))


class P2A11ReconciliationDeltaTests(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        self.delta = timedelta(seconds=32)

    def _status(self, record, database=()):
        result = reconcile([record], database, delta=self.delta, as_of=self.as_of)
        return result["findings"][0]["status"], result

    def test_fresh_legacy_only_record_is_expected_lag(self):
        status, result = self._status({"id": "fresh", "signal_emitted_at": self.as_of - timedelta(seconds=20)})
        self.assertEqual(status, ReconciliationStatus.EXPECTED_LAG.value)
        self.assertFalse(result["clean"])

    def test_legacy_only_record_after_delta_is_missing_database(self):
        status, _ = self._status({"id": "stale", "signal_emitted_at": self.as_of - timedelta(seconds=33)})
        self.assertEqual(status, ReconciliationStatus.MISSING_DATABASE.value)

    def test_quiesced_snapshot_after_delta_remains_strict(self):
        status, result = self._status({"id": "quiesced-gap", "signal_emitted_at": self.as_of - timedelta(minutes=5)})
        self.assertEqual(status, ReconciliationStatus.MISSING_DATABASE.value)
        self.assertFalse(result["clean"])

    def test_decision_time_does_not_grant_lag_grace(self):
        status, _ = self._status({"id": "old-event", "decision_time": self.as_of,
                                  "signal_emitted_at": self.as_of - timedelta(minutes=1)})
        self.assertEqual(status, ReconciliationStatus.MISSING_DATABASE.value)

    def test_missing_timestamp_and_unclassified_anomaly_hints_are_not_suppressed(self):
        missing_timestamp, _ = self._status({"id": "no-time"})
        anomaly_hint, _ = self._status({"id": "unrecognized", "signal_emitted_at": self.as_of - timedelta(minutes=1),
                                        "known_legacy_anomaly": True})
        self.assertEqual(missing_timestamp, ReconciliationStatus.MISSING_DATABASE.value)
        self.assertEqual(anomaly_hint, ReconciliationStatus.MISSING_DATABASE.value)

    def test_matching_signal_remains_match_even_when_old(self):
        record = {"id": "match", "hash": "same", "version": "V1",
                  "signal_emitted_at": self.as_of - timedelta(days=3)}
        status, _ = self._status(record, [dict(record)])
        self.assertEqual(status, ReconciliationStatus.MATCH.value)

    def test_entry_mechanism_collection_reconciles_membership_and_canonical_order(self):
        base = {"id": "mechanism-signal", "entry_mechanisms": ("DEPTH_ONLY", "REJECTION_WICK")}
        same_set = {**base, "entry_mechanisms": ("DEPTH_ONLY", "REJECTION_WICK")}
        changed = {**base, "entry_mechanisms": ("DEPTH_ONLY", "LOWER_TF_ENGULFING")}
        self.assertEqual(reconcile([base], [same_set])["findings"][0]["status"], ReconciliationStatus.MATCH.value)
        self.assertEqual(reconcile([base], [changed])["findings"][0]["status"], ReconciliationStatus.HASH_MISMATCH.value)

    def test_delta_default_and_environment_override(self):
        self.assertEqual(DEFAULT_RECONCILIATION_DELTA, timedelta(seconds=32))
        with patch.dict(os.environ, {"P2_RECONCILIATION_DELTA_SECONDS": "45"}):
            self.assertEqual(reconciliation_delta(), timedelta(seconds=45))
        with patch.dict(os.environ, {"P2_RECONCILIATION_DELTA_SECONDS": "-1"}):
            with self.assertRaises(ValueError):
                reconciliation_delta()

    def test_known_anomaly_enum_remains_for_compatibility_but_is_not_emitted(self):
        self.assertIn(ReconciliationStatus.KNOWN_LEGACY_ANOMALY.value,
                      {status.value for status in ReconciliationStatus})
        status, _ = self._status({"id": "not-classified", "signal_emitted_at": self.as_of - timedelta(minutes=1),
                                  "classification": "GAP_RECOVERY"})
        self.assertNotEqual(status, ReconciliationStatus.KNOWN_LEGACY_ANOMALY.value)


if __name__ == "__main__":
    unittest.main()
