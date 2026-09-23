"""execution_v2.runtime proof: RuntimeConfig.from_env() fails closed on missing/unsafe
configuration (mission section 15, plus the audit-remediation's new V2_BRIDGE_FENCE_URL
requirement), and ExecutionSignalConsumer routes an entry-signal event into ExecutionWorker.
process_signal using ONLY the config-derived execution_authority_mode captured at construction
time - never inferring authority from the event itself.
"""
from __future__ import annotations

import os
import threading
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

from infrastructure.messaging.contracts import EventEnvelope
from migration.flags import ExecutionAuthorityMode

from execution_v2.bridge_fence_sim import BridgeFenceSimulator
from execution_v2.fakes import FakeBroker, FakeConnection
from execution_v2.fence import FenceAuthority
from execution_v2.risk import RiskPolicy
from execution_v2.runtime.bridge_client import HttpBridgeFenceClient
from execution_v2.runtime.config import RuntimeConfig, RuntimeConfigError
from execution_v2.runtime.consumer import ExecutionSignalConsumer, RealBridgeNotWired
from execution_v2.worker import ExecutionAuthorityDisabled, ExecutionWorker

REQUIRED_ENV = {
    "PGHOST": "localhost", "PGPORT": "5432", "PGDATABASE": "v2exec", "PGUSER": "v2exec", "PGPASSWORD": "v2exec",
    "NATS_URL": "nats://localhost:4222",
    "V2_EXECUTION_ACCOUNT_ID": "ACC1",
    "V2_FENCE_SIGNING_KEY": "x" * 32,
    "V2_BRIDGE_FENCE_URL": "http://127.0.0.1:59999",
    "POD_NAME": "execution-v2-test-pod",
}


def _clean_env():
    keys = list(REQUIRED_ENV) + ["EXECUTION_AUTHORITY_MODE", "V2_EXECUTION_BRIDGE_MODE",
                                 "V2_EXECUTION_RISK_POLICY_PATH", "V2_EXECUTION_HEALTH_PORT",
                                 "TRADING_POSTGRES_DSN", "HOSTNAME"]
    return {k: os.environ.get(k) for k in keys}


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self._saved = _clean_env()
        for key in self._saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_loads_successfully_with_full_required_configuration_and_defaults_to_disabled(self):
        with mock.patch.dict(os.environ, REQUIRED_ENV):
            config = RuntimeConfig.from_env()
        self.assertIs(config.execution_authority_mode, ExecutionAuthorityMode.DISABLED)
        self.assertEqual(config.bridge_mode, "demo")
        self.assertEqual(config.bridge_fence_url, REQUIRED_ENV["V2_BRIDGE_FENCE_URL"])
        self.assertTrue(config.risk_policy_path.endswith("v2_execution_risk_policy.json"))

    def test_fails_closed_without_a_postgres_target(self):
        env = {k: v for k, v in REQUIRED_ENV.items() if k not in ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Exception):
                RuntimeConfig.from_env()

    def test_fails_closed_without_nats_url(self):
        env = {k: v for k, v in REQUIRED_ENV.items() if k != "NATS_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_fails_closed_without_an_account_id(self):
        env = {k: v for k, v in REQUIRED_ENV.items() if k != "V2_EXECUTION_ACCOUNT_ID"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_fails_closed_without_a_fence_signing_key(self):
        env = {k: v for k, v in REQUIRED_ENV.items() if k != "V2_FENCE_SIGNING_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_fails_closed_on_a_too_short_fence_signing_key(self):
        env = dict(REQUIRED_ENV, V2_FENCE_SIGNING_KEY="short")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_fails_closed_on_an_invalid_bridge_mode(self):
        env = dict(REQUIRED_ENV, V2_EXECUTION_BRIDGE_MODE="production-live")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_fails_closed_without_a_stable_holder_identity(self):
        env = {k: v for k, v in REQUIRED_ENV.items() if k != "POD_NAME"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_fails_closed_without_a_real_bridge_fence_url(self):
        """The core audit-remediation gate (mission section 2): with no bridge configured at
        all, the runtime must never start, let alone fall back to a simulator."""
        env = {k: v for k, v in REQUIRED_ENV.items() if k != "V2_BRIDGE_FENCE_URL"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_fails_closed_if_bridge_fence_url_targets_the_live_order_port(self):
        env = dict(REQUIRED_ENV, V2_BRIDGE_FENCE_URL="http://192.168.1.166:22348")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeConfigError):
                RuntimeConfig.from_env()

    def test_reads_an_explicitly_enabled_mode_through_unchanged(self):
        env = dict(REQUIRED_ENV, EXECUTION_AUTHORITY_MODE="ENABLED")
        with mock.patch.dict(os.environ, env, clear=True):
            config = RuntimeConfig.from_env()
        self.assertIs(config.execution_authority_mode, ExecutionAuthorityMode.ENABLED)


def _worker(*, bridge: Any = None) -> ExecutionWorker:
    conn = FakeConnection()
    conn.seed_entry_signal(signal_id="SIG1", strategy_id="STRAT1", strategy_version=1, strategy_ref="strat-ref",
                           instrument="EURUSD", direction="LONG", decision_time=datetime.now(timezone.utc),
                           entry_price=1.1000, stop_price=1.0950, target_price=1.1100, entry_signal_hash="h1")
    keys = {"k1": b"0" * 32}
    if bridge is None:
        bridge = BridgeFenceSimulator(keys=keys, configured_account_id="ACC1")
    authority = FenceAuthority(keys=keys, active_key_id="k1")
    policy = RiskPolicy(version=1, enabled=True, max_volume=0.01, allowed_symbols=None,
                       allowed_accounts=("ACC1",), max_signal_age_seconds=3600.0, source="test")
    return ExecutionWorker(conn, fence_authority=authority, bridge=bridge, holder_instance_id="worker-a",
                           account_id="ACC1", mode="demo", risk_policy=policy)


def _envelope() -> EventEnvelope:
    return EventEnvelope("EVT1", "signal.entry.created.v1", "entry_signal", "SIG1", 1,
                         datetime.now(timezone.utc), {"signal_id": "SIG1"}, None, None)


class ExecutionSignalConsumerTests(unittest.TestCase):
    def test_disabled_mode_raises_before_touching_the_bridge_or_postgresql_writes(self):
        worker = _worker()
        consumer = ExecutionSignalConsumer(worker, execution_authority_mode=ExecutionAuthorityMode.DISABLED)
        with self.assertRaises(ExecutionAuthorityDisabled):
            consumer.handle_envelope(_envelope())
        self.assertEqual(len(worker.conn.tables["execution_v2.execution_intent"]), 0)

    def test_defense_in_depth_sentinel_fires_if_any_bridge_ever_invokes_broker_call_locally(self):
        """Not the primary safety mechanism (see RealBridgeNotWired's own docstring) - this
        proves the sentinel itself works, using the test-only simulator purely as a bridge
        implementation that (unlike HttpBridgeFenceClient) DOES invoke broker_call, so the
        regression guard is exercised at all."""
        worker = _worker()
        consumer = ExecutionSignalConsumer(worker, execution_authority_mode=ExecutionAuthorityMode.ENABLED)
        with self.assertRaises(RealBridgeNotWired):
            consumer.handle_envelope(_envelope())
        results = worker.conn.tables["execution_v2.execution_result"]
        self.assertEqual(len(results), 0)

    def test_enabled_mode_with_the_real_http_bridge_client_never_touches_the_local_sentinel(self):
        """The actual production path: ExecutionSignalConsumer -> ExecutionWorker ->
        HttpBridgeFenceClient -> a real (test-local) mt5_bridge_fence HTTP server. The
        platform-side `_real_bridge_not_wired` sentinel is passed down but never invoked -
        HttpBridgeFenceClient ignores it entirely - and the only broker effect is the server's
        own fake, counting broker_call."""
        import tempfile
        from mt5_bridge_fence.boundary import RealBridgeFenceBoundary
        from mt5_bridge_fence.http_server import start_bridge_fence_server

        calls = {"n": 0}
        def broker_call():
            calls["n"] += 1
            return {"status": "FILLED", "broker_order_id": "RT-1"}

        with tempfile.TemporaryDirectory() as td:
            keys = {"k1": b"0" * 32}
            boundary = RealBridgeFenceBoundary(keys=keys, configured_account_id="ACC1", db_path=f"{td}/fence.db")
            server = start_bridge_fence_server(boundary, broker_call=broker_call)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                worker = _worker(bridge=HttpBridgeFenceClient(base_url=f"http://127.0.0.1:{port}"))
                consumer = ExecutionSignalConsumer(worker, execution_authority_mode=ExecutionAuthorityMode.ENABLED)
                outcome = consumer.handle_envelope(_envelope())
                self.assertEqual(outcome.status, "RESULT_RECORDED")
                self.assertEqual(outcome.result_outcome, "FILLED")
                self.assertEqual(calls["n"], 1)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
