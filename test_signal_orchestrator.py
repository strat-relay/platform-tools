import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestration.models import StrategySignal
from orchestration.risk import RiskSizingEngine
from orchestration.brokers.mt5_shadow import BridgeQueueBacklog, MT5ShadowProvider
from orchestration.storage import OrchestrationStore
from orchestration.config import DEFAULT_CONFIG
import signal_orchestrator as so


def signal(direction="LONG"):
    return StrategySignal("sig-1", "strategy-signal-v1", "TEST", "V1", "i1", "event-1", "m1", "s1", "o1", "p1",
        "2026-09-16T00:00:00Z", "2026-09-16T00:00:00Z", "XAUUSDm", "XAUUSD", "XAUUSDm", direction,
        "MARKET", 100.0, 99.0 if direction == "LONG" else 101.0, 101.0 if direction == "LONG" else 99.0,
        1.0, 1.0, 1.0, "M15", "M5", ("H1", "H4"), ("DEPTH_ONLY",), {}, {})


class FakeProvider:
    def account_snapshot(self, account_id):
        return {"account_id": account_id, "timestamp": "2026-09-16T00:00:00Z", "balance": 200.0, "equity": 200.0,
                "margin": 0.0, "free_margin": 200.0, "margin_level": None, "currency": "USD", "leverage": 100}

    def symbol_metadata(self, symbol):
        return {"broker_symbol": symbol, "tick_size": 1.0, "tick_value": 1.0, "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01}


class OrchestratorTests(unittest.TestCase):
    def _startup_config(self, mode="SHADOW"):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["execution_mode"] = mode
        cfg["execution_mcp_url"] = so.EXECUTION_ENDPOINT
        cfg["execution_transport_verified"] = True
        account = cfg["accounts"][0]
        account["execution_mode"] = mode
        account["broker_environment"] = "REAL" if mode == "REAL_EXECUTION" else "DEMO"
        return cfg

    def _real_runtime(self, root):
        orchestration = Path(root) / "orchestration"
        execution = Path(root) / "execution"
        orchestration.mkdir(parents=True, exist_ok=True)
        execution.mkdir(parents=True, exist_ok=True)
        (orchestration / "manifest.json").write_text(json.dumps({"mode": "REAL_EXECUTION", "live_execution_enabled": True}))
        (orchestration / "state.json").write_text(json.dumps({
            "live_execution_enabled": True,
            "live_execution_cutoff_timestamp": "2026-09-16T20:00:00Z",
        }))
        (execution / "real_state.json").write_text(json.dumps({
            "armed": True, "mode": "REAL_EXECUTION", "account_context_id": so.REAL_CONTEXT,
        }))
        (execution / "real_execution_resume.json").write_text(json.dumps({
            "real_execution_resumed_at": "2026-09-17T09:51:12Z", "real_execution_resume_generation": 1,
        }))

    def test_shadow_startup_accepts_shadow_and_rejects_real(self):
        with tempfile.TemporaryDirectory() as td, patch.object(so, "safety_audit", return_value={"pass": True}):
            store = OrchestrationStore(Path(td))
            self.assertTrue(so.startup_audit(self._startup_config("SHADOW"), "SHADOW", store)["pass"])
            self.assertFalse(so.startup_audit(self._startup_config("REAL_EXECUTION"), "SHADOW", store)["pass"])
            self.assertIn("PLATFORM_MODE_NOT_SHADOW", so.startup_audit(self._startup_config("REAL_EXECUTION"), "SHADOW", store)["reasons"])

    def test_real_startup_requires_real_consistent_state(self):
        with tempfile.TemporaryDirectory() as td, patch.object(so, "safety_audit", return_value={"pass": True}):
            store = OrchestrationStore(Path(td) / "orchestration")
            self.assertFalse(so.startup_audit(self._startup_config("SHADOW"), "REAL_EXECUTION", store)["pass"])
            self._real_runtime(td)
            self.assertTrue(so.startup_audit(self._startup_config("REAL_EXECUTION"), "REAL_EXECUTION", store)["pass"])
            broken = self._startup_config("REAL_EXECUTION")
            broken["accounts"][0]["execution_mode"] = "SHADOW"
            result = so.startup_audit(broken, "REAL_EXECUTION", store)
            self.assertFalse(result["pass"])
            self.assertIn("ENABLED_ACCOUNT_NOT_REAL", result["reasons"])

    def test_real_route_is_disposition_only_and_keeps_broker_writes_outside_orchestrator(self):
        with tempfile.TemporaryDirectory() as td:
            store = OrchestrationStore(Path(td))
            cfg = self._startup_config("REAL_EXECUTION")
            cfg["portfolios"][0]["strategy_ids"] = ["TEST"]
            so.route_signal(store, signal(), cfg, FakeProvider(), "REAL_EXECUTION")
            routes = store.rows("route_decisions")
            self.assertTrue(any(row["route_type"] == "REAL_EXECUTION_DISPOSITION" and row["status"] == "QUEUED" for row in routes))
            self.assertTrue(so.safety_audit()["pass"])

    def test_signal_identity_is_deterministic(self):
        self.assertEqual(so.stable_id("SIG", {"a": 1}), so.stable_id("SIG", {"a": 1}))

    def test_discovery_time_cannot_make_old_event_prospective(self):
        self.assertEqual(so.classify_source_timestamp("2026-09-16T10:55:00+00:00", "2026-09-16T11:29:02.633086+00:00"), "PRE_ORCHESTRATOR_REFERENCE")
        self.assertEqual(so.classify_source_timestamp("2026-09-16T11:29:02.633086+00:00", "2026-09-16T11:29:02.633086+00:00"), "PROSPECTIVE_ORCHESTRATOR_SIGNAL")

    def test_live_cutoff_uses_creation_provenance_and_id_set(self):
        state = {"live_execution_enabled": True,
                 "live_execution_cutoff_timestamp": "2026-09-16T12:00:00+00:00",
                 "live_execution_cutoff_signal_ids": ["old-id"]}
        old_discovered_late = {"signal_id": "old-id", "created_at": "2026-09-16T12:01:00+00:00", "signal_timestamp": "2026-09-16T11:00:00+00:00"}
        old_created = {"signal_id": "old-created", "created_at": "2026-09-16T11:59:59+00:00", "signal_timestamp": "2026-09-16T11:59:00+00:00"}
        new_signal = {"signal_id": "new-id", "created_at": "2026-09-16T12:00:01+00:00", "signal_timestamp": "2026-09-16T12:00:01+00:00"}
        self.assertEqual(so.live_classification(old_discovered_late, state), "PRE_LIVE_EXECUTION")
        self.assertEqual(so.live_classification(old_created, state), "PRE_LIVE_EXECUTION")
        self.assertEqual(so.live_classification(new_signal, state), "POST_LIVE_EXECUTION")

    def test_live_enable_captures_existing_high_water_mark_without_broker_write(self):
        with tempfile.TemporaryDirectory() as td, patch.object(so, "ROOT", Path(td)):
            root = Path(td) / "runtime" / "orchestration"
            store = OrchestrationStore(root)
            for index in range(45):
                store.append("signals", {"signal_id": f"sig-{index}", "created_at": "2026-09-16T11:00:00+00:00"}, f"sig-{index}")
            execution = Path(td) / "runtime" / "execution"
            execution.mkdir(parents=True)
            (execution / "real_state.json").write_text(json.dumps({"armed": True, "account_context_id": so.REAL_CONTEXT}))
            config = {"execution_mcp_url": so.EXECUTION_ENDPOINT, "execution_transport_verified": True}
            self.assertEqual(so.live_enable(store, config), 0)
            state = store.load_state()
            self.assertTrue(state["live_execution_enabled"])
            self.assertEqual(len(state["live_execution_cutoff_signal_ids"]), 45)
            self.assertEqual(so.live_classification({"signal_id": "sig-0", "created_at": "2099-01-01T00:00:00+00:00"}, state), "PRE_LIVE_EXECUTION")

    def test_long_sizing(self):
        s = signal(); meta = FakeProvider().symbol_metadata("XAUUSDm")
        d = RiskSizingEngine().size(s, "p", "a", {"snapshot_id": "snap", "equity": 200, "free_margin": 200}, meta, .01)
        self.assertEqual(d.decision, "EXECUTABLE"); self.assertEqual(d.desired_risk_amount, 2.0)

    def test_short_sizing_is_directional(self):
        s = signal("SHORT"); meta = FakeProvider().symbol_metadata("XAUUSDm")
        d = RiskSizingEngine().size(s, "p", "a", {"snapshot_id": "snap", "equity": 200, "free_margin": 200}, meta, .01)
        self.assertEqual(d.decision, "EXECUTABLE")

    def test_equity_changes_size(self):
        e = RiskSizingEngine(); meta = FakeProvider().symbol_metadata("XAUUSDm")
        a = e.size(signal(), "p", "a", {"snapshot_id": "1", "equity": 200}, meta, .01)
        b = e.size(signal(), "p", "a", {"snapshot_id": "2", "equity": 215}, meta, .01)
        self.assertGreater(b.raw_volume, a.raw_volume)

    def test_below_minimum_is_rejected(self):
        meta = FakeProvider().symbol_metadata("XAUUSDm"); meta["volume_min"] = 10
        d = RiskSizingEngine().size(signal(), "p", "a", {"snapshot_id": "1", "equity": 200}, meta, .0025)
        self.assertEqual(d.reason, "BELOW_MINIMUM_VOLUME")

    def test_volume_rounding_does_not_exceed_budget(self):
        meta = FakeProvider().symbol_metadata("XAUUSDm")
        meta["tick_value"] = 100.0
        d = RiskSizingEngine().size(signal(), "p", "a", {"snapshot_id": "1", "equity": 200}, meta, .0135)
        self.assertLessEqual(d.estimated_loss_at_stop, d.desired_risk_amount)
        self.assertEqual(d.rounded_volume, 0.02)

    def test_invalid_stop_and_missing_metadata(self):
        bad = signal(); bad = StrategySignal(**{**bad.to_dict(), "stop_price": 101.0})
        d = RiskSizingEngine().size(bad, "p", "a", {"snapshot_id": "1", "equity": 200}, FakeProvider().symbol_metadata("x"), .01)
        self.assertEqual(d.reason, "INVALID_STOP_GEOMETRY")
        self.assertEqual(RiskSizingEngine().size(signal(), "p", "a", {"snapshot_id": "1", "equity": 200}, {}, .01).reason, "MISSING_SYMBOL_METADATA")

    def test_distribution_is_independent_of_sizing(self):
        with tempfile.TemporaryDirectory() as td:
            store = OrchestrationStore(Path(td)); cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg["portfolios"][0]["strategy_ids"] = ["TEST"]
            with patch.object(so, "OrchestrationStore", return_value=store):
                so.route_signal(store, signal(), cfg, FakeProvider())
            self.assertEqual(len(store.rows("distribution_queue")), 1)
            self.assertGreater(len(store.rows("sizing_decisions")), 0)
            for row in store.rows("sizing_decisions"):
                row["decision"] = "REJECTED"
            self.assertEqual(len(store.rows("distribution_queue")), 1)

    def test_safety_audit_disallows_no_writes(self):
        audit = so.safety_audit()
        self.assertTrue(audit["pass"])
        self.assertEqual(audit["live_execution_enabled"], bool(so.OrchestrationStore(so.RUNTIME).load_state().get("live_execution_enabled", False)))

    def test_bridge_backlog_is_refused_before_queuing_another_read(self):
        provider = MT5ShadowProvider("http://unused")
        with patch.object(provider, "health", return_value={"pending": 3}):
            with self.assertRaises(BridgeQueueBacklog):
                provider.account_snapshot("a")

    def test_account_snapshot_cache_reuses_fresh_snapshot(self):
        provider = MT5ShadowProvider("http://unused")
        with patch.object(provider, "health", return_value={"pending": 0}), patch.object(provider, "_read", return_value={"equity": 200}) as read:
            first = provider.account_snapshot("a")
            second = provider.account_snapshot("a")
        self.assertEqual(read.call_count, 1)
        self.assertTrue(second["snapshot_cache_hit"])

    def test_store_append_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            store = OrchestrationStore(Path(td)); row = {"event_id": "x"}
            self.assertTrue(store.append("events", row, "x")); self.assertFalse(store.append("events", row, "x"))


if __name__ == "__main__": unittest.main()
