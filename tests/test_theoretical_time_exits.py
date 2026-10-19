"""Theoretical exits reach the durable outbox without a broker or market quote."""
import json
from datetime import timedelta
import unittest
from unittest.mock import patch

from test_trade_management_decision_engine import NOW, make_tm_none_trade
from trade_management.deadlines import process_deadlines, theoretical_deadline
from trade_management.fakes import FakeConnection
from trade_management.observation import record_observation
from trade_management.market_data import MarketQuote
from trade_management.tm_profit_exit import ExitPolicy, evaluate_exit_policy


class TheoreticalTimeExitTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()
        self.trade_id = make_tm_none_trade(self.conn)
        self.trade = self.conn.tables["trade_management.managed_trade"][self.trade_id]
        self.trade["decision_time"] = NOW
        self.trade["time_exit_minutes"] = 15
        self.signal = self.conn.tables["strategy.entry_signals"]["SIG_1"]
        self.signal["publication_state"] = "PUBLISHED"

    def events(self, event_type):
        return [row for row in self.conn.tables["platform.outbox_events"].values()
                if row["event_type"] == event_type]

    def test_deadline_uses_theoretical_entry_and_fires_without_broker_or_quote(self):
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=14)), 0)
        stored = self.conn.tables["trade_management.managed_trade"][self.trade_id]
        self.assertEqual(stored["time_exit_at"], NOW + timedelta(minutes=15))
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15)), 1)
        decisions = self.events("trade.decision.made.v1")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(json.loads(decisions[0]["payload"])["action"], "EXIT")
        [event] = self.events("signal.management.published.v1")
        payload = json.loads(event["payload"])
        self.assertEqual((payload["entry_signal_id"], payload["exit_reason"]), ("SIG_1", "TIME_EXIT"))
        self.assertEqual(payload["outcome_kind"], "STRATEGY_THEORETICAL")
        self.assertFalse(self.conn.tables["trade_management.market_snapshot"])
        [observation] = self.conn.tables["trade_management.trade_observation"].values()
        self.assertIsNone(observation["market_snapshot_id"])

    def test_repeated_tick_and_restart_do_not_duplicate_exit_or_notification(self):
        process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15))
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=16)), 0)
        self.assertEqual(len(self.events("trade.decision.made.v1")), 1)
        self.assertEqual(len(self.events("signal.management.published.v1")), 1)

    def test_crash_after_clock_observation_recovers_on_next_tick(self):
        with patch("trade_management.decision_engine.record_decision", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15))
        self.assertEqual(len(self.conn.tables["trade_management.trade_observation"]), 1)
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=16)), 1)
        self.assertEqual(len(self.conn.tables["trade_management.trade_observation"]), 1)
        self.assertEqual(len(self.events("signal.management.published.v1")), 1)

    def test_unpublished_entry_has_no_subscriber_event(self):
        self.signal["publication_state"] = None
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15)), 1)
        self.assertEqual(self.events("signal.management.published.v1"), [])

    def test_stop_or_target_terminal_suppresses_time_exit(self):
        self.conn.seed_strategy_outcome(signal_id="SIG_1", status="STOPPED", exit_timestamp=NOW)
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15)), 0)
        self.assertEqual(self.events("trade.decision.made.v1"), [])

    def test_canonical_time_exit_does_not_suppress_management_notification(self):
        self.conn.seed_strategy_outcome(signal_id="SIG_1", status="TIME_EXIT", exit_timestamp=NOW + timedelta(minutes=15))
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15)), 1)
        self.assertEqual(len(self.events("signal.management.published.v1")), 1)

    def test_lifecycle_waits_for_exit_decision_then_closes(self):
        from trade_management.lifecycle import reconcile_strategy_outcomes
        self.conn.seed_strategy_outcome(signal_id="SIG_1", status="TIME_EXIT", exit_timestamp=NOW + timedelta(minutes=15))
        self.assertEqual(reconcile_strategy_outcomes(self.conn, now_utc=NOW), [])
        process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15))
        self.assertEqual(len(reconcile_strategy_outcomes(self.conn, now_utc=NOW + timedelta(minutes=15))), 1)
        self.assertEqual(len(self.events("signal.management.published.v1")), 1)

    def test_expiry_does_not_need_price_geometry(self):
        self.trade["initial_stop"] = None
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15)), 1)

    def test_off_mode_does_not_emit(self):
        self.conn.set_tm_mode("OFF")
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15)), 0)

    def test_market_observation_no_longer_waits_for_a_broker_fill(self):
        result = record_observation(self.conn, managed_trade_id=self.trade_id, now_utc=NOW,
                                    quote=MarketQuote("XAUUSD", 100, 100.1, NOW.isoformat(), "test"), bars=None)
        self.assertEqual(result.status, "RECORDED")

    def test_limit_entry_needs_theoretical_activation_evidence(self):
        kwargs = dict(signal_time=NOW, strategy_id="TEST", entry_type="LIMIT", metadata={}, captured_minutes=15)
        self.assertIsNone(theoretical_deadline(**kwargs))
        self.assertEqual(theoretical_deadline(**kwargs, outcome_status="TIME_EXIT",
                                              outcome_exit=NOW + timedelta(minutes=20)), NOW + timedelta(minutes=15))

    def test_contract_hold_applies_without_explicit_tm_policy(self):
        self.trade["time_exit_minutes"] = None
        self.signal["strategy_metadata"] = {"outcome_contract": {"max_hold_minutes": 15}}
        self.assertEqual(process_deadlines(self.conn, now_utc=NOW + timedelta(minutes=15)), 1)

    def test_stale_quote_cannot_block_expired_profit_policy_timer(self):
        action, reasons, params = evaluate_exit_policy(
            trade_state="OPEN", direction="LONG", entry_price=100, bid=105, ask=106,
            risk_distance=1, pip_size=1, as_of=NOW, quote_age_seconds=600,
            policy=ExitPolicy(time_exit_at=NOW, profit_target_r=1))
        self.assertEqual((action, reasons), ("EXIT", ("TIME_EXIT_DUE",)))
        self.assertEqual(params["triggered_rules"], ["TIME_EXIT"])


if __name__ == "__main__":
    unittest.main()
