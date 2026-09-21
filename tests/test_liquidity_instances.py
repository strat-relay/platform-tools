import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from execution.demo import REAL_MAPPINGS
from execution.risk_policy import risk_policy_for
from live_execution_consumer import _trusted_signal_emission_time
from orchestration.liquidity_instances import (
    DEFINITIONS_BY_ID,
    LIQUIDITY_INSTANCE_DEFINITIONS,
    LiquidityInstanceAdapter,
    LiquidityLivePublisher,
)


class LiquidityInstancePlumbingTests(unittest.TestCase):
    def test_four_instances_are_unique_and_disabled_in_plan(self):
        ids = [x.strategy_id for x in LIQUIDITY_INSTANCE_DEFINITIONS]
        instances = [x.instance_id for x in LIQUIDITY_INSTANCE_DEFINITIONS]
        publishers = [x.publisher_id for x in LIQUIDITY_INSTANCE_DEFINITIONS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(instances), len(set(instances)))
        self.assertEqual(len(publishers), len(set(publishers)))
        plan = json.loads(Path("orchestration/config/liquidity_variant_registration_plan.json").read_text())
        self.assertFalse(plan["enabled"])
        self.assertTrue(all(not x["enabled"] and not x["real_execution_enabled"] for x in plan["variants"]))

    def test_state_paths_are_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for definition in LIQUIDITY_INSTANCE_DEFINITIONS:
                (root / definition.state_path).write_text(json.dumps({"signals": {definition.instance_id: {"setup_id": definition.instance_id}}}))
            for definition in LIQUIDITY_INSTANCE_DEFINITIONS:
                adapter = LiquidityInstanceAdapter(root, definition)
                self.assertEqual(set(adapter.load_state()["signals"]), {definition.instance_id})

    def test_cross_variant_event_identity_isolated(self):
        base = DEFINITIONS_BY_ID["LIQUIDITY_DISPLACEMENT_SCALP_V1"]
        xau = DEFINITIONS_BY_ID["LIQUIDITY_DISPLACEMENT_SCALP_XAUUSD_33_V1"]
        self.assertNotEqual(base.source_event_id("S1", "2026-09-19T00:00:00Z"), xau.source_event_id("S1", "2026-09-19T00:00:00Z"))
        self.assertNotEqual(base.dedupe_key("S1", "2026-09-19T00:00:00Z"), xau.dedupe_key("S1", "2026-09-19T00:00:00Z"))

    def test_startup_baseline_and_same_instance_duplicate_guard(self):
        definition = replace(DEFINITIONS_BY_ID["LIQUIDITY_DISPLACEMENT_SCALP_BTCUSD_25_V1"], real_execution_enabled=True)
        publisher = LiquidityLivePublisher(definition, mode="REAL_ELIGIBLE", baseline_setup_ids={"OLD"})
        row = {"setup_id": "OLD", "event_time": "2026-09-19T00:00:00Z", "direction": "LONG", "entry": 100.0, "stop": 99.0, "target": 101.25, "source_health": True, "gap_recovery": False}
        self.assertIsNone(publisher.publish(row, emitted_at="2026-09-19T00:01:00Z"))
        row["setup_id"] = "NEW"
        row["source_event_id"] = definition.source_event_id("NEW", row["event_time"])
        first = publisher.publish(row, emitted_at="2026-09-19T00:01:00Z")
        self.assertIsNotNone(first)
        self.assertIsNone(publisher.publish(row, emitted_at="2026-09-19T00:01:01Z"))

    def test_signal_preserves_event_time_and_requires_explicit_emission(self):
        definition = replace(DEFINITIONS_BY_ID["LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1"], real_execution_enabled=True)
        publisher = LiquidityLivePublisher(definition, mode="REAL_ELIGIBLE")
        row = {"setup_id": "NEW", "event_time": "2026-09-19T00:00:00Z", "direction": "SHORT", "entry": 150.0, "stop": 151.0, "target": 148.75, "source_health": True, "gap_recovery": False, "source_event_id": definition.source_event_id("NEW", "2026-09-19T00:00:00Z")}
        signal = publisher.publish(row, emitted_at="2026-09-19T00:00:02Z")
        self.assertEqual(signal.signal_timestamp, "2026-09-19T00:00:00Z")
        self.assertEqual(signal.decision_time, "2026-09-19T00:00:02Z")
        self.assertEqual(signal.signal_emitted_at, "2026-09-19T00:00:02Z")
        self.assertIsNotNone(_trusted_signal_emission_time(signal.to_dict()))
        with self.assertRaises(ValueError):
            publisher.publish({**row, "setup_id": "NEW2", "source_event_id": definition.source_event_id("NEW2", row["event_time"])})

    def test_explicit_risk_policy_and_real_symbol_mapping(self):
        for definition in LIQUIDITY_INSTANCE_DEFINITIONS:
            policy = risk_policy_for(definition.strategy_id)
            self.assertEqual(policy.source, "STRATEGY_OVERRIDE")
            self.assertEqual(policy.risk_percent, 3.0)
            self.assertEqual(policy.risk_budget_usd, 6.0)
            self.assertEqual(REAL_MAPPINGS[definition.symbol]["status"], "NATIVE")
            self.assertEqual(REAL_MAPPINGS[definition.symbol]["broker_symbol"], definition.broker_symbol)


if __name__ == "__main__":
    unittest.main()
