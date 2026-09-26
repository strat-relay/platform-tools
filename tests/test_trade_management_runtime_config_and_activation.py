from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from trade_management.runtime.activation import establish_activation_boundary, load_activation_boundary
from trade_management.runtime.fakes import RuntimeFakeConnection as FakeConnection
from trade_management.runtime.config import RuntimeConfig, RuntimeConfigError

REQUIRED_ENV = {
    "PGHOST": "db.example", "PGPORT": "5432", "PGDATABASE": "trading", "PGUSER": "app",
    "PGPASSWORD": "secret", "NATS_URL": "nats://nats.example:4222",
}


class RuntimeConfigTests(unittest.TestCase):
    ENV_KEYS = (*REQUIRED_ENV.keys(), "EXECUTION_AUTHORITY_MODE", "P4_BRIDGE_MCP_URL",
               "P4_OBSERVATION_INTERVAL_SECONDS", "P4_HEALTH_PORT", "TRADING_POSTGRES_DSN")

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.ENV_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_starts_with_valid_configuration(self):
        os.environ.update(REQUIRED_ENV)
        config = RuntimeConfig.from_env()
        self.assertEqual(config.nats_url, REQUIRED_ENV["NATS_URL"])
        self.assertEqual(config.bridge_mcp_url, "http://127.0.0.1:22347/mcp")
        self.assertEqual(config.observation_interval_seconds, 30.0)

    def test_missing_postgres_target_fails_closed(self):
        os.environ["NATS_URL"] = "nats://nats.example:4222"
        with self.assertRaises(ValueError):
            RuntimeConfig.from_env()

    def test_missing_nats_url_fails_closed(self):
        os.environ.update({k: v for k, v in REQUIRED_ENV.items() if k != "NATS_URL"})
        with self.assertRaises(RuntimeConfigError):
            RuntimeConfig.from_env()

    def test_execution_authority_enabled_fails_closed(self):
        os.environ.update(REQUIRED_ENV)
        os.environ["EXECUTION_AUTHORITY_MODE"] = "ENABLED"
        with self.assertRaises(RuntimeConfigError):
            RuntimeConfig.from_env()

    def test_bridge_url_targeting_execution_port_fails_closed(self):
        os.environ.update(REQUIRED_ENV)
        os.environ["P4_BRIDGE_MCP_URL"] = "http://127.0.0.1:22348/mcp"
        with self.assertRaises(RuntimeConfigError):
            RuntimeConfig.from_env()

    def test_non_positive_observation_interval_fails_closed(self):
        os.environ.update(REQUIRED_ENV)
        os.environ["P4_OBSERVATION_INTERVAL_SECONDS"] = "0"
        with self.assertRaises(RuntimeConfigError):
            RuntimeConfig.from_env()

    def test_custom_observation_interval_is_honoured(self):
        os.environ.update(REQUIRED_ENV)
        os.environ["P4_OBSERVATION_INTERVAL_SECONDS"] = "45"
        self.assertEqual(RuntimeConfig.from_env().observation_interval_seconds, 45.0)


class ActivationBoundaryTests(unittest.TestCase):
    NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)

    def test_establishing_the_boundary_persists_it(self):
        conn = FakeConnection()
        boundary = establish_activation_boundary(conn, consumer_name="trade-mgmt-open",
                                                 subject="signal.entry.created.v1", stream="TRADING_CORE",
                                                 stream_messages_at_establishment=9, now_utc=self.NOW)
        self.assertEqual(boundary["consumer_name"], "trade-mgmt-open")
        self.assertEqual(boundary["stream_messages_at_establishment"], 9)
        loaded = load_activation_boundary(conn)
        self.assertEqual(loaded, boundary)

    def test_boundary_is_first_write_wins_never_moves_forward_on_redeploy(self):
        conn = FakeConnection()
        first = establish_activation_boundary(conn, consumer_name="trade-mgmt-open",
                                              subject="signal.entry.created.v1", stream="TRADING_CORE",
                                              stream_messages_at_establishment=9, now_utc=self.NOW)
        later = self.NOW.replace(hour=13)
        second = establish_activation_boundary(conn, consumer_name="trade-mgmt-open",
                                               subject="signal.entry.created.v1", stream="TRADING_CORE",
                                               stream_messages_at_establishment=42, now_utc=later)
        self.assertEqual(first, second)
        self.assertEqual(second["stream_messages_at_establishment"], 9)  # not 42

    def test_no_boundary_established_yet_returns_none(self):
        self.assertIsNone(load_activation_boundary(FakeConnection()))


if __name__ == "__main__":
    unittest.main()
