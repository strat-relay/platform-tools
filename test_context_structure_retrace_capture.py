from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research.context_structure_retrace_capture import (
    V1_CONFIG_HASH, V1_STRATEGY_FINGERPRINT, V2_CONTRACT_HASH, V2_PARAMETER_HASH,
    capture, freeze_precapture, records_at_t0, verify_precapture, verify_sealed_capture,
)


def metadata(**overrides: object) -> dict:
    value = {
        "experiment_id": "exp-1", "captured_at_utc": "2026-10-07T12:00:00Z", "t0_utc": "2026-10-07T12:00:00Z",
        "capture_started_at": "2026-10-07T11:59:50Z", "capture_completed_at": "2026-10-07T12:00:10Z",
        "platform_source_commit": "a" * 40, "gitops_revision": "b" * 40,
        "runtime_image": "ghcr.io/example/runtime:tested", "runtime_image_digest": "sha256:" + "c" * 64,
        "migration_version": "040", "schema_fingerprint": "d" * 64,
        "strategy_v1": {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1",
                         "config_hash": V1_CONFIG_HASH, "strategy_fingerprint": V1_STRATEGY_FINGERPRINT},
        "strategy_v2": {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V2", "strategy_version": "V2",
                         "mode": "RESEARCH_ONLY", "min_planned_r": 1.0, "broker_writes": False,
                         "contract_hash": V2_CONTRACT_HASH, "parameter_hash": V2_PARAMETER_HASH},
        "execution_safety": {"db_authority_state": "DISABLED", "db_authority_revision": 1,
                              "runtime_authority_state": "DISABLED", "effective_authority_state": "DISABLED",
                              "execution_allowlist": ["CONTEXT_STRUCTURE_RETRACE_V1@V1"],
                              "v2_execution_enabled": False, "v2_execution_intent_count": 0,
                              "v2_execution_attempt_count": 0},
        "evidence_boundaries": {"state": {"source_timestamp": "2026-10-07T12:00:01Z"},
                                 "market_data": {"source_timestamp": "2026-10-07T12:00:02Z"},
                                 "publication": {"max_id": 10}, "execution": {"max_id": 20},
                                 "post_exit_observer": {"max_id": 30}, "decision_ledger": {"last_hash": "e" * 64}},
        "source_skew": {source: {"source_timestamp": "2026-10-07T11:59:59Z",
                                  "capture_timestamp": "2026-10-07T12:00:00Z",
                                  "offset_from_t0_ms": 1000, "max_allowed_skew_ms": 30000}
                       for source in ("state", "market_data", "publication", "execution", "observer", "decision_ledger")},
        "ledger": {"previous_anchor_hash": "f" * 64, "first_post_t0_sequence": None},
        "deployment_precapture": {"directory": "precapture", "manifest_sha256": "0" * 64,
                                   "captured_at_utc": "2026-10-07T11:59:00Z"},
    }
    for key, patch in overrides.items():
        if isinstance(patch, dict) and isinstance(value.get(key), dict):
            value[key] = {**value[key], **patch}
        else:
            value[key] = patch
    return value


def prepare(root: Path) -> tuple[dict, Path]:
    source = root / "deployment-source.json"
    source.write_text('{"runtime_image_digest":"sha256:test"}')
    precapture_dir = root / "precapture"
    frozen = freeze_precapture(precapture_dir, source, captured_at_utc="2026-10-07T11:59:00Z")
    meta = metadata(deployment_precapture={"directory": str(precapture_dir),
                                           "manifest_sha256": frozen["manifest_sha256"],
                                           "captured_at_utc": frozen["captured_at_utc"]})
    return meta, precapture_dir


class ContextRetraceCaptureTests(unittest.TestCase):
    def test_precapture_is_sealed_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "deployment-source.json"
            source.write_text('{"runtime_image_digest":"sha256:test"}')
            precapture = root / "precapture"
            freeze_precapture(precapture, source, captured_at_utc="2026-10-07T11:59:00Z")
            self.assertEqual(verify_precapture(precapture)["status"], "FROZEN")
            (precapture / "deployment.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "artifact tampering"):
                verify_precapture(precapture)

    def test_complete_manifest_seals_and_second_capture_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "bars.jsonl"; source.write_text('{"timestamp":"2026-10-07T12:00:00Z"}\n')
            meta, precapture_dir = prepare(root)
            destination = root / "capture-1"
            result = capture(destination, (("market_data.jsonl", source, "jsonl"),), metadata=meta, precapture_dir=precapture_dir)
            self.assertEqual(result["status"], "SEALED")
            self.assertEqual(verify_sealed_capture(destination)["experiment_id"], "exp-1")
            with self.assertRaises(FileExistsError):
                capture(destination, (("market_data.jsonl", source, "jsonl"),), metadata=meta, precapture_dir=precapture_dir)

    def test_missing_field_wrong_hash_and_v2_safety_cannot_seal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "state.json"; source.write_text("{}")
            meta, precapture_dir = prepare(root)
            patches = ({"schema_fingerprint": None}, {"strategy_v1": {"config_hash": "wrong"}},
                       {"strategy_v2": {"contract_hash": "wrong"}},
                       {"execution_safety": {"v2_execution_enabled": True}},
                       {"execution_safety": {"execution_allowlist": ["CONTEXT_STRUCTURE_RETRACE_V2@V2"]}},
                       {"execution_safety": {"v2_execution_intent_count": 1}},
                       {"execution_safety": {"v2_execution_attempt_count": 1}},
                       {"strategy_v2": {"parameter_hash": "wrong"}})
            for index, patch in enumerate(patches):
                bad = dict(meta)
                changed = metadata(**patch)
                bad.update(changed)
                with self.assertRaises(ValueError):
                    capture(root / f"bad-{index}", (("state.json", source),), metadata=bad, precapture_dir=precapture_dir)

    def test_tampering_and_incomplete_capture_are_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "state.json"; source.write_text("{}")
            meta, precapture_dir = prepare(root)
            destination = root / "capture"; capture(destination, (("state.json", source),), metadata=meta, precapture_dir=precapture_dir)
            (destination / "state.json").write_text('{"tampered":true}')
            with self.assertRaisesRegex(ValueError, "artifact tampering"):
                verify_sealed_capture(destination)
            (destination / "state.json").write_text("{}")
            manifest = destination / "manifest.json"
            manifest.write_text(manifest.read_text(encoding="utf-8").replace('"status": "SEALED"', '"status": "CHANGED"'), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest tampering"):
                verify_sealed_capture(destination)
            incomplete = root / "incomplete"; incomplete.mkdir()
            (incomplete / "capture-status.json").write_text('{"status":"INCOMPLETE"}')
            with self.assertRaisesRegex(ValueError, "INCOMPLETE"):
                verify_sealed_capture(incomplete)

    def test_t0_boundary_is_inclusive_for_forward_records(self):
        records = [{"timestamp": "2026-10-06T23:59:59Z", "reason": "MISSING_SYMBOL_PROVENANCE"},
                   {"timestamp": "2026-10-07T00:00:00Z"}, {"event_time": "2026-10-07T00:00:01Z"}]
        self.assertEqual(records_at_t0(records, "2026-10-07T00:00:00Z"), records[1:])

    def test_authority_mismatch_is_recorded_but_research_capture_is_valid(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "state.json"; source.write_text("{}")
            meta, precapture_dir = prepare(root)
            altered = dict(meta)
            altered["execution_safety"] = {**meta["execution_safety"], "db_authority_state": "ENABLED", "runtime_authority_state": "DISABLED"}
            result = capture(root / "authority-mismatch", (("state.json", source),), metadata=altered, precapture_dir=precapture_dir)
            self.assertEqual(result["execution_safety"]["db_authority_state"], "ENABLED")
            self.assertEqual(verify_sealed_capture(root / "authority-mismatch")["status"], "SEALED")

    def test_post_t0_capture_uses_frozen_deployment_only(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "state.json"; source.write_text("{}")
            meta, precapture_dir = prepare(root)
            (root / "deployment-source.json").unlink()
            result = capture(root / "capture", (("state.json", source),), metadata=meta, precapture_dir=precapture_dir)
            self.assertEqual(result["status"], "SEALED")

    def test_stale_source_cannot_seal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "state.json"; source.write_text("{}")
            meta, precapture_dir = prepare(root)
            stale = dict(meta); stale["source_skew"] = {**meta["source_skew"],
                "state": {**meta["source_skew"]["state"], "offset_from_t0_ms": 30001}}
            with self.assertRaisesRegex(ValueError, "source freshness"):
                capture(root / "stale", (("state.json", source),), metadata=stale, precapture_dir=precapture_dir)


if __name__ == "__main__":
    unittest.main()
