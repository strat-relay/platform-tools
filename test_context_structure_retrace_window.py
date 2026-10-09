from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research.context_structure_retrace_window import DecisionLedger, report


def row(strategy: str, candidate: str, signal: str | None, *, outcome: str | None = None,
        realized: float | None = None, rejection: str | None = None, planned: float = 1.2) -> dict:
    return {"strategy_id": strategy, "strategy_version": "V1" if strategy.endswith("V1") else "V2",
            "strategy_instance": "phase6", "config_hash": "cfg", "instrument": "GBPUSD",
            "decision_timestamp": "2026-10-06T09:45:00Z", "market_cutoff_timestamp": "2026-10-06T09:45:00Z",
            "market_snapshot_hash": "snap", "candidate_id": candidate, "signal_id": signal,
            "direction": "SHORT", "entry": 1.3, "stop": 1.31, "target": 1.29,
            "risk_distance": 0.01, "reward_distance": planned * 0.01, "planned_r": planned,
            "target_source": "STRUCTURE", "decision": "SIGNAL_CREATED" if signal else "REJECTED",
            "rejection_reason": rejection, "evaluation_id": candidate, "strategy_outcome": outcome,
            "strategy_r": realized, "execution_intent": None, "execution_decision": None,
            "broker_position_state": "NOT_OPENED", "broker_realized_pnl": 0}


class ContextRetraceWindowTests(unittest.TestCase):
    def test_hash_chain_and_report_keep_rejected_denominator(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "decisions.jsonl"
            ledger = DecisionLedger(path, capture_hash="cap", t0="2026-01-01T00:00:00Z")
            ledger.append(row("CONTEXT_STRUCTURE_RETRACE_V1", "c1", "s1", outcome="STOPPED", realized=-1))
            ledger.append(row("CONTEXT_STRUCTURE_RETRACE_V1", "c2", "s2", planned=.15, outcome="TARGET_HIT", realized=.15))
            ledger.append(row("CONTEXT_STRUCTURE_RETRACE_V2", "c1", "s1", outcome="STOPPED", realized=-1))
            ledger.append(row("CONTEXT_STRUCTURE_RETRACE_V2", "c2", None, rejection="RR_BELOW_MINIMUM", planned=.15))
            result = report(path)
            self.assertEqual(result["v1"]["evaluations"], 2)
            self.assertEqual(result["v2"]["rr_rejected"], 1)
            self.assertEqual(result["v1_removed_by_v2"]["signals"], 1)
            self.assertEqual(result["v1_removed_by_v2"]["total_r"], .15)


if __name__ == "__main__":
    unittest.main()
