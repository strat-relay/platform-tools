from pathlib import Path
import tempfile
import unittest

from research.context_structure_retrace_window import DecisionLedger, report


def decision(timestamp, strategy, candidate, signal):
    return {
        "strategy_id": strategy,
        "strategy_version": "V1",
        "strategy_instance": "phase6",
        "config_hash": "config",
        "instrument": "BTCUSDm",
        "decision_timestamp": timestamp,
        "market_cutoff_timestamp": timestamp,
        "market_snapshot_hash": "snapshot",
        "candidate_id": candidate,
        "signal_id": signal,
        "direction": "LONG",
        "entry": 100,
        "stop": 95,
        "target": 105,
        "risk_distance": 5,
        "reward_distance": 5,
        "planned_r": 1.0,
        "target_source": "TEST",
        "decision": "SIGNAL",
        "rejection_reason": None,
        "evaluation_id": f"eval-{candidate}",
        "strategy_outcome": "TARGET_HIT",
        "strategy_r": 1.0,
    }


def test_report_excludes_pre_t0_records(tmp_path: Path):
    path = tmp_path / "ledger.jsonl"
    ledger = DecisionLedger(path, capture_hash="capture", t0="2026-10-07T10:00:00+00:00")
    ledger.append(decision("2026-10-07T09:59:00+00:00", "CONTEXT_STRUCTURE_RETRACE_V1", "old", "old"))
    ledger.append(decision("2026-10-07T10:00:00+00:00", "CONTEXT_STRUCTURE_RETRACE_V2", "new", "new"))

    result = report(path)

    assert result["t0"] == "2026-10-07T10:00:00+00:00"
    assert result["historical_records_excluded"] == 1
    assert result["experiment_records"] == 1
    assert result["v1"]["evaluations"] == 0
    assert result["v2"]["evaluations"] == 1


class ContextRetraceWindowTests(unittest.TestCase):
    def test_report_excludes_pre_t0_records(self):
        with tempfile.TemporaryDirectory() as directory:
            test_report_excludes_pre_t0_records(Path(directory))
