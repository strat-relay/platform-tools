from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from platform_api.control import PlatformControlApi, PlatformControlRepository
from platform_api.signals import CanonicalSourceUnavailable, UnifiedPlatformApi


class FakeStrategyMgmtApi:
    """Minimal strategy_mgmt_api stub for registry-fallback path tests."""

    def __init__(self, rows=None):
        self._rows = rows or []

    def registry_rows(self):
        return list(self._rows)

    def handle(self, method, path, body):
        return None

    def get_instance_instruments(self, *_args):
        return None

    def sync_instance_instrument(self, *_args):
        return False


def _v3_registry_row(instance_uuid="e5f7a9b1-c3d5-4e7f-a1b3-5c7e9f1b3d5e") -> dict:
    return {
        "definition_id": "7f3a9c10-b4e2-4d8a-9c15-2e6f8b3a1d05",
        "name": "Kojo Structure Reclaim",
        "family_key": "KOJO",
        "description": "V3 test row",
        "version_id": "a2b4c6d8-e0f2-4a6c-8e0a-2c4e6f8a0b2d",
        "version_label": "V3",
        "evaluator_key": "kojo_structure_reclaim_v3",
        "lifecycle": "IMPLEMENTED",
        "instance_id": instance_uuid,
        "instance_display_name": "Kojo V3 Forward",
        "online": True,
        "execution_eligible": False,
        "instruments": [{"canonical_instrument": "XAUUSDm"}],
        "attributes": {},
        "parameter_set_id": "kojo-v3-default",
        "parameter_fingerprint": "abc",
        "parameter_values": {},
    }


class FakeStrategyCatalog:
    """Stands in for platform_api.strategy_catalog.StrategyCatalogRepository (migration 028)."""

    ROWS = [{"strategy_id": "S", "strategy_version": "V1", "enabled": True, "adapter": "Adapter",
             "routes": {"audit": True}},
            {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1", "enabled": True,
             "adapter": "Context", "routes": {"audit": True}},
            {"strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "strategy_version": "V1", "enabled": True,
             "adapter": "Liquidity", "routes": {"audit": True}}]

    def __init__(self, *, unavailable: bool = False):
        self.unavailable = unavailable

    def _ok(self):
        if self.unavailable:
            raise CanonicalSourceUnavailable("test strategy catalog unavailable")

    def list_strategies(self):
        self._ok()
        return [dict(r) for r in self.ROWS]

    def strategy_page(self, strategy_id):
        self._ok()
        return next((dict(r) for r in self.ROWS if r["strategy_id"] == strategy_id), None)

    def strategy_instances(self, strategy_id):
        self._ok()
        return [{"instance_id": "phase6", "strategy_id": strategy_id,
                 "display_name": "test instance", "enabled": False}]

    def instance_page(self, strategy_id, instance_id):
        self._ok()
        if strategy_id == "CONTEXT_STRUCTURE_RETRACE_V1" and instance_id == "phase6":
            return {"instance_id": instance_id, "strategy_id": strategy_id}
        if strategy_id == "LIQUIDITY_DISPLACEMENT_SCALP_V1" and instance_id == "liquidity-btc25":
            return {"instance_id": instance_id, "strategy_id": strategy_id}
        return None


class FakeRepository:
    def __init__(self, *, unavailable: bool = False):
        self.unavailable = unavailable

    def _ok(self):
        if self.unavailable:
            raise CanonicalSourceUnavailable("test source unavailable")

    def platform_status(self, *, include_event_counts=True):
        self._ok()
        return {"outbox_count": 18, "inbox_count": 0, "orchestrator_running": 0}

    def execution_runtime_status(self):
        return {
            "instance_id": "execution-v2-test",
            "worker_status": "HEALTHY",
            "execution_authority_mode": "DISABLED",
            "account_id": "188428665",
            "risk_policy": {"enabled": False, "canary_max_new_executions": 1},
            "canary": {"max_new_executions": 1, "consumed": 1, "remaining": 0},
            "execution_bridge": {"status": "HEALTHY"},
            "broker_account": {"status": "CONNECTED", "account": "******8665", "currency": "USD"},
        }

    def events(self, limit, offset, event_id=None):
        self._ok()
        rows = [{"event_id": "evt-1", "event_type": "signal.entry.created.v1",
                 "aggregate_type": "signal", "aggregate_id": "sig-1", "schema_version": "1",
                 "payload": {"signal_id": "sig-1", "strategy_id": "S"},
                 "occurred_at": "2026-09-22T00:00:00Z", "correlation_id": None, "causation_id": None}]
        return [r for r in rows if event_id is None or r["event_id"] == event_id]

    def executions(self, limit, offset, intent_id=None):
        self._ok()
        return []

    def execution_metrics(self):
        self._ok()
        return {"execution_intents": 0, "broker_capable_requests_attempted": 0,
                "mt5_order_send_attempted": 0, "broker_orders_accepted": 0,
                "broker_fills_observed": 0, "rejected": 0, "blocked": 0}

    def context_entry_outcome_report(self, instance_id=None):
        self._ok()
        return {"found": True, "report": {"outcome_authority": "canonical_postgres",
                                            "outcome_type": "ENTRY_ONLY",
                                            "identity": {"strategy_instance_id": instance_id,
                                                         "observed_at": "2026-10-04T00:00:00Z"},
                                            "performance": {"trades": 10, "open": 1}}}

    def liquidity_entry_outcome_report(self, instance_id=None):
        self._ok()
        return {"found": True, "report": {"outcome_authority": "canonical_postgres",
                                            "outcome_type": "LIQUIDITY_ENTRY",
                                            "identity": {"strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
                                                         "strategy_instance_id": instance_id},
                                            "performance": {"trades": 10, "open": 0}}}

    def trade_manager_summary(self):
        self._ok()
        return {"total_managed_trades": 0, "open_managed_trades": 0,
                "latest_observation_at": None, "latest_decision_at": None,
                "observation_count": 0, "decision_count": 0,
                "published_decision_count": 0, "withheld_decision_count": 0,
                "policy_versions": []}

    def managed_trades(self, limit, offset, trade_id=None):
        self._ok()
        rows = [{"managed_trade_id": "MT_1", "entry_signal_id": "SIG_1",
                 "strategy_id": "S", "instrument": "XAUUSD", "direction": "LONG",
                 "state": "OPEN"}]
        return [row for row in rows if trade_id is None or row["managed_trade_id"] == trade_id]

    def trade_decisions(self, trade_id):
        self._ok()
        return []


class FakeReadOnlyBridge:
    def __init__(self, value=None, error=None):
        self.value = value if value is not None else {"account": "188428665", "balance": 1000}
        self.error = error
        self.calls = []

    def call(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        if self.error:
            raise RuntimeError(self.error)
        return self.value



class PlatformControlApiTests(unittest.TestCase):
    def make_api(self, *, env=None, unavailable=False, bridge_reader=None):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        config = Path(td.name) / "platform.json"
        config.write_text(json.dumps({"strategies": [
            {"strategy_id": "S", "strategy_version": "V1", "enabled": True,
             "adapter": "Adapter", "routes": {"audit": True}},
            {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1",
             "enabled": True, "adapter": "Context", "routes": {"audit": True}},
        ]}))
        authority = {"ORCHESTRATOR_MODE": "PRIMARY", "SIGNAL_AUTHORITY_MODE": "DB_PRIMARY",
                     "EXECUTION_AUTHORITY_MODE": "DISABLED", "SIGNAL_DB_PRIMARY_ENABLED": "true"}
        authority.update(env or {})
        api = PlatformControlApi(FakeRepository(unavailable=unavailable), authority, str(config),
                                 bridge_reader=bridge_reader)
        api.strategy_catalog = FakeStrategyCatalog(unavailable=unavailable)
        return api

    def test_runtime_projection_preserves_lifecycle_status_for_arm_preflight(self):
        repository = PlatformControlRepository()
        repository.query = lambda sql, params=(): [{
            "instance_id": "execution-v2-live",
            "status": "RUNNING",
            "last_heartbeat_at": "2026-09-25T00:00:00Z",
            "metadata": {
                "execution_bridge": {"status": "HEALTHY"},
                "broker_account": {"status": "CONNECTED"},
                "account_id": "188428665",
                "risk_policy": {"enabled": True},
            },
        }]
        projection = repository.execution_runtime_status()
        self.assertEqual(projection["status"], "RUNNING")
        self.assertEqual(projection["worker_status"], "HEALTHY")

    def test_system_reports_canonical_modes_and_inactive_execution(self):
        status, body = self.make_api().execute("GET", "/api/v1/system")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["orchestrator_mode"], "PRIMARY")
        self.assertEqual(body["data"]["signal_authority_mode"], "DB_PRIMARY")
        self.assertEqual(body["data"]["execution_authority_mode"], "DISABLED")
        self.assertEqual(body["data"]["components"]["execution"]["status"], "INACTIVE")
        self.assertEqual(body["data"]["components"]["orchestrator"]["status"], "UNKNOWN")

    def test_system_core_view_defers_expensive_observability_counts(self):
        status, body = self.make_api().execute("GET", "/api/v1/system?view=core")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["components"]["trade_manager"]["status"], "DEFERRED")

    def test_system_projects_effective_v2_runtime_state_not_api_pod_environment(self):
        api = self.make_api(env={"EXECUTION_AUTHORITY_MODE": "DISABLED"})
        api.repository.execution_runtime_status = lambda: {
            "instance_id": "execution-v2-live",
            "worker_status": "HEALTHY",
            "execution_authority_mode": "ENABLED",
            "account_id": "188428665",
            "risk_policy": {
                "enabled": True, "risk_per_trade": 0.005, "max_volume": 0.01,
                "max_signal_age_seconds": 60, "max_daily_loss": 10,
                "max_concurrent_positions": 1, "max_concurrent_orders": 1,
                "allowed_accounts": ["188428665"],
                "allowed_strategies": ["CONTEXT_STRUCTURE_RETRACE_V1@V1"],
                "allowed_symbols": ["XAUUSD", "BTCUSD", "USDJPY", "EURUSD"],
                "canary_max_new_executions": 1,
            },
            "canary": {"max_new_executions": 1, "consumed": 0, "remaining": 1},
            "execution_bridge": {"status": "HEALTHY"},
            "broker_account": {"status": "CONNECTED", "account": "******8665", "currency": "USD"},
        }
        with patch("execution_v2.authority_store.read_authority", return_value={"state": "ENABLED", "revision": 2, "source": "POSTGRES"}):
            status, body = api.execute("GET", "/api/v1/system")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["execution_authority_mode"], "ENABLED")
        self.assertEqual(body["data"]["components"]["execution"]["risk_policy"]["max_volume"], 0.01)
        self.assertEqual(body["data"]["components"]["execution"]["canary"]["remaining"], 1)

    def test_safety_projects_armed_v2_state_without_reporting_disabled(self):
        api = self.make_api(env={"EXECUTION_AUTHORITY_MODE": "DISABLED"})
        api.repository.execution_runtime_status = lambda: {
            "instance_id": "execution-v2-live", "worker_status": "HEALTHY",
            "execution_authority_mode": "ENABLED", "account_id": "188428665",
            "risk_policy": {"enabled": True, "canary_max_new_executions": 1},
            "canary": {"max_new_executions": 1, "consumed": 1, "remaining": 0},
            "execution_bridge": {"status": "HEALTHY"},
            "broker_account": {"status": "CONNECTED"},
        }
        with patch("execution_v2.authority_store.read_authority", return_value={"state": "ENABLED", "revision": 2, "source": "POSTGRES"}):
            status, body = api.execute("GET", "/api/v1/safety")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "ARMED")
        self.assertTrue(body["data"]["execution_enabled"])
        self.assertTrue(body["data"]["broker_write_path_active"])

    def test_readiness_requires_canonical_postgres(self):
        self.assertEqual(self.make_api().execute("GET", "/readyz")[0], 200)
        status, body = self.make_api(unavailable=True).execute("GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "SOURCE_UNAVAILABLE")

    def test_safety_execution_disabled_is_safe_not_unavailable(self):
        status, body = self.make_api().execute("GET", "/api/v1/safety")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "SAFE")
        self.assertFalse(body["data"]["execution_enabled"])
        self.assertFalse(body["data"]["broker_write_path_active"])

    def test_safety_fails_closed_when_canonical_database_unavailable(self):
        status, body = self.make_api(unavailable=True).execute("GET", "/api/v1/safety")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "SOURCE_UNAVAILABLE")

    def test_strategies_come_from_the_database_catalog(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/strategies")
        self.assertEqual((status, body["source"]), (200, "canonical_postgres"))
        self.assertEqual(body["data"][0]["strategy_id"], "S")
        detail = api.execute("GET", "/api/v1/strategies/S")[1]
        self.assertEqual((detail["data"]["strategy_version"], detail["source"]), ("V1", "canonical_postgres"))
        self.assertEqual(api.execute("GET", "/api/v1/strategies/MISSING")[0], 404)
        self.assertEqual(api.execute("GET", "/api/v1/strategies/S/report")[0], 503)

    def test_context_report_reads_canonical_outcomes_without_legacy_builder(self):
        status, body = self.make_api().execute(
            "GET", "/api/v1/strategies/CONTEXT_STRUCTURE_RETRACE_V1/report")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["data"]["report"]["outcome_authority"], "canonical_postgres")
        self.assertEqual(body["data"]["report"]["outcome_type"], "ENTRY_ONLY")

    def test_context_instance_report_is_supported_and_scoped(self):
        status, body = self.make_api().execute(
            "GET", "/api/v1/strategies/CONTEXT_STRUCTURE_RETRACE_V1/instances/phase6/report")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["data"]["report"]["identity"]["strategy_instance_id"], "phase6")

    def test_context_instance_report_unknown_instance_is_not_found(self):
        status, body = self.make_api().execute(
            "GET", "/api/v1/strategies/CONTEXT_STRUCTURE_RETRACE_V1/instances/missing/report")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "RESOURCE_NOT_FOUND")

    def test_liquidity_instance_report_is_supported_and_uses_liquidity_outcomes(self):
        status, body = self.make_api().execute(
            "GET", "/api/v1/strategies/LIQUIDITY_DISPLACEMENT_SCALP_V1/instances/liquidity-btc25/report")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["data"]["report"]["outcome_type"], "LIQUIDITY_ENTRY")
        self.assertEqual(body["data"]["report"]["identity"]["strategy_instance_id"], "liquidity-btc25")

    def test_liquidity_instance_report_unknown_instance_is_not_found(self):
        status, body = self.make_api().execute(
            "GET", "/api/v1/strategies/LIQUIDITY_DISPLACEMENT_SCALP_V1/instances/missing/report")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "RESOURCE_NOT_FOUND")

    def test_strategy_file_is_never_consulted(self):
        api = self.make_api()
        api.strategy_config_path = "/path/that/must/not/be/read/runtime/execution/state.json"
        status, body = api.execute("GET", "/api/v1/strategies")
        self.assertEqual(status, 200)
        self.assertNotIn("LEGACY", json.dumps(body))

    def test_strategy_catalog_outage_is_unavailable_not_empty(self):
        status, body = self.make_api(unavailable=True).execute("GET", "/api/v1/strategies")
        self.assertEqual((status, body["error"]), (503, "SOURCE_UNAVAILABLE"))

    def test_events_are_canonical_and_audit_truthfully_unavailable(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/events")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["data"][0]["event_id"], "evt-1")
        self.assertEqual(api.execute("GET", "/api/v1/events/evt-1")[1]["data"]["signal_id"], "sig-1")
        status, audit = api.execute("GET", "/api/v1/audit")
        self.assertEqual(status, 503)
        self.assertEqual(audit["status"], "UNAVAILABLE")

    def test_stale_events_and_safety_files_are_not_authority(self):
        import inspect
        from platform_api import control
        source = inspect.getsource(control)
        self.assertNotIn("signals.jsonl", source)
        self.assertNotIn("events.jsonl", source)
        self.assertNotIn("execution/state.json", source)

    def test_execution_disabled_is_inactive_and_metrics_are_canonical_zeroes(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/executions")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "INACTIVE")
        self.assertEqual(body["data"]["executions"], [])
        status, metrics = api.execute("GET", "/api/v1/executions/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(metrics["data"]["mt5_order_send_attempted"], 0)

    def test_execution_database_failure_is_not_misreported_as_inactive(self):
        status, body = self.make_api(unavailable=True).execute("GET", "/api/v1/executions")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "UNAVAILABLE")

    def test_execution_disabled_does_not_require_22348(self):
        status, body = self.make_api().execute("GET", "/api/v1/system")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["components"]["execution"]["status"], "INACTIVE")
        self.assertNotIn("22348", json.dumps(body))

    def test_broker_routes_are_explicitly_unavailable_without_bridge_facts(self):
        api = self.make_api()
        paths = ["/api/v1/broker/account", "/api/v1/broker/positions", "/api/v1/broker/pending-orders",
                 "/api/v1/broker/history-orders", "/api/v1/broker/deals", "/api/v1/broker/symbols",
                 "/api/v1/broker/exposure", "/api/v1/exposure"]
        for path in paths:
            with self.subTest(path=path):
                status, body = api.execute("GET", path)
                self.assertEqual(status, 503)
                self.assertEqual(body["status"], "UNAVAILABLE")
                self.assertIsNone(body.get("data"))

    def test_broker_account_reads_from_read_only_bridge(self):
        bridge = FakeReadOnlyBridge({"account": "188428665", "balance": 1234.5})
        status, body = self.make_api(bridge_reader=bridge).execute("GET", "/api/v1/broker/account")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "mt5_bridge_read_only")
        self.assertEqual(body["data"]["balance"], 1234.5)
        self.assertEqual(bridge.calls, [("mt5_account_info", None)])

    def test_broker_account_bridge_failure_is_structured_unavailable(self):
        bridge = FakeReadOnlyBridge(error="connection refused")
        status, body = self.make_api(bridge_reader=bridge).execute("GET", "/api/v1/broker/account")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "SOURCE_UNAVAILABLE")
        self.assertIn("connection refused", body["message"])

    def test_connections_separate_bridge_unavailable_from_execution_inactive(self):
        status, body = self.make_api().execute("GET", "/api/v1/connections")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["data_channel"]["status"], "UNAVAILABLE")
        self.assertEqual(body["data"]["execution_channel"]["status"], "INACTIVE")

    def test_trade_manager_empty_state_is_healthy_and_postgres_sourced(self):
        status, body = self.make_api().execute("GET", "/api/v1/trade-manager/summary")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["data"]["total_managed_trades"], 0)
        status, body = self.make_api().execute("GET", "/api/v1/managed-trades")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], [{"managed_trade_id": "MT_1", "entry_signal_id": "SIG_1",
                                         "strategy_id": "S", "instrument": "XAUUSD", "direction": "LONG",
                                         "state": "OPEN"}])

    def test_trade_manager_database_failure_is_unavailable(self):
        status, body = self.make_api(unavailable=True).execute("GET", "/api/v1/managed-trades")
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "UNAVAILABLE")

    def test_reports_use_the_canonical_context_projection(self):
        status, body = self.make_api().execute("GET", "/api/v1/reports")
        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["data"][0]["id"], "context-entry-outcomes")

        status, body = self.make_api().execute("GET", "/api/v1/reports/report-1")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "RESOURCE_NOT_FOUND")

    def test_unknown_api_path_does_not_fall_back(self):
        status, body = self.make_api().execute("GET", "/api/v1/not-a-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["source"], "platform_api_router")

    def test_all_discovered_route_patterns_have_explicit_handlers(self):
        api = self.make_api()
        paths = ["/api/v1/system", "/api/v1/safety", "/api/v1/strategies", "/api/v1/strategies/S",
                 "/api/v1/signals", "/api/v1/events", "/api/v1/events/evt-1", "/api/v1/audit",
                 "/api/v1/audit/a", "/api/v1/executions", "/api/v1/executions/metrics", "/api/v1/executions/i",
                 "/api/v1/broker/account", "/api/v1/broker/positions", "/api/v1/broker/pending-orders",
                 "/api/v1/broker/history-orders", "/api/v1/broker/deals", "/api/v1/broker/symbols",
                 "/api/v1/broker/exposure", "/api/v1/exposure", "/api/v1/connections", "/api/v1/reports",
                 "/api/v1/reports/r", "/api/v1/strategies/S/instances", "/api/v1/strategies/S/shadow",
                 "/api/v1/trade-manager/summary", "/api/v1/managed-trades", "/api/v1/managed-trades/MT_1",
                 "/api/v1/managed-trades/MT_1/decisions"]
        for path in paths:
            with self.subTest(path=path):
                status, body = api.execute("GET", path)
                self.assertIn(status, (200, 404, 503))
                self.assertIn("source", body)

    def test_signal_dispatch_remains_unchanged(self):
        class SignalSpy:
            def execute(self, method, target):
                return 209, {"path": target, "method": method}
        combined = UnifiedPlatformApi(signals=SignalSpy(), control_api=self.make_api())
        self.assertEqual(combined.execute("GET", "/api/v1/signals?limit=1")[0], 209)
        self.assertEqual(combined.execute("GET", "/api/v1/system")[0], 200)

    def test_registry_fallback_instance_detail_exposes_instance_id_at_root(self):
        """instance_page returns None for managed (strategy_mgmt) instances; the registry
        fallback must spread instance fields at the top level so the frontend adapter can
        find instanceId without it being nested under an 'instance' key."""
        uuid = "e5f7a9b1-c3d5-4e7f-a1b3-5c7e9f1b3d5e"
        api = self.make_api()
        api.strategy_mgmt_api = FakeStrategyMgmtApi([_v3_registry_row(uuid)])
        status, body = api.execute("GET", f"/api/v1/strategies/KOJO/instances/{uuid}")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["instance_id"], uuid)
        self.assertEqual(data["strategy_id"], "KOJO")
        self.assertNotIn("instance", data)

    def test_registry_fallback_instance_detail_404_when_uuid_not_in_registry(self):
        api = self.make_api()
        api.strategy_mgmt_api = FakeStrategyMgmtApi([_v3_registry_row()])
        status, _ = api.execute("GET", "/api/v1/strategies/KOJO/instances/00000000-0000-0000-0000-000000000000")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
