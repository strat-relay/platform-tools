from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from trade_management.binding import DefaultTmNoneResolver
from trade_management.fakes import FakeConnection
from trade_management.managed_trade import create_managed_trade
from trade_management.market_data import BarWindow, FakeMarketDataProvider, MarketQuote
from trade_management.observation import TradeObservationService, record_observation
from trade_management.tm_none import DECISION_CONSUMER_NAME, record_decision
from trade_management.versions import TM_NONE_1_MANIFEST

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
TM_NONE_ID = TM_NONE_1_MANIFEST.tm_version_id()


def make_open_trade(conn: FakeConnection, *, signal_id="SIG_1", instrument="XAUUSD") -> str:
    conn.seed_tm_version(tm_version_id=TM_NONE_ID, manifest_hash=TM_NONE_1_MANIFEST.manifest_hash())
    conn.seed_entry_signal(signal_id=signal_id, strategy_id="STRAT_A", strategy_version="V1",
                           strategy_ref="STRAT_A@V1", parameter_set_ref=None,
                           parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID", strategy_instance_id="inst-1",
                           instrument=instrument, direction="LONG", decision_time="2026-09-22T11:00:00Z",
                           entry_price=100.0, stop_price=99.0, risk_distance=1.0, target_price=103.0,
                           entry_signal_hash=f"HASH_{signal_id}")
    resolver = DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)
    result = create_managed_trade(conn, event_id=f"evt-{signal_id}", signal_id=signal_id, resolver=resolver,
                                  now_utc=NOW)
    assert result.status == "CREATED", result
    return result.managed_trade_id


def quote(*, instrument="XAUUSD", bid=101.0, ask=101.2, ts="2026-09-22T12:00:00.000Z") -> MarketQuote:
    return MarketQuote(instrument=instrument, bid=bid, ask=ask, source_timestamp=ts, provider_id="fake-provider",
                      feed_id="feed-1")


class ObservationRecordingTests(unittest.TestCase):
    def test_observation_recorded_from_market_data_provider_only(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        result = record_observation(conn, managed_trade_id=trade_id, quote=quote(),
                                    bars=BarWindow(timeframe="M5", completed_through="2026-09-22T11:55:00Z",
                                                  count=205, digest="sha256:abc"),
                                    now_utc=NOW)
        self.assertEqual(result.status, "RECORDED")
        self.assertEqual(result.observation_seq, 1)
        row = conn.tables["trade_management.trade_observation"][result.observation_id]
        self.assertEqual(row["managed_trade_id"], trade_id)
        self.assertEqual(row["tm_version_id"], TM_NONE_ID)

    def test_sequence_is_gapless_per_trade(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        seqs = []
        for i in range(5):
            r = record_observation(conn, managed_trade_id=trade_id,
                                   quote=quote(ts=f"2026-09-22T12:0{i}:00.000Z", bid=100.0 + i), bars=None, now_utc=NOW)
            seqs.append(r.observation_seq)
        self.assertEqual(seqs, [1, 2, 3, 4, 5])

    def test_two_trades_have_independent_gapless_sequences(self):
        conn = FakeConnection()
        t1 = make_open_trade(conn, signal_id="SIG_1")
        t2 = make_open_trade(conn, signal_id="SIG_2")
        r1a = record_observation(conn, managed_trade_id=t1, quote=quote(bid=100), bars=None, now_utc=NOW)
        r2a = record_observation(conn, managed_trade_id=t2, quote=quote(bid=100), bars=None, now_utc=NOW)
        r1b = record_observation(conn, managed_trade_id=t1, quote=quote(bid=101), bars=None, now_utc=NOW)
        self.assertEqual((r1a.observation_seq, r2a.observation_seq, r1b.observation_seq), (1, 1, 2))

    def test_duplicate_market_fact_does_not_consume_a_new_sequence_number(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        same_quote = quote()
        first = record_observation(conn, managed_trade_id=trade_id, quote=same_quote, bars=None, now_utc=NOW)
        second = record_observation(conn, managed_trade_id=trade_id, quote=same_quote, bars=None, now_utc=NOW)
        self.assertEqual(first.status, "RECORDED")
        self.assertEqual(second.status, "DUPLICATE")
        self.assertEqual(first.observation_id, second.observation_id)
        self.assertEqual(first.observation_seq, second.observation_seq)
        self.assertEqual(len(conn.tables["trade_management.trade_observation"]), 1)

    def test_restart_redelivery_does_not_duplicate_domain_effects(self):
        # Simulates a producer crash right after commit but before the caller's own
        # "delivered" bookkeeping: re-processing the identical fact is safe.
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        q = quote()
        for _ in range(3):
            record_observation(conn, managed_trade_id=trade_id, quote=q, bars=None, now_utc=NOW)
        self.assertEqual(len(conn.tables["trade_management.trade_observation"]), 1)
        observation_events = [r for r in conn.tables["platform.outbox_events"].values()
                              if r["event_type"] == "trade.observation.recorded.v1"]
        self.assertEqual(len(observation_events), 1)  # trade.opened.v1 from make_open_trade is the other row

    def test_observation_for_missing_managed_trade_is_reported_not_raised(self):
        conn = FakeConnection()
        result = record_observation(conn, managed_trade_id="MT_doesnotexist000000000", quote=quote(), bars=None,
                                    now_utc=NOW)
        self.assertEqual(result.status, "MANAGED_TRADE_MISSING")

    def test_observation_event_published_on_correct_subject_and_stream(self):
        from infrastructure.messaging.contracts import STREAMS
        self.assertIn("trade.observation.recorded.v1", STREAMS["TRADING_OBSERVATION"]["subjects"])
        self.assertNotIn("trade.observation.recorded.v1", STREAMS["TRADING_CORE"]["subjects"])
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        result = record_observation(conn, managed_trade_id=trade_id, quote=quote(), bars=None, now_utc=NOW)
        outbox_row = conn.tables["platform.outbox_events"][result.observation_id]
        self.assertEqual(outbox_row["event_type"], "trade.observation.recorded.v1")

    def test_no_broker_position_order_or_execution_result_required(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        provider = FakeMarketDataProvider()
        provider.set_quote("XAUUSD", quote())
        service = TradeObservationService(lambda: conn, provider, clock=lambda: NOW)
        results = service.poll_instrument("XAUUSD", [trade_id])
        self.assertEqual(results[0].status, "RECORDED")
        self.assertEqual(provider.quote_calls, 1)
        # No execution/broker table exists or was touched.
        self.assertEqual(len(conn.tables.get("execution.execution_intents", {})), 0)


class TmNoneDecisionTests(unittest.TestCase):
    def _observe(self, conn: FakeConnection, trade_id: str) -> str:
        result = record_observation(conn, managed_trade_id=trade_id, quote=quote(), bars=None, now_utc=NOW)
        self.assertEqual(result.status, "RECORDED")
        return result.observation_id

    def test_tm_none_consumes_eligible_observation_and_produces_hold(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        obs_id = self._observe(conn, trade_id)
        result = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        self.assertEqual(result.status, "RECORDED")
        self.assertEqual(result.action, "HOLD")
        self.assertIn("NO_MANAGEMENT_POLICY", result.reason_codes)

    def test_decision_is_durable_and_auditable(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        obs_id = self._observe(conn, trade_id)
        result = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        row = conn.tables["trade_management.trade_manager_decision"][result.decision_id]
        self.assertEqual(row["managed_trade_id"], trade_id)
        self.assertEqual(row["observation_id"], obs_id)
        self.assertEqual(row["action"], "HOLD")
        self.assertEqual(row["record_mode"], "SHADOW")
        self.assertIn(result.decision_id, conn.tables["platform.outbox_events"])
        self.assertEqual(conn.tables["platform.outbox_events"][result.decision_id]["event_type"],
                         "trade.decision.made.v1")

    def test_duplicate_observation_delivery_does_not_duplicate_decision(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        obs_id = self._observe(conn, trade_id)
        first = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        second = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        third = record_decision(conn, observation_id=obs_id, event_id="dec-evt-2", now_utc=NOW)  # different delivery
        self.assertEqual(first.status, "RECORDED")
        self.assertEqual(second.status, "INBOX_DUPLICATE")
        self.assertEqual(third.status, "DUPLICATE")
        self.assertEqual(len(conn.tables["trade_management.trade_manager_decision"]), 1)

    def test_no_execution_intent_or_broker_action_is_ever_created(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        obs_id = self._observe(conn, trade_id)
        record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        for table in conn.tables:
            self.assertNotIn("execution", table)
            self.assertNotIn("broker", table)

    def test_publication_gate_withholds_every_hold_decision(self):
        conn = FakeConnection()
        trade_id = make_open_trade(conn)
        obs_id = self._observe(conn, trade_id)
        result = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        gate_row = conn.tables["trade_management.publication_decision"][result.decision_id]
        self.assertEqual(gate_row["outcome"], "WITHHELD")
        self.assertEqual(gate_row["reason"], "NOT_ACTIONABLE_HOLD")

    def test_decision_for_missing_observation_is_reported_not_raised(self):
        conn = FakeConnection()
        result = record_decision(conn, observation_id="TOBS_doesnotexist00000000", event_id="dec-evt-1", now_utc=NOW)
        self.assertEqual(result.status, "OBSERVATION_MISSING")


if __name__ == "__main__":
    unittest.main()
