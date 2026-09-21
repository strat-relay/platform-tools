import tempfile
import unittest
from pathlib import Path

from trade_manager import central


class CentralManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.registry = central.OwnershipRegistry(root / "ownership.jsonl")
        self.state = central.BrokerStateStream(root / "broker_state.json")
        self.old_intents, self.old_decisions = central.INTENTS_PATH, central.DECISIONS_PATH
        central.INTENTS_PATH, central.DECISIONS_PATH = root / "intents.jsonl", root / "decisions.jsonl"

    def tearDown(self):
        central.INTENTS_PATH, central.DECISIONS_PATH = self.old_intents, self.old_decisions
        self.tmp.cleanup()

    def execution(self, *, strategy="S", instance="I", ticket="42", symbol="XAUUSDm", intent_id=None):
        intent = {"execution_intent_id": intent_id or f"INT-{ticket}-{strategy}", "strategy_id": strategy,
                  "strategy_instance_id": instance, "signal_id": "SIG-1",
                  "broker_symbol": symbol, "approved_volume": 0.03}
        self.registry.record_real_execution(intent, {"ok": True, "ticket": ticket,
                                                     "position_id": ticket,
                                                     "account_position_mode": "HEDGING"})
        return intent

    def position(self, ticket="42", strategy="S", instance="I", symbol="XAUUSDm"):
        return {"ticket": ticket, "symbol": symbol, "strategy_id": strategy,
                "instance_id": instance, "direction": "LONG", "current_stop": 100.0,
                "current_target": 120.0, "volume": 0.03}

    def proposal(self, position, snapshot=1, action="TRAIL_STOP", stop=105.0):
        return central.ManagementProposal(
            management_proposal_id="MP-1", proposal_version="management-proposal-v1",
            strategy_id=position["strategy_id"], instance_id=position["instance_id"],
            position_identity=position["ticket"], broker_symbol=position["symbol"],
            source_intent_id="INT-1", source_event_id="EV-1", action=action,
            current_position_snapshot_version=snapshot, requested_stop=stop,
            requested_target=None, requested_close_volume=None, reason_code="TEST",
            policy_id="P", policy_version="1", decision_time="2026-01-01T00:00:00+00:00",
            proposal_emitted_at="2026-01-01T00:00:00+00:00", source_state_hash="hash").as_dict()

    def test_owned_position_authorizes_once(self):
        self.execution()
        position = self.position()
        self.state.save(account={"position_mode": "HEDGING"}, positions=[position])
        ok, reason, intent = central.authorize(self.proposal(position), state=self.state.load(), registry=self.registry)
        self.assertTrue(ok); self.assertEqual(reason, "AUTHORIZED"); self.assertEqual(intent["intent_type"], "TRAIL_STOP")
        ok, reason, _ = central.authorize(self.proposal(position), state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertEqual(reason, "DUPLICATE_MANAGEMENT_ACTION")

    def test_unowned_and_wrong_identity_are_blocked(self):
        position = self.position(); self.state.save(account={"position_mode": "HEDGING"}, positions=[position])
        ok, reason, _ = central.authorize(self.proposal(position), state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertEqual(reason, "UNOWNED_POSITION")
        self.execution(strategy="OTHER")
        ok, reason, _ = central.authorize(self.proposal(position), state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertIn(reason, {"UNOWNED_POSITION", "OWNERSHIP_STRATEGY_OR_INSTANCE_MISMATCH"})

    def test_netting_mixed_ownership_is_fail_closed(self):
        self.execution(strategy="BASE", instance="base")
        self.execution(strategy="XAU33", instance="xau33")
        position = self.position(strategy="BASE", instance="base")
        self.state.save(account={"position_mode": "NETTING"}, positions=[position])
        ok, reason, _ = central.authorize(self.proposal(position), state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertEqual(reason, "AMBIGUOUS_NET_POSITION_OWNERSHIP")

    def test_stale_and_risk_increasing_proposals_are_blocked(self):
        self.execution(); position = self.position(); self.state.save(account={"position_mode": "HEDGING"}, positions=[position])
        ok, reason, _ = central.authorize(self.proposal(position, snapshot=0), state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertEqual(reason, "POSITION_STATE_CHANGED")
        bad = self.proposal(position, stop=95.0)
        ok, reason, _ = central.authorize(bad, state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertEqual(reason, "RISK_INCREASING_STOP_CHANGE")

    def test_unsupported_entry_and_excess_close_are_blocked(self):
        self.execution(); position = self.position(); self.state.save(account={"position_mode": "HEDGING"}, positions=[position])
        bad = self.proposal(position, action="OPEN_POSITION")
        ok, reason, _ = central.authorize(bad, state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertEqual(reason, "UNSUPPORTED_MANAGEMENT_ACTION")
        bad = self.proposal(position, action="PARTIAL_CLOSE"); bad["requested_close_volume"] = 0.04
        ok, reason, _ = central.authorize(bad, state=self.state.load(), registry=self.registry)
        self.assertFalse(ok); self.assertEqual(reason, "CLOSE_VOLUME_EXCEEDS_OWNED_VOLUME")

    def test_fake_end_to_end_provenance(self):
        execution = self.execution(strategy="CONTEXT_STRUCTURE_RETRACE_V1", instance="ctx", ticket="77")
        position = self.position(ticket="77", strategy="CONTEXT_STRUCTURE_RETRACE_V1", instance="ctx")
        self.state.save(account={"position_mode": "HEDGING"}, positions=[position])
        proposal = self.proposal(position)
        proposal.update(strategy_id=execution["strategy_id"], instance_id=execution["strategy_instance_id"])
        ok, _, intent = central.authorize(proposal, state=self.state.load(), registry=self.registry)
        self.assertTrue(ok)
        self.assertEqual(intent["management_proposal_id"], proposal["management_proposal_id"])
        self.assertEqual(intent["ownership_id"], next(iter(self.registry.current().values()))["ownership_id"])
        self.assertEqual(intent["strategy_id"], "CONTEXT_STRUCTURE_RETRACE_V1")


if __name__ == "__main__":
    unittest.main()
