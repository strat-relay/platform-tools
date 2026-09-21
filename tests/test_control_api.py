import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen

from control_api.app import ControlApi, create_server


class FakeBridge:
    calls = []

    def __init__(self, endpoint):
        self.endpoint = endpoint

    def call(self, name, arguments=None):
        self.calls.append((self.endpoint, name, arguments))
        return {"tool": name}

    def health(self):
        return {"ok": True, "pending": 0, "lifecycle": {"active_waiters": 0}}


class UnavailableBridge(FakeBridge):
    def call(self, name, arguments=None):
        raise TimeoutError("timed out")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ControlApiTests(unittest.TestCase):
    def make_api(self):
        root = Path(tempfile.mkdtemp())
        write_json(root / "orchestration/config/platform.json", {
            "execution_mode": "SHADOW", "mcp_url": "http://127.0.0.1:22347/mcp",
            "execution_mcp_url": "http://127.0.0.1:22348/mcp", "strategies": [],
        })
        write_json(root / "runtime/orchestration/manifest.json", {"mode": "REAL_EXECUTION", "live_execution_enabled": True})
        write_json(root / "runtime/orchestration/state.json", {"live_execution_enabled": True})
        write_json(root / "runtime/execution/state.json", {"mode": "REAL_EXECUTION", "status": "ACTIVE"})
        write_json(root / "runtime/execution/real_state.json", {"armed": True, "mode": "REAL_EXECUTION"})
        write_json(root / "runtime/execution/real_execution_resume.json", {})
        write_json(root / "artifacts/MT5TradingBridge_execution_build_manifest.json", {"canonical_order_send_enabled": True})
        for name in ("execution_intents", "execution_decisions", "execution_skips", "events"):
            write_jsonl(root / "runtime/execution" / f"{name}.jsonl", [])
        write_jsonl(root / "runtime/execution/real_trades.jsonl", [])
        return ControlApi(root, FakeBridge)

    def make_context_api(self, generation=3, cutoff="2026-09-17T20:20:52.882814+00:00"):
        api = self.make_api()
        api.config["strategies"] = [{
            "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1",
            "enabled": True, "adapter": "ContextStructureRetraceAdapter",
            "routes": {"audit": True, "shadow_execution": True, "distribution_queue": True},
        }]
        write_json(api.sources.execution / "real_execution_resume.json", {
            "generation": generation, "real_execution_resume_generation": generation,
            "real_execution_resumed_at": cutoff, "schema": "real-execution-resume-v1",
        })
        write_json(api.sources.root / "context_structure_retrace_forward_cohort.json", {
            "status": "ACTIVE", "active_symbols": ["XAUUSDm", "BTCUSDm"],
        })
        write_jsonl(api.sources.orchestration / "signals.jsonl", [])
        write_jsonl(api.sources.orchestration / "events.jsonl", [])
        return api

    @staticmethod
    def context_signal(signal_id, created_at, **extra):
        return {"signal_id": signal_id, "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
                "created_at": created_at, "entry_opportunity_id": f"opp-{signal_id}",
                "economic_position_id": f"pos-{signal_id}", "symbol": "XAUUSDm", **extra}

    def test_strategy_card_excludes_pre_cutoff_signals_and_event(self):
        api = self.make_context_api()
        old = self.context_signal("old", "2026-09-17T20:20:52+00:00")
        write_jsonl(api.sources.orchestration / "signals.jsonl", [old])
        write_jsonl(api.sources.orchestration / "events.jsonl", [{
            "event_id": "old-event", "strategy_id": old["strategy_id"],
            "timestamp": "2026-09-17T20:20:52.5+00:00",
        }])
        card = api.execute("GET", "/api/v1/strategies/CONTEXT_STRUCTURE_RETRACE_V1", {})[1]["data"]
        self.assertEqual(card["current_epoch_signals"], 0)
        self.assertEqual(card["current_epoch_entry_opportunities"], 0)
        self.assertIsNone(card["current_epoch_last_event"])

    def test_strategy_card_shows_first_post_cutoff_signal_and_local_event(self):
        api = self.make_context_api()
        signal = self.context_signal("new", "2026-09-17T20:20:53+00:00")
        write_jsonl(api.sources.orchestration / "signals.jsonl", [signal])
        card = api.execute("GET", "/api/v1/strategies", {})[1]["data"][0]
        self.assertEqual(card["current_epoch_signals"], 1)
        self.assertEqual(card["current_epoch_entry_opportunities"], 1)
        self.assertEqual(card["current_epoch_last_event"], "2026-09-17T20:20:53+00:00")
        self.assertEqual(card["current_epoch"]["last_event_local"], "Sep 17, 2026 2:20:53 PM MDT")
        self.assertEqual(card["active_symbols"], ["XAUUSDm", "BTCUSDm"])

    def test_strategy_card_preserves_historical_signal_view(self):
        api = self.make_context_api()
        old = self.context_signal("old", "2026-09-17T20:20:52+00:00")
        write_jsonl(api.sources.orchestration / "signals.jsonl", [old])
        rows = api.execute("GET", "/api/v1/signals", {})[1]["data"]
        self.assertEqual([row["signal_id"] for row in rows], ["old"])

    def test_strategy_card_follows_new_generation_cutoff(self):
        api = self.make_context_api(generation=3, cutoff="2026-09-17T20:20:52+00:00")
        signal = self.context_signal("between", "2026-09-17T20:20:53+00:00")
        write_jsonl(api.sources.orchestration / "signals.jsonl", [signal])
        self.assertEqual(api.execute("GET", "/api/v1/strategies", {})[1]["data"][0]["current_epoch"]["generation"], 3)
        write_json(api.sources.execution / "real_execution_resume.json", {
            "generation": 4, "real_execution_resume_generation": 4,
            "real_execution_resumed_at": "2026-09-17T20:20:54+00:00",
        })
        card = api.execute("GET", "/api/v1/strategies", {})[1]["data"][0]
        self.assertEqual(card["current_epoch"]["generation"], 4)
        self.assertEqual(card["current_epoch_signals"], 0)

    def test_strategy_card_does_not_filter_physical_broker_positions(self):
        class PositionBridge(FakeBridge):
            def call(self, name, arguments=None):
                self.calls.append((self.endpoint, name, arguments))
                return [{"ticket": 1, "symbol": "EURUSDm", "opened_at": "2026-09-17T19:00:00+00:00"}]
        api = self.make_context_api()
        api.bridge_factory = PositionBridge
        status, body = api.execute("GET", "/api/v1/broker/positions", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"][0]["ticket"], 1)

    def test_strategy_card_exposes_generation_three_boundary_in_local_time(self):
        api = self.make_context_api()
        card = api.execute("GET", "/api/v1/strategies", {})[1]["data"][0]
        self.assertEqual(card["current_epoch"]["generation"], 3)
        self.assertEqual(card["current_epoch"]["cutoff"], "2026-09-17T20:20:52.882814+00:00")
        self.assertEqual(card["current_epoch"]["cutoff_local"], "Sep 17, 2026 2:20:52 PM MDT")

    def test_safety_surfaces_contradiction(self):
        status, body = self.make_api().execute("GET", "/api/v1/safety", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "BLOCKED")
        self.assertIn("PLATFORM_CONFIG_MODE_DIFFERS_FROM_MANIFEST_MODE", body["data"]["consistency"]["contradictions"])

    def test_safety_uses_build_evidence_and_does_not_block_armed_consumer(self):
        api = self.make_api()
        api.config["execution_mode"] = "REAL_EXECUTION"
        write_json(api.sources.orchestration / "manifest.json", {"mode": "REAL_EXECUTION", "live_execution_enabled": True})
        write_json(api.sources.orchestration / "state.json", {"live_execution_enabled": True, "live_execution_resume_generation": 2})
        write_json(api.sources.execution / "real_execution_resume.json", {
            "real_execution_resumed_at": "2026-09-17T19:38:09.929837+00:00", "generation": 2,
            "account_context_id": "real", "schema": "real-execution-resume-v1"})
        write_json(api.sources.execution / "real_state.json", {"armed": True, "mode": "REAL_EXECUTION", "account_context_id": "real"})
        status, body = api.execute("GET", "/api/v1/safety", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "SAFE")
        self.assertEqual(body["data"]["canonical_order_send_gate"]["effective"], "ENABLED")

    def test_safety_capability_states_fail_closed(self):
        api = self.make_api()
        for value, expected_status, expected_blocker in ((False, "BLOCKED", "CANONICAL_ORDERSEND_DISABLED"), (None, "BLOCKED", "CANONICAL_ORDERSEND_CAPABILITY_UNKNOWN")):
            manifest = {} if value is None else {"canonical_order_send_enabled": value}
            write_json(api.sources.root / "artifacts/MT5TradingBridge_execution_build_manifest.json", manifest)
            status, body = api.execute("GET", "/api/v1/safety", {})
            self.assertEqual(status, 200)
            self.assertEqual(body["data"]["status"], expected_status)
            self.assertIn(expected_blocker, body["data"]["blockers"])

    def test_safety_explicit_runtime_blockers(self):
        api = self.make_api()
        cases = (("armed", False, "REAL_CONSUMER_NOT_ARMED"), ("sole_write_owner", False, "REAL_CONSUMER_NOT_SOLE_WRITE_OWNER"),
                 ("unknown_active_outcomes", 1, "UNKNOWN_ACTIVE_OUTCOME"), ("unresolved_attempts", 1, "UNRESOLVED_ATTEMPTS"))
        for field, value, blocker in cases:
            state = {"armed": True, "mode": "REAL_EXECUTION", field: value}
            write_json(api.sources.execution / "real_state.json", state)
            if field in {"sole_write_owner", "unknown_active_outcomes", "unresolved_attempts"}:
                write_json(api.sources.execution / "state.json", {"mode": "REAL_EXECUTION", "status": "ACTIVE", field: value})
            status, body = api.execute("GET", "/api/v1/safety", {})
            self.assertEqual(status, 200)
            self.assertIn(blocker, body["data"]["blockers"])

    def test_safety_generation_and_account_mismatch_block(self):
        api = self.make_api()
        api.config["execution_mode"] = "REAL_EXECUTION"
        write_json(api.sources.execution / "real_state.json", {"armed": True, "mode": "REAL_EXECUTION", "account_context_id": "account-a"})
        write_json(api.sources.execution / "real_execution_resume.json", {"real_execution_resumed_at": "cutoff", "generation": 1, "account_context_id": "account-b"})
        write_json(api.sources.orchestration / "state.json", {"live_execution_enabled": True, "live_execution_resume_generation": 2})
        status, body = api.execute("GET", "/api/v1/safety", {})
        self.assertEqual(status, 200)
        self.assertIn("ACCOUNT_CONTEXT_MISMATCH", body["data"]["blockers"])
        self.assertIn("GENERATION_MISMATCH", body["data"]["blockers"])

    def test_mutations_are_rejected_without_bridge_call(self):
        FakeBridge.calls = []
        status, body = self.make_api().execute("POST", "/api/v1/system", {})
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "READ_ONLY_API")
        self.assertEqual(FakeBridge.calls, [])

    def test_broker_reads_use_only_read_tools(self):
        FakeBridge.calls = []
        status, body = self.make_api().execute("GET", "/api/v1/broker/account", {})
        self.assertEqual(status, 200)
        self.assertEqual(FakeBridge.calls[0][1], "mt5_account_info")

    def test_execution_metrics_separate_empty_categories(self):
        status, body = self.make_api().execute("GET", "/api/v1/executions/metrics", {})
        self.assertEqual(status, 200)
        metrics = body["data"]
        self.assertEqual(metrics["broker_capable_requests_attempted"], 0)
        self.assertEqual(metrics["mt5_order_send_attempted"], 0)
        self.assertEqual(metrics["broker_orders_accepted"], 0)
        self.assertEqual(metrics["broker_fills_observed"], 0)

    def test_malformed_authoritative_source_is_degraded(self):
        api = self.make_api()
        path = api.sources.orchestration / "signals.jsonl"
        path.write_text("not-json\n", encoding="utf-8")
        status, body = api.execute("GET", "/api/v1/signals", {})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "SOURCE_MALFORMED")
        self.assertTrue(body["degraded"])
        self.assertEqual(body["data"] if "data" in body else None, None)

    def test_missing_singular_resource_is_404(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/strategies/no-such-strategy", {})
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "RESOURCE_NOT_FOUND", "resource": "strategy", "id": "no-such-strategy"})

    def test_singular_lookup_returns_object_and_adapts_legacy_fields(self):
        api = self.make_api()
        write_jsonl(api.sources.orchestration / "signals.jsonl", [{
            "signal_id": "SIG_1", "strategy_id": "S", "strategy_instance_id": "phase6",
            "source_event_id": "phase6:economic_position:1"
        }])
        status, body = api.execute("GET", "/api/v1/signals/SIG_1", {})
        self.assertEqual(status, 200)
        self.assertNotIn("strategy_instance_id", body["data"])
        self.assertNotIn("source_event_id", body["data"])
        self.assertIn("technical_metadata", body["data"])
        self.assertEqual(body["data"]["technical_metadata"]["legacy_source_instance_id"], "phase6")

    def test_report_id_is_not_ignored(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/reports/r1", {})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "SOURCE_UNAVAILABLE")
        self.assertEqual(body["id"], "r1")

    def test_started_at_is_stable(self):
        api = self.make_api()
        first = api.execute("GET", "/api/v1/system", {})[1]["data"]["started_at"]
        time.sleep(0.001)
        second = api.execute("GET", "/api/v1/system", {})[1]["data"]["started_at"]
        self.assertEqual(first, second)

    def test_unsupported_query_parameter_is_400(self):
        status, body = self.make_api().execute("GET", "/api/v1/signals", {"from": ["2026-01-01"]})
        self.assertEqual(status, 400)
        self.assertEqual(body, {"error": "UNSUPPORTED_QUERY_PARAMETER", "parameter": "from"})

    def test_strategy_report_route_for_unknown_strategy_degrades_cleanly_not_500(self):
        status, body = self.make_api().execute("GET", "/api/v1/strategies/BASELINE/report", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["degraded"])
        self.assertEqual(body["data"]["status"], "NO_ADAPTER")

    def test_strategy_report_route_for_live_context_strategy_returns_real_report(self):
        status, body = self.make_api().execute("GET", "/api/v1/strategies/CONTEXT_STRUCTURE_RETRACE_V1/report", {})
        self.assertEqual(status, 200)
        self.assertFalse(body["degraded"])
        self.assertEqual(body["data"]["identity"]["strategy_id"], "CONTEXT_STRUCTURE_RETRACE_V1")
        self.assertIn("performance", body["data"])

    def test_strategy_instances_route_lists_liquidity_family(self):
        status, body = self.make_api().execute("GET", "/api/v1/strategies/LIQUIDITY_DISPLACEMENT_SCALP_V1/instances", {})
        self.assertEqual(status, 200)
        instance_ids = {row["instance_id"] for row in body["data"]}
        self.assertIn("LIQUIDITY_DISPLACEMENT_SCALP_V1", instance_ids)
        self.assertIn("LIQUIDITY_DISPLACEMENT_SCALP_USDJPY_25_V1", instance_ids)

    def test_strategy_shadow_route_for_strategy_with_no_shadow_is_empty_list_not_404(self):
        status, body = self.make_api().execute("GET", "/api/v1/strategies/LIQUIDITY_DISPLACEMENT_SCALP_V1/shadow", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], [])

    def test_strategies_plain_identifier_lookup_still_works_unchanged(self):
        # Config-row lookup (existing behavior) must be unaffected by the new
        # /report, /instances, /shadow sub-resource routes.
        status, body = self.make_api().execute("GET", "/api/v1/strategies", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"], [])

    def test_broker_unavailable_remains_degraded_and_null(self):
        api = self.make_api()
        api.bridge_factory = UnavailableBridge
        status, body = api.execute("GET", "/api/v1/broker/positions", {})
        self.assertEqual(status, 200)
        self.assertIsNone(body["data"])
        self.assertTrue(body["degraded"])

    def test_options_preflight_is_localhost_only(self):
        server = create_server(host="127.0.0.1", port=0, api=self.make_api())
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/v1/system", method="OPTIONS")
            with urlopen(request) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "http://localhost:5173")
                self.assertEqual(response.headers["Access-Control-Allow-Methods"], "GET, OPTIONS")
        finally:
            server.shutdown()
            server.server_close()

    def test_cors_origins_are_configurable_and_allow_listed(self):
        from control_api.app import cors_origins_from_env

        self.assertEqual(cors_origins_from_env({}), ("http://localhost:5173",))
        self.assertEqual(
            cors_origins_from_env({"CONTROL_API_CORS_ORIGINS": " https://console.stratrelay.app/ ,http://localhost:5173"}),
            ("https://console.stratrelay.app", "http://localhost:5173"),
        )
        with patch.dict(os.environ, {"CONTROL_API_CORS_ORIGINS": "https://console.stratrelay.app"}):
            server = create_server(host="127.0.0.1", port=0, api=self.make_api())
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/v1/system"
            with urlopen(Request(url, headers={"Origin": "https://console.stratrelay.app"})) as response:
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://console.stratrelay.app")
                self.assertEqual(response.headers["Access-Control-Allow-Credentials"], "true")
                self.assertEqual(response.headers["Vary"], "Origin")
            with urlopen(Request(url, headers={"Origin": "https://evil.example"})) as response:
                self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://console.stratrelay.app")
                self.assertIsNone(response.headers["Access-Control-Allow-Credentials"])
        finally:
            server.shutdown()
            server.server_close()

    def test_http_mutation_remains_405(self):
        server = create_server(host="127.0.0.1", port=0, api=self.make_api())
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(f"http://127.0.0.1:{server.server_port}/api/v1/broker/positions", method="POST")
            with self.assertRaises(Exception) as raised:
                urlopen(request)
            self.assertEqual(getattr(raised.exception, "code", None), 405)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
