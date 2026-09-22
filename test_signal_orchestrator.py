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
from migration.flags import ExecutionAuthorityMode, SignalAuthorityMode
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

    def quote(self, symbol):
        return {"bid": 99.5, "ask": 100.5, "timestamp": "2026-09-16T00:00:00Z"}


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
        with tempfile.TemporaryDirectory() as td, patch.object(so, "safety_audit", return_value={"pass": True}), patch.object(so, "REAL_CONTEXT", "SYNTHETIC_ACCOUNT"), patch.object(so, "PLATFORM_RUNTIME", Path(td) / "runtime"):
            store = OrchestrationStore(Path(td))
            self.assertTrue(so.startup_audit(self._startup_config("SHADOW"), "SHADOW", store)["pass"])
            self.assertFalse(so.startup_audit(self._startup_config("REAL_EXECUTION"), "SHADOW", store)["pass"])
            self.assertIn("PLATFORM_MODE_NOT_SHADOW", so.startup_audit(self._startup_config("REAL_EXECUTION"), "SHADOW", store)["reasons"])

    def test_real_startup_requires_real_consistent_state(self):
        with tempfile.TemporaryDirectory() as td, patch.object(so, "safety_audit", return_value={"pass": True}), patch.object(so, "PLATFORM_RUNTIME", Path(td) / "runtime"), patch.object(so, "REAL_CONTEXT", "SYNTHETIC_ACCOUNT"):
            store = OrchestrationStore(Path(td) / "orchestration")
            self.assertFalse(so.startup_audit(self._startup_config("SHADOW"), "REAL_EXECUTION", store,
                execution_authority_mode=ExecutionAuthorityMode.ENABLED)["pass"])
            self._real_runtime(td)
            self.assertTrue(so.startup_audit(self._startup_config("REAL_EXECUTION"), "REAL_EXECUTION", store,
                execution_authority_mode=ExecutionAuthorityMode.ENABLED)["pass"])
            broken = self._startup_config("REAL_EXECUTION")
            broken["accounts"][0]["execution_mode"] = "SHADOW"
            result = so.startup_audit(broken, "REAL_EXECUTION", store,
                execution_authority_mode=ExecutionAuthorityMode.ENABLED)
            self.assertFalse(result["pass"])
            self.assertIn("ENABLED_ACCOUNT_NOT_REAL", result["reasons"])

    def test_primary_startup_requires_independent_canonical_and_execution_modes(self):
        valid_env = {
            "ORCHESTRATOR_MODE": "PRIMARY",
            "SIGNAL_AUTHORITY_MODE": "DB_PRIMARY",
            "SIGNAL_DB_PRIMARY_ENABLED": "true",
            "SIGNAL_JETSTREAM_PRIMARY_ENABLED": "true",
            "EXECUTION_AUTHORITY_MODE": "DISABLED",
            "TRADING_POSTGRES_DSN": "postgresql://writer:secret@db.example/trading",
            "NATS_URL": "nats://nats.example:4222",
            "SIGNAL_CUTOFF_ID": "persisted-cutoff",
            "SIGNAL_CUTOFF_UTC": "2026-09-22T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(so, "safety_audit", return_value={"pass": True}), \
             patch.dict("os.environ", valid_env, clear=True):
            store = OrchestrationStore(Path(td) / "orchestration")
            self.assertTrue(so.startup_audit(self._startup_config("REAL_EXECUTION"), "PRIMARY", store)["pass"])

            invalid_cases = [
                ({"SIGNAL_AUTHORITY_MODE": "LEGACY_FILE", "SIGNAL_DB_PRIMARY_ENABLED": "false",
                  "SIGNAL_JETSTREAM_PRIMARY_ENABLED": "false"}, "PRIMARY_REQUIRES_DB_PRIMARY_SIGNAL_AUTHORITY"),
                ({"TRADING_POSTGRES_DSN": ""}, "CANONICAL_POSTGRES_NOT_CONFIGURED"),
                ({"SIGNAL_CUTOFF_ID": ""}, "SIGNAL_CUTOFF_ID_MISSING"),
                ({"SIGNAL_CUTOFF_UTC": ""}, "SIGNAL_CUTOFF_UTC_MISSING"),
                ({"SIGNAL_CUTOFF_UTC": "2026-09-22T00:00:00"}, "SIGNAL_CUTOFF_UTC_MUST_INCLUDE_TIMEZONE"),
                ({"NATS_URL": ""}, "CANONICAL_NATS_NOT_CONFIGURED"),
                ({"EXECUTION_AUTHORITY_MODE": "ENABLED"}, "PRIMARY_REQUIRES_EXECUTION_DISABLED"),
                ({"EXECUTION_AUTHORITY_MODE": ""}, "INVALID_EXECUTION_AUTHORITY"),
                ({"ORCHESTRATOR_MODE": ""}, "ORCHESTRATOR_MODE_MUST_EXPLICITLY_BE_PRIMARY"),
            ]
            for overrides, expected_reason in invalid_cases:
                with self.subTest(expected_reason=expected_reason):
                    env = {**valid_env, **overrides}
                    with patch.dict("os.environ", env, clear=True):
                        result = so.startup_audit(self._startup_config("REAL_EXECUTION"), "PRIMARY", store)
                    self.assertFalse(result["pass"])
                    self.assertTrue(any(reason.startswith(expected_reason) for reason in result["reasons"]))

    def test_shadow_rejects_db_primary_to_prevent_dual_authority(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(so, "safety_audit", return_value={"pass": True}), \
             patch.dict("os.environ", {
                 "SIGNAL_AUTHORITY_MODE": "DB_PRIMARY",
                 "SIGNAL_DB_PRIMARY_ENABLED": "true",
                 "SIGNAL_JETSTREAM_PRIMARY_ENABLED": "true",
                 "EXECUTION_AUTHORITY_MODE": "DISABLED",
             }, clear=True):
            result = so.startup_audit(self._startup_config("SHADOW"), "SHADOW",
                                      OrchestrationStore(Path(td) / "orchestration"))
            self.assertFalse(result["pass"])
            self.assertIn("SHADOW_CANNOT_USE_DB_PRIMARY_SIGNAL_AUTHORITY", result["reasons"])

    def test_primary_account_routes_are_inert_without_broker_provider(self):
        with tempfile.TemporaryDirectory() as td:
            store = OrchestrationStore(Path(td) / "orchestration")
            cfg = self._startup_config("REAL_EXECUTION")
            cfg["portfolios"][0]["strategy_ids"] = ["TEST"]
            class ForbiddenProvider:
                def __getattr__(self, name):
                    raise AssertionError(f"PRIMARY attempted broker access: {name}")
            so.route_signal(store, signal(), cfg, ForbiddenProvider(), "PRIMARY")
            execution_routes = [row for row in store.rows("route_decisions")
                                if row.get("route_type") == "EXECUTION_DISABLED"]
            self.assertEqual(len(execution_routes), 1)
            self.assertEqual(execution_routes[0]["status"], "DISABLED")
            self.assertEqual(execution_routes[0]["reason"], "EXECUTION_AUTHORITY_DISABLED")

    def test_primary_poll_uses_canonical_publisher_without_provider_or_signal_file(self):
        class Adapter:
            def discover_new_signals(self, seen):
                return [signal()]
        class Publisher:
            def __init__(self): self.published = []
            def existing_signal_ids(self): return set()
            def publish(self, value): self.published.append(value); return (value, True)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = OrchestrationStore(root / "orchestration")
            publisher = Publisher()
            with patch.object(so, "load_adapters", return_value=[Adapter()]), \
                 patch.object(so, "MT5ShadowProvider", side_effect=AssertionError("PRIMARY created MT5 provider")), \
                 patch.object(so, "HEARTBEAT", root / "heartbeat.json"):
                count = so.poll_once(store, {}, {"freeze_timestamp": "2026-09-22T00:00:00Z"},
                    "PRIMARY", signal_authority_mode=SignalAuthorityMode.DB_PRIMARY,
                    canonical_publisher=publisher)
            self.assertEqual(count, 1)
            self.assertEqual(len(publisher.published), 1)
            self.assertFalse((root / "orchestration" / "signals.jsonl").exists())

    def test_real_route_is_disposition_only_and_keeps_broker_writes_outside_orchestrator(self):
        with tempfile.TemporaryDirectory() as td:
            store = OrchestrationStore(Path(td))
            cfg = self._startup_config("REAL_EXECUTION")
            cfg["portfolios"][0]["strategy_ids"] = ["TEST"]
            so.route_signal(store, signal(), cfg, FakeProvider(), "REAL_EXECUTION")
            routes = store.rows("route_decisions")
            self.assertTrue(any(row["route_type"] == "REAL_EXECUTION_DISPOSITION" and row["status"] == "QUEUED" for row in routes))
            self.assertTrue(so.safety_audit()["pass"])

    def test_od01_tradeability_stream_is_persisted_instead_of_false_missing_account_data(self):
        with tempfile.TemporaryDirectory() as td:
            store = OrchestrationStore(Path(td))
            cfg = self._startup_config("REAL_EXECUTION")
            cfg["portfolios"][0]["strategy_ids"] = ["TEST"]
            so.route_signal(store, signal(), cfg, FakeProvider(), "REAL_EXECUTION")
            self.assertEqual(len(store.rows("tradeability_decisions")), 1)
            self.assertFalse(any(row.get("error") == "'tradeability_decisions'" for row in store.rows("sizing_decisions")))

    def test_legitimate_missing_account_data_is_still_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            store = OrchestrationStore(Path(td))
            cfg = self._startup_config("REAL_EXECUTION")
            cfg["portfolios"][0]["strategy_ids"] = ["TEST"]
            class MissingAccount(FakeProvider):
                def account_snapshot(self, account_id):
                    raise RuntimeError("account snapshot unavailable")
            so.route_signal(store, signal(), cfg, MissingAccount(), "REAL_EXECUTION")
            self.assertEqual(store.rows("sizing_decisions")[0]["reason"], "MISSING_ACCOUNT_DATA")

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
        with tempfile.TemporaryDirectory() as td, patch.object(so, "ROOT", Path(td)), patch.object(so, "PLATFORM_RUNTIME", Path(td) / "runtime"), patch.object(so, "REAL_CONTEXT", "SYNTHETIC_ACCOUNT"):
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
