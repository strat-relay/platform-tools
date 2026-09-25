from __future__ import annotations

import unittest
import os
from datetime import datetime, timezone

from execution_v2.bridge_fence_sim import BridgeFenceSimulator
from execution_v2.fakes import FakeBroker, FakeConnection
from execution_v2.fence import FenceAuthority
from execution_v2.risk import RiskPolicy
from execution_v2.worker import ExecutionWorker


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
ACCOUNT = "ACC1"
KEYS = {"k1": b"0" * 32}


def _policy() -> RiskPolicy:
    return RiskPolicy(version=1, enabled=True, max_volume=1.0, allowed_symbols=None,
                      allowed_accounts=(ACCOUNT,), max_signal_age_seconds=3600.0, source="test")


def _worker(conn: FakeConnection, broker: FakeBroker) -> ExecutionWorker:
    bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=ACCOUNT, clock=lambda: NOW)
    return ExecutionWorker(conn, fence_authority=FenceAuthority(keys=KEYS, active_key_id="k1"),
                           bridge=bridge, holder_instance_id="worker-a", account_id=ACCOUNT,
                           mode="demo", risk_policy=_policy())


def _seed(conn: FakeConnection) -> None:
    conn.seed_entry_signal(signal_id="SIG1", strategy_id="STRAT1", strategy_version=1,
                           strategy_ref="strat-ref", instrument="EURUSD", direction="LONG",
                           decision_time=NOW, signal_emitted_at=NOW, entry_price=1.1,
                           stop_price=1.095, target_price=1.11, entry_signal_hash="hash-1")


class HistoricalQuarantineTests(unittest.TestCase):
    def setUp(self):
        self._symbol_map_before = os.environ.get("V2_BROKER_SYMBOL_MAP_JSON")
        os.environ["V2_BROKER_SYMBOL_MAP_JSON"] = '{"demo:ACC1":{"EURUSD":"EURUSD"}}'

    def tearDown(self):
        if self._symbol_map_before is None:
            os.environ.pop("V2_BROKER_SYMBOL_MAP_JSON", None)
        else:
            os.environ["V2_BROKER_SYMBOL_MAP_JSON"] = self._symbol_map_before

    def test_quarantined_attempt_is_not_recovered_or_submitted_after_redelivery(self):
        conn = FakeConnection()
        _seed(conn)
        broker = FakeBroker(mode="fill")
        worker = _worker(conn, broker)

        first = worker.process_signal("SIG1", execution_authority_enabled=True, broker_call=broker, now_utc=NOW)
        attempt_id = first.attempt_id
        self.assertEqual(broker.calls, 1)

        # Test-only setup of the migration's persisted quarantine relation. Production uses the
        # additive 022 migration, never this direct fake setup.
        attempt_row = next(row for row in conn.tables["execution_v2.execution_attempt"].values()
                           if row["attempt_id"] == attempt_id)
        attempt_row["state"] = "SENDING"
        conn.tables["execution_v2.execution_attempt_quarantine"][attempt_id] = {
            "attempt_id": attempt_id,
            "reason": "HISTORICAL_AMBIGUOUS_EXECUTION",
            "disposition": "RECONCILIATION_REQUIRED",
            "provenance": {"source": "test"},
        }

        # A fresh worker instance models consumer/process restart. The persisted quarantine must
        # still be checked before any generation/fence/bridge operation.
        restarted = _worker(conn, broker)
        second = restarted.process_signal("SIG1", execution_authority_enabled=True, broker_call=broker, now_utc=NOW)
        self.assertEqual(second.status, "RECONCILIATION_REQUIRED")
        self.assertEqual(second.result_outcome, "HISTORICAL_AMBIGUOUS_EXECUTION")
        self.assertEqual(broker.calls, 1)

    def test_disabled_authority_creates_no_attempt_even_on_redelivery(self):
        conn = FakeConnection()
        _seed(conn)
        broker = FakeBroker(mode="fill")
        worker = _worker(conn, broker)

        for _ in range(3):
            result = worker.process_signal("SIG1", execution_authority_enabled=False,
                                           broker_call=broker, now_utc=NOW)
            self.assertEqual(result.status, "BLOCKED")
        self.assertEqual(len(conn.tables["execution_v2.execution_attempt"]), 0)
        self.assertEqual(broker.calls, 0)


if __name__ == "__main__":
    unittest.main()
