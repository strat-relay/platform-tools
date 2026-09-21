import json
import math
import tempfile
import unittest
from pathlib import Path

from execution.demo import virtual_size
from execution.models import ExecutionIntent
from execution.risk_policy import RiskPolicyError, RiskPolicyResolver


BASE = {
    "version": 1,
    "portfolio": {"virtual_equity_usd": 200.0},
    "defaults": {"risk_percent_per_position": 3.0},
    "strategies": {"CONTEXT_STRUCTURE_RETRACE_V1": {"risk_percent_per_position": 3.0}},
}


class RiskPolicyTests(unittest.TestCase):
    def write(self, path: Path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_default_resolution_and_budget(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "risk.json"; self.write(p, BASE)
            resolved = RiskPolicyResolver(p).for_strategy("OTHER")
            self.assertEqual(resolved.source, "DEFAULT")
            self.assertEqual(resolved.risk_budget_usd, 6.0)

    def test_strategy_override(self):
        raw = {**BASE, "strategies": {"X": {"risk_percent_per_position": 5.0}}}
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "risk.json"; self.write(p, raw)
            resolved = RiskPolicyResolver(p).for_strategy("X")
            self.assertEqual(resolved.source, "STRATEGY_OVERRIDE")
            self.assertEqual(resolved.risk_budget_usd, 10.0)

    def test_reload_applies_only_to_next_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "risk.json"; self.write(p, BASE)
            resolver = RiskPolicyResolver(p)
            first = resolver.for_strategy("OTHER")
            changed = {**BASE, "defaults": {"risk_percent_per_position": 4.0}}
            self.write(p, changed)
            second = resolver.for_strategy("OTHER")
            self.assertEqual(first.risk_budget_usd, 6.0)
            self.assertEqual(second.risk_budget_usd, 8.0)

    def test_invalid_policy_fails_closed(self):
        invalid_values = [0, -1, float("nan"), float("inf"), 101]
        for value in invalid_values:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                p = Path(td) / "risk.json"
                raw = {**BASE, "defaults": {"risk_percent_per_position": value}}
                p.write_text(json.dumps(raw, allow_nan=True), encoding="utf-8")
                with self.assertRaises(RiskPolicyError): RiskPolicyResolver(p).for_strategy("OTHER")

    def test_missing_strategy_and_default_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "risk.json"; raw = {**BASE, "defaults": {}}
            self.write(p, raw)
            with self.assertRaises(RiskPolicyError): RiskPolicyResolver(p).for_strategy("OTHER")

    def test_zero_and_negative_override_rejected(self):
        for value in (0, -0.1):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                p = Path(td) / "risk.json"
                self.write(p, {**BASE, "strategies": {"X": {"risk_percent_per_position": value}}})
                with self.assertRaises(RiskPolicyError): RiskPolicyResolver(p).for_strategy("X")

    def test_sizing_uses_budget_without_changing_stop(self):
        metadata = {"tick_size": 1.0, "tick_value": 10.0, "volume_min": 0.01, "volume_max": 10.0, "volume_step": 0.01}
        result = virtual_size(entry=100.0, stop=99.0, metadata=metadata, virtual_equity=200.0, risk_fraction=0.03)
        self.assertEqual(result["desired_risk_amount"], 6.0)
        self.assertEqual(result["risk_fraction"], 0.03)
        self.assertEqual(result["rounded_volume"], 0.6)

    def test_minimum_volume_rejection_remains_fail_closed(self):
        metadata = {"tick_size": 1.0, "tick_value": 730.0, "volume_min": 0.01, "volume_max": 10.0, "volume_step": 0.01}
        result = virtual_size(entry=100.0, stop=99.0, metadata=metadata, virtual_equity=200.0, risk_fraction=0.03)
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reason"], "BELOW_MINIMUM_VOLUME_FOR_RISK_BUDGET")

    def test_intent_persists_policy_snapshot(self):
        signal = {"signal_id": "SIG-RISK", "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
                  "strategy_version": "V1", "direction": "LONG", "symbol": "XAUUSDm",
                  "signal_timestamp": "2026-09-19T00:00:00+00:00"}
        sizing = {"sizing_decision_id": "SIZE-RISK", "entry": 100.0, "stop": 99.0, "target": 101.0,
                  "rounded_volume": 0.01, "desired_risk_amount": 6.0, "desired_risk_fraction": 0.03,
                  "account_snapshot_id": "SNAP", "created_at": "2026-09-19T00:00:00+00:00",
                  "risk_policy_version": 1, "virtual_equity_usd": 200.0, "risk_percent": 3.0,
                  "risk_budget_usd": 6.0}
        intent = ExecutionIntent.from_records(signal, sizing, {"portfolio_id": "p"}, {"account_id": "a"}).to_dict()
        self.assertEqual(intent["risk_policy_version"], 1)
        self.assertEqual(intent["risk_budget_usd"], 6.0)


if __name__ == "__main__":
    unittest.main()
