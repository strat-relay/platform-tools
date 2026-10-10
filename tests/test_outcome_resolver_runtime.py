import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from outcome_resolver_runtime import ResolverRedisCandleStore, ResolverSignal


class FakeRedis:
    def __init__(self, value):
        self.value = value

    def get(self, key):
        self.key = key
        return self.value


class OutcomeResolverRuntimeTests(unittest.TestCase):
    def signal(self):
        return ResolverSignal(
            signal_id="SIG-1", strategy_id="NEW_STRATEGY_V1", instrument="EURUSD",
            direction="LONG", entry=1.1, stop=1.0, target=1.2,
            decision_time=datetime(2026, 1, 1, tzinfo=timezone.utc), entry_type="MARKET",
            strategy_metadata={}, source_provenance={"provider_symbol": "EURUSDm"},
        )

    def test_candle_store_is_strategy_agnostic_and_preserves_close(self):
        client = FakeRedis(json.dumps([{"time": 1767225600, "high": 1.2,
                                        "low": 1.05, "close": 1.15}]))
        candles = ResolverRedisCandleStore(client).candles(self.signal(), 15)
        self.assertEqual(client.key, "md:bars:EURUSDm:M15")
        self.assertEqual(candles[0].close, 1.15)

    def test_migration_declares_durable_cursor_lease_and_cutover_fence(self):
        sql = (Path(__file__).resolve().parents[1] /
               "postgres/migrations/052_outcome_resolver_runtime_state.sql").read_text()
        for marker in ("outcome_resolver_signal_state", "outcome_resolver_lease",
                       "outcome_resolver_control", "LEGACY_COMPAT", "RESOLVER_PRIMARY"):
            self.assertIn(marker, sql)

    def test_service_is_fail_closed_without_explicit_primary_flags(self):
        from scripts.outcome_resolver_service import run_once
        with patch.dict(os.environ, {"OUTCOME_RESOLVER_ENABLED": "false",
                                     "OUTCOME_RESOLVER_PRIMARY": "false"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "primary ownership"):
                run_once(limit=1)


if __name__ == "__main__":
    unittest.main()
