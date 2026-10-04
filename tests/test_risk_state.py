"""Cached execution risk state (execution_v2/risk_state): snapshot, collector, freshness, atomic
reservation and its lifecycle through the real ExecutionWorker - with zero read-bridge calls.

Policy fixture = the mission's non-negotiable limits: risk_per_trade 0.20, max_volume 1.0,
max_daily_loss 100 USD, max_concurrent_positions 1, max_signal_age 60 s.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    import fakeredis
except ImportError:  # pragma: no cover - CI installs requirements-test.txt
    fakeredis = None

from execution_v2.bridge_fence_sim import BridgeFenceSimulator
from execution_v2.fakes import FakeBroker, FakeConnection
from execution_v2.fence import FenceAuthority
from execution_v2.intent import create_execution_intent
from execution_v2.risk import RiskPolicy
from execution_v2.worker import ExecutionWorker

NOW = datetime(2026, 9, 27, 7, 11, 59, tzinfo=timezone.utc)
T = NOW.timestamp()
ACCOUNT = "188428665"
STRATEGY_REF = "CONTEXT_STRUCTURE_RETRACE_V1@V1"
KEYS = {"k1": b"0" * 32}
SYMBOL_MAP = {"default": {"ETHUSD": "ETHUSDm", "BTCUSD": "BTCUSDm", "EURUSD": "EURUSDm"}}
# The ETHUSD incident signal (production, 2026-09-27): reached evaluation, BLOCKED RISK_STATE_UNAVAILABLE,
# no attempt. It is never replayed to a broker here - only evaluated.
ETH_SIGNAL = dict(signal_id="SIG_a96f9ef37981a7c05c5bd11d", strategy_id="CONTEXT_STRUCTURE_RETRACE_V1",
                  strategy_version="V1", strategy_ref=STRATEGY_REF, instrument="ETHUSD", direction="LONG",
                  decision_time=NOW - timedelta(seconds=1), signal_emitted_at=NOW - timedelta(seconds=1),
                  entry_price=2707.344, stop_price=2702.33, target_price=2713.45, entry_signal_hash="eth-hash")
REFERENCE = {"ETHUSDm": {"tick_size": 0.01, "tick_value": 0.01, "min_lot": 0.1, "max_lot": 200.0, "lot_step": 0.1},
             "BTCUSDm": {"tick_size": 0.01, "tick_value": 0.01, "min_lot": 0.01, "max_lot": 100.0, "lot_step": 0.01},
             "EURUSDm": {"tick_size": 0.00001, "tick_value": 1.0, "min_lot": 0.01, "max_lot": 200.0, "lot_step": 0.01}}
BTC_POSITION = {"ticket": 257161530, "symbol": "BTCUSDm", "type": 0, "volume": 0.53, "price_open": 84130.2,
                "sl": 84042.38, "tp": 84171.78, "profit": -3.74, "time": 1790440573}


def policy(**overrides) -> RiskPolicy:
    fields = dict(version=1, enabled=True, max_volume=1.0, allowed_symbols=("ETHUSD", "BTCUSD", "EURUSD"),
                  allowed_accounts=(ACCOUNT,), max_signal_age_seconds=60.0, source="test",
                  allowed_strategies=(STRATEGY_REF,), risk_per_trade=0.20, max_daily_loss=100.0,
                  max_concurrent_positions=1, max_concurrent_orders=1, max_account_exposure=1000.0)
    fields.update(overrides)
    return RiskPolicy(**fields)


class Broker:
    """Fake read bridge (22347) for the collector only. Counts every read."""

    def __init__(self, positions=(), orders=(), history=(), equity=430.0):
        self.positions, self.orders, self.history, self.equity = list(positions), list(orders), list(history), equity
        self.calls: list[str] = []
        self.fail: str | None = None

    def __call__(self, tool, arguments):
        self.calls.append(tool)
        if self.fail and tool in self.fail:
            raise ConnectionError("read bridge unreachable")
        return {"mt5_account_info": {"login": int(ACCOUNT), "equity": self.equity, "balance": self.equity},
                "mt5_positions": self.positions, "mt5_orders": self.orders, "mt5_history": self.history,
                "mt5_symbol_info": REFERENCE.get((arguments or {}).get("symbol"))}[tool]


@unittest.skipIf(fakeredis is None, "fakeredis[lua] not installed (requirements-test.txt)")
class RiskStateTestCase(unittest.TestCase):
    def setUp(self):
        from execution_v2.risk_state.collector import RiskStateCollector
        from execution_v2.risk_state.gate import CachedRiskGate
        from execution_v2.risk_state.snapshot import account_ref
        from execution_v2.risk_state.store import RedisRiskStateStore
        self.env = mock.patch.dict(os.environ, {"V2_BROKER_SYMBOL_MAP_JSON": json.dumps(SYMBOL_MAP)})
        self.env.start()
        self.clock = {"t": T}
        self.redis = fakeredis.FakeRedis()
        self.store = RedisRiskStateStore(self.redis, account_ref(ACCOUNT), clock=lambda: self.clock["t"])
        self.broker = Broker()
        canonical = {v: k for k, v in SYMBOL_MAP["default"].items()}
        self.collector = RiskStateCollector(self.store, self.broker, canonical_for=canonical.get,
                                            reference_symbols=lambda: ["ETHUSDm", "EURUSDm"],
                                            clock=lambda: self.clock["t"])
        self.gate = CachedRiskGate(self.store, broker_symbol_for=SYMBOL_MAP["default"].__getitem__,
                                   clock=lambda: self.clock["t"])
        # Tripwire: nothing on the execution path may read the MT5 bridge.
        from execution_v2.runtime.bridge_client import HttpBridgeFenceClient
        self.tripwires = [mock.patch.object(HttpBridgeFenceClient, name, side_effect=AssertionError(f"bridge {name}"))
                          for name in ("_read_tool", "read_risk_context")]
        for t in self.tripwires:
            t.start()

    def tearDown(self):
        for t in self.tripwires:
            t.stop()
        self.env.stop()

    def collect(self):
        self.assertTrue(self.collector.collect_reference())
        self.assertTrue(self.collector.collect_fast())
        self.assertTrue(self.collector.collect_history())
        self.broker.calls.clear()

    def conn(self, *signals):
        conn = FakeConnection()
        for s in signals or (ETH_SIGNAL,):
            conn.seed_entry_signal(**s)
        return conn

    def evaluate(self, conn, signal_id=ETH_SIGNAL["signal_id"], risk_policy=None, gate=None):
        return create_execution_intent(conn, signal_id=signal_id, account_id=ACCOUNT, risk_policy=risk_policy or policy(),
                                       now_utc=NOW, risk_gate=gate or self.gate)

    def worker(self, conn, risk_policy=None):
        bridge = BridgeFenceSimulator(keys=KEYS, configured_account_id=ACCOUNT, clock=lambda: NOW)
        return ExecutionWorker(conn, fence_authority=FenceAuthority(keys=KEYS, active_key_id="k1"), bridge=bridge,
                               holder_instance_id="worker-a", account_id=ACCOUNT, mode="real",
                               risk_policy=risk_policy or policy(), risk_gate=self.gate)

    def reservation(self, conn, signal_id=ETH_SIGNAL["signal_id"]):
        intent_id = next(r["execution_intent_id"] for r in conn.view("execution_v2.execution_intent").values()
                         if r["entry_signal_id"] == signal_id)
        return self.store.reservations().get(intent_id)


def signal(n: int, instrument="ETHUSD") -> dict:
    return {**ETH_SIGNAL, "signal_id": f"SIG_{n}", "entry_signal_hash": f"h{n}", "instrument": instrument}


class EthusdIncidentRegressionTests(RiskStateTestCase):
    # The bridge path no longer aborts on any open position (it counts platform positions and
    # excludes manual ones): see tests/test_bridge_risk_context.py.

    def test_cached_snapshot_makes_existing_positions_normal_policy_input(self):
        self.broker.positions = [BTC_POSITION]
        self.collect()
        conn = self.conn()
        result = self.evaluate(conn)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "MAX_CONCURRENT_POSITIONS_EXCEEDED"))
        evidence = next(iter(conn.view("execution_v2.execution_risk_evidence").values()))
        diagnostics = json.loads(evidence["diagnostics"])
        self.assertEqual((diagnostics["open_position_count"], diagnostics["risk_rejection_reason"],
                          diagnostics["risk_context_source"]), (1, "MAX_CONCURRENT_POSITIONS_EXCEEDED", "REDIS"))
        self.assertNotIn(ACCOUNT, json.dumps(diagnostics))            # account reference only
        self.assertEqual(self.store.reservations(), {})
        self.assertEqual(self.broker.calls, [])                        # no bridge reads to decide

    def test_with_capacity_the_incident_signal_is_sized_and_reserved(self):
        self.collect()
        conn = self.conn()
        result = self.evaluate(conn)
        self.assertEqual(result.status, "CREATED", result.reason)
        intent = next(iter(conn.view("execution_v2.execution_intent").values()))
        # 0.20 * 430 USD = 86 USD budget; 5.014 stop / 0.01 tick * 0.01 = 5.014 USD per lot -> capped at max_volume 1.0
        self.assertAlmostEqual(intent["approved_volume"], 1.0)
        self.assertEqual(self.reservation(conn)["status"], "RESERVED")


class ConcurrencyTests(RiskStateTestCase):
    def test_a_two_concurrent_signals_get_exactly_one_slot(self):
        self.collect()
        conns = [self.conn(signal(1)), self.conn(signal(2))]
        barrier, results = threading.Barrier(2), {}

        def run(i):
            barrier.wait()
            results[i] = self.evaluate(conns[i], signal_id=f"SIG_{i + 1}")
        threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = sorted((r.status, r.reason) for r in results.values())
        self.assertEqual(statuses, [("BLOCKED", "MAX_CONCURRENT_POSITIONS_EXCEEDED"), ("CREATED", None)])
        self.assertEqual(len(self.store.active_reservations(self.store.read_snapshot())), 1)

    def test_a_the_atomic_step_refuses_even_when_the_local_view_was_stale(self):
        """Deterministic interleaving: B's evaluation saw no reservation (read before A reserved);
        the Lua reservation still refuses it."""
        self.collect()
        self.assertEqual(self.evaluate(self.conn(signal(1)), signal_id="SIG_1").status, "CREATED")
        with mock.patch.object(self.store, "active_reservations", return_value=[]):
            result = self.evaluate(self.conn(signal(2)), signal_id="SIG_2")
        self.assertEqual((result.status, result.reason), ("BLOCKED", "MAX_CONCURRENT_POSITIONS_EXCEEDED"))

    def test_b_unknown_submission_keeps_blocking_even_past_the_ttl(self):
        self.collect()
        conn = self.conn(signal(1), signal(2))
        worker = self.worker(conn)
        outcome = worker.process_signal("SIG_1", execution_authority_enabled=True, now_utc=NOW,
                                        broker_call=FakeBroker(mode="ambiguous"))
        self.assertEqual(outcome.result_outcome, "UNKNOWN_RECONCILIATION_REQUIRED")
        self.assertEqual(self.reservation(conn, "SIG_1")["status"], "UNKNOWN")
        self.clock["t"] += 3600
        self.collect()                                   # fresh snapshot, still no broker position
        second = worker.process_signal("SIG_2", execution_authority_enabled=True,
                                       now_utc=NOW, broker_call=FakeBroker())
        self.assertEqual((second.status, second.detail), ("BLOCKED", "MAX_CONCURRENT_POSITIONS_EXCEEDED"))

    def test_c_definite_pre_submit_rejection_releases_the_slot(self):
        self.collect()
        conn = self.conn(signal(1), signal(2))
        worker = self.worker(conn)
        with mock.patch.object(worker, "_acquire_generation", side_effect=RuntimeError("lease held elsewhere")):
            first = worker.process_signal("SIG_1", execution_authority_enabled=True, now_utc=NOW, broker_call=FakeBroker())
        self.assertEqual(first.status, "FENCED_OUT")
        self.assertEqual(self.reservation(conn, "SIG_1")["status"], "RELEASED")
        broker = FakeBroker()
        second = worker.process_signal("SIG_2", execution_authority_enabled=True, now_utc=NOW, broker_call=broker)
        self.assertEqual((second.status, second.result_outcome, broker.calls), ("RESULT_RECORDED", "FILLED", 1))

    def test_d_stale_snapshot_fails_both_closed_without_bridge_fallback(self):
        self.collect()
        self.clock["t"] += 31                              # past RISK_POSITION_MAX_AGE_SECONDS=30
        conn = self.conn(signal(1), signal(2))
        for sid in ("SIG_1", "SIG_2"):
            result = self.evaluate(conn, signal_id=sid)
            self.assertEqual((result.status, result.reason), ("BLOCKED", "RISK_STATE_STALE"))
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.store.reservations(), {})

    def test_e_a_failed_collection_makes_a_young_snapshot_untrusted(self):
        """Policy: source health gates use. One failed fast collection -> degraded -> fail closed,
        even though the last good snapshot is still within its max age."""
        self.collect()
        self.broker.fail = "mt5_positions"
        self.assertFalse(self.collector.collect_fast())
        result = self.evaluate(self.conn())
        self.assertEqual((result.status, result.reason), ("BLOCKED", "RISK_STATE_UNAVAILABLE"))
        self.broker.fail = None
        self.assertTrue(self.collector.collect_fast())
        self.assertEqual(self.evaluate(self.conn()).status, "CREATED")

    def test_f_collector_failure_never_replaces_positions_with_an_empty_list(self):
        self.broker.positions = [BTC_POSITION]
        self.collect()
        self.broker.fail = "mt5_positions"
        for _ in range(3):
            self.clock["t"] += 10
            self.assertFalse(self.collector.collect_fast())
        snapshot = self.store.read_snapshot()
        self.assertEqual([p.ticket for p in snapshot.open_positions], ["257161530"])
        self.assertEqual(snapshot.source_health["fast"], "unavailable")
        health = self.store.read_health()["components"]["fast"]
        self.assertEqual((health["consecutive_failures"], health["last_failure_stage"], health["last_failure_code"]),
                         (3, "BRIDGE_READ", "CONNECTIONERROR"))
        self.assertEqual(self.store.read_health()["status"], "unavailable")


class ReservationLifecycleTests(RiskStateTestCase):
    def test_fill_confirms_and_requests_an_immediate_refresh(self):
        self.collect()
        conn = self.conn()
        outcome = self.worker(conn).process_signal(ETH_SIGNAL["signal_id"], execution_authority_enabled=True,
                                                   now_utc=NOW, broker_call=FakeBroker())
        self.assertEqual(outcome.result_outcome, "FILLED")
        self.assertEqual(self.reservation(conn)["status"], "CONFIRMED")
        self.assertEqual(self.store.take_refresh_request()["reason"], "BROKER_CONFIRMED_FILL")

    def test_confirmed_capacity_is_handed_over_to_the_broker_snapshot(self):
        self.collect()
        conn = self.conn(signal(1), signal(2))
        worker = self.worker(conn)
        worker.process_signal("SIG_1", execution_authority_enabled=True, now_utc=NOW, broker_call=FakeBroker())
        self.clock["t"] += 1
        self.broker.positions = [{**BTC_POSITION, "symbol": "ETHUSDm", "ticket": 9, "price_open": 2707.3, "sl": 2702.33}]
        self.collect()                                              # snapshot after confirmation
        snapshot = self.store.read_snapshot()
        self.assertEqual(self.store.active_reservations(snapshot), [])  # counted as the broker position now
        self.store.prune(snapshot)
        self.assertEqual(self.reservation(conn, "SIG_1")["status"], "RETIRED")
        blocked = worker.process_signal("SIG_2", execution_authority_enabled=True, now_utc=NOW, broker_call=FakeBroker())
        self.assertEqual(blocked.detail, "MAX_CONCURRENT_POSITIONS_EXCEEDED")

    def test_broker_rejection_releases(self):
        self.collect()
        conn = self.conn()
        self.worker(conn).process_signal(ETH_SIGNAL["signal_id"], execution_authority_enabled=True, now_utc=NOW,
                                         broker_call=FakeBroker(mode="reject"))
        self.assertEqual(self.reservation(conn)["status"], "RELEASED")

    def test_ttl_only_expires_reservations_that_never_reached_submission(self):
        self.collect()
        self.assertEqual(self.evaluate(self.conn(signal(1)), signal_id="SIG_1").status, "CREATED")
        self.clock["t"] += 61                                       # past RISK_RESERVATION_TTL_SECONDS
        self.collect()
        self.assertEqual(self.evaluate(self.conn(signal(2)), signal_id="SIG_2").status, "CREATED")
        intent = next(iter(self.store.reservations()))
        ok, status = self.store.mark_submitted(next(i for i, r in self.store.reservations().items()
                                                     if r["signal_id"] == "SIG_1"))
        self.assertEqual((ok, status), (False, "EXPIRED"))          # an expired slot can never be submitted
        self.assertIsNotNone(intent)

    def test_submitted_reservation_blocks_submission_when_it_was_lost(self):
        self.collect()
        conn = self.conn()
        worker = self.worker(conn)
        with mock.patch.object(self.store, "mark_submitted", return_value=(False, "EXPIRED")):
            outcome = worker.process_signal(ETH_SIGNAL["signal_id"], execution_authority_enabled=True, now_utc=NOW,
                                            broker_call=FakeBroker())
        self.assertEqual((outcome.status, outcome.detail), ("BLOCKED", "RISK_RESERVATION_NOT_HELD"))
        attempt = next(iter(conn.view("execution_v2.execution_attempt").values()))
        self.assertEqual(attempt["state"], "NOT_SENT")

    def test_reconciliation_moves_held_reservations_only_on_durable_outcomes(self):
        from execution_v2.risk_state.collector import RiskStateCollector  # noqa: F401
        self.collect()
        for n in (1, 2, 3):
            self.evaluate(self.conn(signal(n)), signal_id=f"SIG_{n}",
                          risk_policy=policy(max_concurrent_positions=5, max_concurrent_orders=5))
        by_signal = {r["signal_id"]: i for i, r in self.store.reservations().items()}
        for sid in by_signal:
            self.store.mark_submitted(by_signal[sid])
        self.store.mark_unknown(by_signal["SIG_2"], "lost")
        self.store.mark_unknown(by_signal["SIG_3"], "lost")
        states = {by_signal["SIG_1"]: "CONFIRMED", by_signal["SIG_2"]: "UNCERTAIN", by_signal["SIG_3"]: "REJECTED"}
        changes = self.collector.reconcile_reservations(lambda ids: {i: states[i] for i in ids})
        self.assertEqual(changes, {by_signal["SIG_1"]: "CONFIRMED", by_signal["SIG_3"]: "RELEASED"})
        self.assertEqual(self.store.reservations()[by_signal["SIG_2"]]["status"], "UNKNOWN")

    def test_unknown_cannot_be_released_by_the_worker_paths(self):
        self.collect()
        self.evaluate(self.conn())
        intent = next(iter(self.store.reservations()))
        self.store.mark_submitted(intent)
        self.store.mark_unknown(intent, "lost")
        self.assertEqual(self.store.release_before_submit(intent, "x"), (False, "UNKNOWN"))
        self.assertEqual(self.store.release_not_dispatched(intent, "x"), (False, "UNKNOWN"))


class FreshnessAndSnapshotTests(RiskStateTestCase):
    def test_missing_snapshot_fails_closed(self):
        result = self.evaluate(self.conn())
        self.assertEqual((result.status, result.reason), ("BLOCKED", "RISK_STATE_UNAVAILABLE"))

    def test_history_from_the_previous_trading_day_is_stale(self):
        self.collect()
        self.clock["t"] = datetime(2026, 9, 28, 0, 0, 5, tzinfo=timezone.utc).timestamp()
        self.collector.collect_fast()
        with mock.patch.object(self.store, "read_snapshot", wraps=self.store.read_snapshot):
            result = create_execution_intent(self.conn(), signal_id=ETH_SIGNAL["signal_id"], account_id=ACCOUNT,
                                             risk_policy=policy(max_signal_age_seconds=1e9), now_utc=NOW,
                                             risk_gate=self.gate)
        self.assertEqual((result.status, result.reason), ("BLOCKED", "RISK_STATE_STALE"))

    def test_daily_loss_semantics_are_unchanged_and_enforced(self):
        today = int(T) - 60
        self.broker.history = [{"time": today, "profit": -80.0, "commission": -5.0, "swap": 0},
                               {"time": today, "profit": -20.0}, {"time": today, "profit": 500.0},
                               {"time": today - 86400, "profit": -900.0}]
        self.collect()
        snapshot = self.store.read_snapshot()
        self.assertEqual((snapshot.daily_loss, snapshot.daily_realized_pnl), (105.0, 395.0))
        result = self.evaluate(self.conn())
        self.assertEqual(result.reason, "DAILY_LOSS_LIMIT_EXCEEDED")

    def test_position_without_a_stop_is_unbounded_exposure_not_zero(self):
        self.broker.positions = [{**BTC_POSITION, "sl": 0.0}]
        self.collect()
        result = self.evaluate(self.conn(), risk_policy=policy(max_concurrent_positions=5))
        self.assertEqual(result.reason, "MAX_ACCOUNT_EXPOSURE_EXCEEDED")

    def test_malformed_positions_keep_the_last_snapshot_and_fail_closed(self):
        self.collect()
        self.broker.positions = [{"ticket": 1, "symbol": "ETHUSDm", "type": 0, "price_open": 1.0}]  # no volume
        self.assertFalse(self.collector.collect_fast())
        self.assertEqual(self.store.read_health()["components"]["fast"]["last_failure_code"], "POSITION_VOLUME_INVALID")
        self.assertEqual(self.store.read_snapshot().open_positions, [])     # last valid (empty) kept, not the bad rows
        self.assertEqual(self.evaluate(self.conn()).reason, "RISK_STATE_UNAVAILABLE")

    def test_new_position_symbol_metadata_is_fetched_by_the_fast_cycle(self):
        self.collect()
        self.broker.positions = [BTC_POSITION]                       # BTCUSDm is not a reference symbol
        self.assertTrue(self.collector.collect_fast())
        self.assertIn("BTCUSDm", self.store.read_references())

    def test_reference_uses_fresh_market_metadata_before_read_bridge(self):
        for symbol, info in (("ETHUSDm", REFERENCE["ETHUSDm"]), ("EURUSDm", REFERENCE["EURUSDm"])):
            self.redis.set(f"md:meta:{symbol}", json.dumps({"symbol_info": info, "observed_at": T}))
        self.broker.fail = "mt5_symbol_info"

        self.assertTrue(self.collector.collect_reference())
        self.assertNotIn("mt5_symbol_info", self.broker.calls)
        self.assertEqual(set(self.store.read_references()), {"ETHUSDm", "EURUSDm"})

    def test_open_position_on_a_symbol_without_metadata_is_not_guessed(self):
        self.collect()
        self.broker.positions = [{**BTC_POSITION, "symbol": "XAUUSDm"}]   # broker has no metadata for it
        self.assertFalse(self.collector.collect_fast())
        self.assertEqual(self.store.read_health()["components"]["fast"]["last_failure_code"], "SYMBOL_INFO_MALFORMED")
        result = self.evaluate(self.conn(), risk_policy=policy(max_concurrent_positions=5))
        self.assertEqual(result.reason, "RISK_STATE_UNAVAILABLE")

    def test_refresh_request_triggers_an_early_fast_collection(self):
        self.collector.tick()
        self.broker.calls.clear()
        self.clock["t"] += 1
        self.assertEqual(self.collector.tick(), {})                         # nothing due yet
        self.store.request_refresh("BROKER_CONFIRMED_FILL")
        ran = self.collector.tick()
        self.assertEqual(sorted(ran), ["fast", "history"])

    def test_default_runtime_source_is_bridge(self):
        from execution_v2.runtime.config import RuntimeConfig
        self.assertEqual(RuntimeConfig.__dataclass_fields__["risk_context_source"].default, "BRIDGE")


if __name__ == "__main__":
    unittest.main()
