from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from migration.gates import CutoverGate, GateEvidence, evaluate_gate
from migration.modes import EventTransportMode, LegacyProjectionMode, RuntimeModes, StateAuthorityMode
from migration.observability import migration_status
from migration.projector import CompatibilityProjector
from migration.reconcile import ReconciliationStatus, reconcile
from migration.state import MigrationState, MigrationStateStore, transition
from migration.tailer import AppendOnlyTailer


class MigrationSubstrateTests(unittest.TestCase):
    def test_modes_are_orthogonal_and_fail_closed(self):
        RuntimeModes(StateAuthorityMode.LEGACY_FILE, EventTransportMode.LEGACY_FILE).validate()
        with self.assertRaises(ValueError):
            RuntimeModes(StateAuthorityMode.LEGACY_FILE, EventTransportMode.JETSTREAM_SHADOW).validate()
        with self.assertRaises(ValueError):
            RuntimeModes(StateAuthorityMode.DB_PRIMARY, EventTransportMode.LEGACY_FILE, LegacyProjectionMode.DISABLED).validate()
        with self.assertRaises(RuntimeError):
            RuntimeModes(StateAuthorityMode.DB_PRIMARY, EventTransportMode.JETSTREAM_PRIMARY).require_dependencies(database_available=lambda: False)
        with self.assertRaises(RuntimeError):
            RuntimeModes(StateAuthorityMode.DB_PRIMARY, EventTransportMode.JETSTREAM_PRIMARY).require_dependencies(database_available=lambda: True, jetstream_available=lambda: False)

    def test_state_transitions_and_durable_store(self):
        self.assertEqual(transition(MigrationState.NOT_STARTED, MigrationState.SHADOW_WRITE), MigrationState.SHADOW_WRITE)
        with self.assertRaises(ValueError): transition(MigrationState.NOT_STARTED, MigrationState.RETIRED)
        with tempfile.TemporaryDirectory() as td:
            store = MigrationStateStore(Path(td) / "state.json")
            store.write("signals", MigrationState.SHADOW_WRITE, {"run": "test"})
            self.assertEqual(store.read()["signals"]["state"], "SHADOW_WRITE")

    def test_projector_is_deterministic_idempotent_and_compatibility_only(self):
        with tempfile.TemporaryDirectory() as td:
            p = CompatibilityProjector(Path(td) / "projection.jsonl")
            event = {"event_id": "e1", "aggregate_id": "a", "value": 1}
            self.assertTrue(p.project(event)); self.assertFalse(p.project(event))
            row = json.loads((Path(td) / "projection.jsonl").read_text())
            self.assertTrue(row["compatibility_only"])

    def test_tailer_partial_malformed_duplicate_restart_and_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source, checkpoint = root / "events.jsonl", root / "checkpoint.json"
            source.write_text('{"id":"1"}\n{"id":"2"')
            first = AppendOnlyTailer(source, checkpoint).run_once()
            self.assertEqual([r["id"] for r in first.records], ["1"])
            with source.open("a") as fh: fh.write('}\nnot-json\n')
            second = AppendOnlyTailer(source, checkpoint).run_once()
            self.assertEqual([r["id"] for r in second.records], ["2"]); self.assertEqual(len(second.malformed), 1)
            self.assertEqual(AppendOnlyTailer(source, checkpoint).run_once().records, [])
            source.write_text('{"id":"rotated"}\n')
            rotated = AppendOnlyTailer(source, checkpoint).run_once()
            self.assertTrue(rotated.rotated); self.assertEqual(rotated.records[0]["id"], "rotated")

    def test_reconciliation_reports_concrete_statuses(self):
        result = reconcile(
            [{"id": "a", "hash": "same", "version": 1}, {"id": "b", "hash": "old"}, {"id": "c"}],
            [{"id": "a", "hash": "same", "version": 1}, {"id": "b", "hash": "new"}, {"id": "d"}],
        )
        statuses = {x["identity"]: x["status"] for x in result["findings"]}
        self.assertEqual(statuses["a"], ReconciliationStatus.MATCH.value)
        self.assertEqual(statuses["b"], ReconciliationStatus.HASH_MISMATCH.value)
        self.assertEqual(statuses["c"], ReconciliationStatus.MISSING_DATABASE.value)
        self.assertEqual(statuses["d"], ReconciliationStatus.MISSING_LEGACY.value)

    def test_gate_requires_evidence_and_observability_is_read_only(self):
        denied = evaluate_gate(CutoverGate.DB_AUTHORITY_READY, GateEvidence(measures={"mismatch_count": 0}))
        self.assertFalse(denied["approved"])
        approved = evaluate_gate(CutoverGate.DB_AUTHORITY_READY, GateEvidence(("run-1",), {"mismatch_count": 0}))
        self.assertTrue(approved["approved"])
        status = migration_status(modes=RuntimeModes(StateAuthorityMode.DB_SHADOW, EventTransportMode.JETSTREAM_SHADOW), phase="P1", gate_status=approved, reconciliation={"summary": {"MATCH": 2}}, unresolved_sites=3, health={"outbox_unpublished": 1}, schema_version="009")
        self.assertEqual(status["postgres_schema_version"], "009")
        self.assertEqual(status["mismatch_count"], 0)


if __name__ == "__main__":
    unittest.main()
