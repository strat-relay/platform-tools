from datetime import timedelta
from unittest.mock import MagicMock, patch
import threading
import unittest

from test_trade_management_decision_engine import NOW, make_breakeven_trail_trade, observe
from trade_management.decision_engine import record_decision
from trade_management.fakes import FakeConnection
from trade_management.signal_monitoring import monitoring_state
from trade_management.runtime.market_data_live import resolve_broker_symbol, BrokerSymbolUnavailable
from trade_management.runtime.deadline_scheduler import DeadlineScheduler
from trade_management.runtime.health import HealthState
from outcome_resolver import Candle, resolve_candle_path


class MonitoringTests(unittest.TestCase):
    def test_only_configured_provider_alias_is_accepted(self):
        with patch("trade_management.runtime.market_data_live._broker_symbol_map", return_value={"XAUUSD": "XAUUSDm"}):
            self.assertEqual(resolve_broker_symbol("XAUUSD"), "XAUUSDm")
            self.assertEqual(resolve_broker_symbol("XAUUSDm"), "XAUUSDm")
            with self.assertRaises(BrokerSymbolUnavailable):
                resolve_broker_symbol("XAUUSDx")

    def test_pending_entry_cannot_trigger_breakeven(self):
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)
        signal = conn.tables["strategy.entry_signals"]["SIG_BE"]
        signal["entry_type"] = "LIMIT"
        observation_id = observe(conn, trade_id, bid=102, ask=102.1)
        result = record_decision(conn, observation_id=observation_id, event_id="evt1", now_utc=NOW)
        self.assertEqual((result.action, result.reason_codes), ("HOLD", ("THEORETICAL_PENDING_ENTRY",)))

    def test_theoretical_activation_enables_price_management_without_broker(self):
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)
        conn.tables["strategy.entry_signals"]["SIG_BE"]["entry_type"] = "LIMIT"
        conn.seed_strategy_outcome(signal_id="SIG_BE", status="OPEN")
        conn.tables["strategy.entry_signal_outcomes"]["SIG_BE"]["resolution_evidence"] = {"activated_at": (NOW - timedelta(minutes=1)).isoformat()}
        observation_id = observe(conn, trade_id, bid=102, ask=102.1)
        result = record_decision(conn, observation_id=observation_id, event_id="evt1", now_utc=NOW)
        self.assertEqual(result.action, "MOVE_TO_BREAKEVEN")

    def test_unfilled_expiry_and_ambiguity_are_not_active_positions(self):
        row = {"entry_type": "LIMIT", "strategy_id": "TEST"}
        self.assertEqual(monitoring_state(row), "PENDING_ENTRY")
        self.assertEqual(monitoring_state({**row, "resolution_evidence": {"event": "EXPIRED_UNFILLED"}}), "EXPIRED")
        self.assertEqual(monitoring_state({**row, "resolution_state": "AMBIGUOUS_INTRABAR"}), "DATA_UNAVAILABLE")
        self.assertEqual(monitoring_state({**row, "outcome_status": "STOPPED"}), "CLOSED")

    def test_resolver_retains_theoretical_activation_for_open_limit(self):
        result = resolve_candle_path(direction="LONG", entry=100, stop=90, target=120,
            entry_timestamp=NOW, activation="ENTRY_PRICE_TOUCH", timeframe_minutes=5,
            candles=[Candle(NOW, NOW + timedelta(minutes=5), 102, 99, 101)])
        self.assertEqual(result.status, "OPEN")
        self.assertEqual(result.evidence["activated_at"], (NOW + timedelta(minutes=5)).isoformat())

    def test_further_quotes_do_not_record_another_time_exit(self):
        from test_trade_management_decision_engine import make_tm_none_trade
        from trade_management.deadlines import process_deadlines
        conn = FakeConnection()
        trade_id = make_tm_none_trade(conn)
        conn.tables["trade_management.managed_trade"][trade_id]["time_exit_minutes"] = 15
        process_deadlines(conn, now_utc=NOW)
        observation_id = observe(conn, trade_id, bid=100, ask=100.1)
        result = record_decision(conn, observation_id=observation_id, event_id="another-quote", now_utc=NOW)
        self.assertEqual(result.status, "TIME_EXIT_ALREADY_RECORDED")
        self.assertEqual(len(conn.tables["trade_management.trade_manager_decision"]), 1)

    def test_signal_projection_includes_unregistered_pending_signals_without_broker_access(self):
        from platform_api.control import PlatformControlRepository
        repository = PlatformControlRepository()
        repository._trade_management_query = MagicMock(return_value=[{
            "signal_id": "S", "strategy_id": "TEST", "entry_type": "LIMIT", "managed_trade_id": None,
            "resolution_evidence": {}, "strategy_metadata": {}, "outcome_status": None}])
        [signal] = repository.monitored_signals(50, 0)
        self.assertEqual(signal["monitoring_state"], "PENDING_ENTRY")
        self.assertEqual(signal["management_status"], "NOT_REGISTERED")
        self.assertNotIn("strategy_metadata", signal)

    def test_clock_runs_on_its_own_thread_and_closes_its_connection(self):
        conn = MagicMock()
        visited = threading.Event()
        worker_threads = []
        def process(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            visited.set()
            return 0
        scheduler = DeadlineScheduler(lambda: conn, HealthState(), interval_seconds=1)
        with patch("trade_management.runtime.deadline_scheduler.process_deadlines", side_effect=process):
            scheduler.start()
            try:
                self.assertTrue(visited.wait(2))
            finally:
                scheduler.close()
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
