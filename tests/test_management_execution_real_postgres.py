"""Real-PostgreSQL proof of personal management execution (execution_v2/management.py).

A Trade Manager decision reaches the broker only when every legacy authorize() rule holds: LIVE
mode, execution authority, a platform position linked to the decision's trade, a fresh decision,
the position present at the broker, a stop that never loosens or disappears, a changed request, and
an unused action key. Then exactly one REDUCE_ONLY fenced write is sent, and an ambiguous outcome is
reconciled by goal state. The bridge is a recording fake; no broker is involved.
"""
from __future__ import annotations

import unittest
from datetime import timedelta

from execution_v2.bridge_fence_types import SubmitResult
from execution_v2.fence import FenceAuthority
from execution_v2.management import ManagementWorker, management_request_fingerprint
from execution_v2.risk import RiskPolicy
from execution_v2.worker import ExecutionWorker
from test_trade_manager_live_real_postgres import ACCOUNT, LiveTradeManagerRealPostgresTests, _database_available

TICKET = "5001"


class Bridge:
    def __init__(self):
        self.positions = [{"ticket": 5001, "symbol": "XAUUSDm", "type": 0, "volume": 0.01, "price_open": 2001.0,
                           "sl": 1990.0, "tp": 2020.0, "profit": 1.0}]
        self.submits: list = []
        self.response = {"ok": True, "applied": True}
        self.error: Exception | None = None

    def read_positions(self):
        return [dict(p) for p in self.positions]

    def submit_management(self, *, authorization, request_args):
        self.submits.append((authorization, dict(request_args)))
        if self.error:
            raise self.error
        return SubmitResult(authorization.attempt_id, "DISPATCHED", self.response)


@unittest.skipUnless(_database_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class ManagementExecutionTests(unittest.TestCase):
    _signal = LiveTradeManagerRealPostgresTests._signal
    _managed_trade = LiveTradeManagerRealPostgresTests._managed_trade
    _v2_fill = LiveTradeManagerRealPostgresTests._v2_fill

    def setUp(self):
        from test_integration_tm_membership_runner import FreshDatabase
        from postgres.db import apply_migrations
        self.db = FreshDatabase()
        self.conn = self.db.connect()
        apply_migrations(self.conn)
        self.conn.commit()
        from datetime import datetime, timezone
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.set_mode("LIVE")
        from execution_v2.runtime.service import register_runtime_instance
        register_runtime_instance(self.conn, instance_id="test-holder")      # the lease holder, as at runtime startup
        self.conn.commit()
        self.authority = "ENABLED"
        self.bridge = Bridge()
        worker = ExecutionWorker(self.conn, fence_authority=FenceAuthority(keys={"k1": b"k" * 32}, active_key_id="k1"),
                                 bridge=self.bridge, holder_instance_id="test-holder", account_id=ACCOUNT, mode="real",
                                 risk_policy=RiskPolicy(version=1, enabled=False, max_volume=0.0, allowed_symbols=(),
                                                        allowed_accounts=(), max_signal_age_seconds=60.0, source="test"),
                                 authority_provider=lambda: self.authority)
        self.manager = ManagementWorker(worker)
        signal = self._signal()
        self.trade = self._managed_trade(signal)
        self._v2_fill(signal, position_id=TICKET)

    def tearDown(self):
        self.conn.close()
        self.db.drop()

    def set_mode(self, mode):
        with self.conn.cursor() as cur:
            cur.execute("UPDATE trade_management.trade_manager_mode SET mode=%s WHERE mode_id='current'", (mode,))
        self.conn.commit()

    def decide(self, action="MOVE_TO_BREAKEVEN", *, decision_id="D1", age=5, trade=None, **params):
        payload = {"decision_id": decision_id, "managed_trade_id": trade or self.trade, "action": action,
                   "parameters": params, "decision_time": (self.now - timedelta(seconds=age)).isoformat()}
        return self.manager.process_decision(payload, now_utc=self.now)

    def row(self, decision_id="D1"):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT status, reason, broker_action, requested_stop, requested_target, broker_ticket
                           FROM execution_v2.management_intent WHERE decision_id=%s""", (decision_id,))
            r = cur.fetchone()
        return r and (r[0], r[1], r[2], float(r[3]) if r[3] is not None else None,
                      float(r[4]) if r[4] is not None else None, r[5])

    def test_breakeven_is_one_fenced_reduce_only_modify(self):
        outcome = self.decide(new_stop=2000.0)
        self.assertEqual(outcome.status, "APPLIED")
        [(auth, args)] = self.bridge.submits
        self.assertEqual(args, {"ticket": 5001, "stop_loss": 2000.0, "take_profit": 2020.0, "confirm": True})
        self.assertEqual((auth.tool, auth.scope_class, auth.resource), ("mt5_position_modify", "REDUCE_ONLY",
                                                                        f"execution:real:{ACCOUNT}"))
        self.assertEqual(auth.request_fingerprint, management_request_fingerprint("mt5_position_modify", args))
        self.assertEqual(self.row(), ("APPLIED", None, "MODIFY", 2000.0, 2020.0, TICKET))

    def test_redelivery_and_repeated_action_never_write_twice(self):
        self.decide(new_stop=2000.0)
        self.assertEqual(self.decide(new_stop=2000.0).status, "DUPLICATE")
        second = self.decide(new_stop=2000.0, decision_id="D2")          # same change, new decision
        self.assertEqual((second.status, second.reason), ("REJECTED", "DUPLICATE_MANAGEMENT_ACTION"))
        self.assertEqual(len(self.bridge.submits), 1)

    def test_shadow_mode_unlinked_trades_and_hold_are_ignored_without_a_row(self):
        self.assertEqual(self.decide("HOLD").reason, "NOT_ACTIONABLE")
        unlinked = self._managed_trade(self._signal())
        self.assertEqual(self.decide(new_stop=2000.0, trade=unlinked, decision_id="D3").reason, "NOT_A_PLATFORM_POSITION")
        self.set_mode("SHADOW")
        self.assertEqual(self.decide(new_stop=2000.0).reason, "TRADE_MANAGER_NOT_LIVE")
        self.assertIsNone(self.row())
        self.assertEqual(self.bridge.submits, [])

    def test_a_stop_is_never_loosened(self):
        outcome = self.decide("TRAIL_STOP", new_stop=1985.0)                # below the broker stop of a LONG
        self.assertEqual((outcome.status, outcome.reason), ("REJECTED", "RISK_INCREASING_STOP_CHANGE"))
        self.assertEqual(self.bridge.submits, [])

    def test_fail_closed_conditions(self):
        cases = [("authority", "EXECUTION_AUTHORITY_DISABLED"), ("stale", "STALE_DECISION"),
                 ("missing", "POSITION_NOT_FOUND"), ("unreadable", "BROKER_POSITIONS_UNAVAILABLE")]
        for n, (case, reason) in enumerate(cases):
            with self.subTest(case):
                self.authority = "DISABLED" if case == "authority" else "ENABLED"
                self.bridge.positions = [] if case == "missing" else Bridge().positions
                if case == "unreadable":
                    self.bridge.read_positions = lambda: (_ for _ in ()).throw(ConnectionError("down"))
                outcome = self.decide(new_stop=2000.0, decision_id=f"F{n}", age=300 if case == "stale" else 5)
                self.assertEqual((outcome.status, outcome.reason), ("REJECTED", reason))
        self.assertEqual(self.bridge.submits, [])

    def test_request_equal_to_the_broker_is_no_change(self):
        self.assertEqual(self.decide("MOVE_STOP", new_stop=1990.0).status, "NO_CHANGE")
        self.assertEqual(self.bridge.submits, [])

    def test_move_target_keeps_the_broker_stop(self):
        self.assertEqual(self.decide("MOVE_TARGET", new_target=2035.0).status, "APPLIED")
        self.assertEqual(self.bridge.submits[0][1], {"ticket": 5001, "stop_loss": 1990.0, "take_profit": 2035.0,
                                                     "confirm": True})

    def test_exit_closes_the_position(self):
        self.assertEqual(self.decide("EXIT").status, "APPLIED")
        auth, args = self.bridge.submits[0]
        self.assertEqual((auth.tool, args), ("mt5_close_position", {"ticket": 5001, "confirm": True}))

    def test_broker_refusal_is_recorded(self):
        self.bridge.response = {"ok": False, "error": "STOP_INSIDE_BROKER_DISTANCE"}
        outcome = self.decide(new_stop=2000.0)
        self.assertEqual((outcome.status, outcome.reason), ("BROKER_REJECTED", "STOP_INSIDE_BROKER_DISTANCE"))

    def test_ambiguous_outcome_reconciles_by_goal_state(self):
        self.bridge.error = TimeoutError("bridge timed out")
        lost = self.decide(new_stop=2000.0)                                   # the write never landed
        self.assertEqual(lost.status, "UNKNOWN_RECONCILIATION_REQUIRED")
        original = self.bridge.submit_management

        def lands_then_times_out(*, authorization, request_args):
            self.bridge.positions[0]["sl"] = request_args["stop_loss"]      # broker applied it...
            return original(authorization=authorization, request_args=request_args)   # ...reply lost
        self.bridge.submit_management = lands_then_times_out
        landed = self.decide("TRAIL_STOP", new_stop=2004.0, decision_id="D2")
        self.assertEqual((landed.status, landed.reason), ("APPLIED", "GOAL_STATE_RECONCILED"))
        self.assertEqual(self.row("D2")[:2], ("APPLIED", "GOAL_STATE_RECONCILED"))

    def test_fingerprint_matches_the_bridge(self):
        # Vectors computed by mt5_bridge.fence_boundary.management_request_fingerprint.
        self.assertEqual(management_request_fingerprint("mt5_position_modify", {"ticket": 5001, "stop_loss": 2000.0,
                                                                                "take_profit": 2020.0, "confirm": True}),
                         "c786a8a199dc26ae5534887c7d2d03888e80bce5f2e4e71a7926c2b116ac7d68")
        self.assertEqual(management_request_fingerprint("mt5_close_position", {"ticket": 5001, "confirm": True}),
                         "282b614c1c9ce0aa3bdd3b0cd2ee268444d22967b84151b6fd678991065bee1c")


if __name__ == "__main__":
    unittest.main()
