from __future__ import annotations

import unittest
from datetime import datetime, timezone

from infrastructure.messaging.contracts import EventEnvelope, STREAMS
from trade_management.binding import DefaultTmNoneResolver
from trade_management.market_data import FakeMarketDataProvider, MarketQuote
from trade_management.runtime.activation import load_activation_boundary
from trade_management.runtime.fakes import FakeJetStreamManager, FakeMsg, FakeStream, RuntimeFakeConnection
from trade_management.runtime.observation_runtime import observation_tick
from trade_management.runtime.open_consumer_runtime import bootstrap_open_consumer, make_open_consumer, subscribe_open_consumer
from trade_management.runtime.streams import ensure_p4_streams, verify_trading_core_unchanged
from trade_management.runtime.tm_none_runtime import bootstrap_tm_none_consumer, subscribe_tm_none_consumer
from trade_management.versions import TM_NONE_1_MANIFEST

from nats.js.api import DeliverPolicy

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
TM_NONE_ID = TM_NONE_1_MANIFEST.tm_version_id()
OPEN_CONSUMER = "trade-mgmt-open"
DECISION_CONSUMER = "trade-manager-shadow"


def seed_db(conn: RuntimeFakeConnection) -> None:
    conn.seed_tm_version(tm_version_id=TM_NONE_ID, manifest_hash=TM_NONE_1_MANIFEST.manifest_hash())
    conn.tables.setdefault("trade_management.managed_trade", {})


def entry_signal_envelope(signal_id: str) -> bytes:
    envelope = EventEnvelope(event_id=f"{signal_id}:entry.created", event_type="signal.entry.created.v1",
                             aggregate_type="signal", aggregate_id=signal_id, aggregate_version=1,
                             occurred_at="2026-09-22T11:00:01Z",
                             payload={"signal_id": signal_id, "entry_signal_hash": f"HASH_{signal_id}"})
    return envelope.canonical_bytes()


def seed_entry_signal(conn: RuntimeFakeConnection, signal_id: str, *, instrument="XAUUSD") -> None:
    conn.seed_entry_signal(signal_id=signal_id, strategy_id="STRAT_A", strategy_version="V1",
                           strategy_ref="STRAT_A@V1", parameter_set_ref=None,
                           parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID", strategy_instance_id="inst-1",
                           instrument=instrument, direction="LONG", decision_time="2026-09-22T11:00:00Z",
                           entry_price=100.0, stop_price=99.0, risk_distance=1.0, target_price=103.0,
                           entry_signal_hash=f"HASH_{signal_id}")


class ActivationBoundaryAndBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_creates_a_deliver_policy_new_consumer_on_first_call(self):
        js = FakeJetStreamManager()
        js.streams["TRADING_CORE"] = FakeStream(subjects=["signal.entry.created.v1"])
        conn = RuntimeFakeConnection()
        seed_db(conn)

        boundary = await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        self.assertFalse(boundary["consumer_already_existed_at_bootstrap"])
        stream, config = js.add_consumer_calls[0]
        self.assertEqual(stream, "TRADING_CORE")
        self.assertEqual(config.deliver_policy, DeliverPolicy.NEW)
        self.assertEqual(config.filter_subject, "signal.entry.created.v1")
        self.assertEqual(config.durable_name, OPEN_CONSUMER)

    async def test_bootstrap_never_recreates_the_consumer_on_a_second_call(self):
        js = FakeJetStreamManager()
        js.streams["TRADING_CORE"] = FakeStream(subjects=["signal.entry.created.v1"])
        conn = RuntimeFakeConnection()
        seed_db(conn)

        first = await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        second = await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        self.assertEqual(len(js.add_consumer_calls), 1)  # never recreated
        self.assertTrue(second["consumer_already_existed_at_bootstrap"])
        self.assertEqual(first["established_at"], second["established_at"])

    async def test_pre_boundary_messages_are_never_delivered_to_the_open_consumer(self):
        js = FakeJetStreamManager()
        js.streams["TRADING_CORE"] = FakeStream(subjects=["signal.entry.created.v1"])
        js.seed_pre_existing_messages("TRADING_CORE", "signal.entry.created.v1",
                                      [entry_signal_envelope(f"PRE_{i}") for i in range(9)])
        conn = RuntimeFakeConnection()
        seed_db(conn)
        for i in range(9):
            seed_entry_signal(conn, f"PRE_{i}")

        boundary = await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        self.assertEqual(boundary["stream_messages_at_establishment"], 9)

        resolver = DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)
        consumer = make_open_consumer(lambda: conn, resolver, consumer_name=OPEN_CONSUMER)
        await subscribe_open_consumer(js, consumer, consumer_name=OPEN_CONSUMER)

        # The 9 pre-existing messages were never delivered (subscribe happened after they were
        # published, matching DeliverPolicy.NEW): no ManagedTrade exists for any of them.
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 0)

        # A signal published AFTER subscribe (a natural new EntrySignal) IS delivered and creates
        # exactly one ManagedTrade.
        seed_entry_signal(conn, "SIG_NEW")
        await js.publish("signal.entry.created.v1", entry_signal_envelope("SIG_NEW"))
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)

    async def test_trading_core_verification_never_creates_or_modifies_the_stream(self):
        js = FakeJetStreamManager()
        with self.assertRaises(Exception):
            await verify_trading_core_unchanged(js)  # missing -> fails closed
        self.assertEqual(js.add_stream_calls, [])  # never attempted to create it

        js.streams["TRADING_CORE"] = FakeStream(subjects=["signal.entry.created.v1"])
        await verify_trading_core_unchanged(js)  # present -> passes, still never touched add_stream
        self.assertEqual(js.add_stream_calls, [])

    async def test_ensure_p4_streams_only_ever_creates_trading_observation(self):
        js = FakeJetStreamManager()
        await ensure_p4_streams(js)
        self.assertEqual(js.add_stream_calls, ["TRADING_OBSERVATION"])
        self.assertNotIn("TRADING_CORE", js.streams)  # never created by this runtime

    async def test_trading_core_subject_definition_is_unchanged_by_this_branch(self):
        # The exact same assertion the pre-P4.2 test suite made about TRADING_CORE's own
        # subjects, re-affirmed here: this branch's contracts.py edit never adds to it.
        self.assertTrue(all(s.startswith(("strategy.", "signal.")) for s in STREAMS["TRADING_CORE"]["subjects"]))
        self.assertNotIn("trade.opened.v1", STREAMS["TRADING_CORE"]["subjects"])
        self.assertNotIn("trade.decision.made.v1", STREAMS["TRADING_CORE"]["subjects"])


class DuplicateAndRedeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def _bootstrapped(self):
        js = FakeJetStreamManager()
        js.streams["TRADING_CORE"] = FakeStream(subjects=["signal.entry.created.v1"])
        conn = RuntimeFakeConnection()
        seed_db(conn)
        await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        resolver = DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)
        consumer = make_open_consumer(lambda: conn, resolver, consumer_name=OPEN_CONSUMER)
        await subscribe_open_consumer(js, consumer, consumer_name=OPEN_CONSUMER)
        return js, conn

    async def test_redelivery_of_the_same_message_creates_no_duplicate_managed_trade(self):
        js, conn = await self._bootstrapped()
        seed_entry_signal(conn, "SIG_1")
        payload = entry_signal_envelope("SIG_1")
        await js.publish("signal.entry.created.v1", payload)
        # Simulate JetStream redelivery: the same subscription callback fires again.
        sub = js.subscriptions[0]
        await sub.cb(FakeMsg(data=payload))
        await sub.cb(FakeMsg(data=payload))
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_entry_signal_flows_through_to_withheld_hold(self):
        js = FakeJetStreamManager()
        js.streams["TRADING_CORE"] = FakeStream(subjects=["signal.entry.created.v1"])
        conn = RuntimeFakeConnection()
        seed_db(conn)
        await verify_trading_core_unchanged(js)
        await ensure_p4_streams(js)
        boundary = await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        await bootstrap_tm_none_consumer(js, consumer_name=DECISION_CONSUMER)

        resolver = DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)
        open_consumer = make_open_consumer(lambda: conn, resolver, consumer_name=OPEN_CONSUMER)
        await subscribe_open_consumer(js, open_consumer, consumer_name=OPEN_CONSUMER)
        await subscribe_tm_none_consumer(js, lambda: conn, consumer_name=DECISION_CONSUMER, clock=lambda: NOW)

        # 1. New canonical EntrySignal -> ManagedTrade.
        seed_entry_signal(conn, "SIG_LIVE")
        await js.publish("signal.entry.created.v1", entry_signal_envelope("SIG_LIVE"))
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)
        managed_trade_id = next(iter(conn.tables["trade_management.managed_trade"]))
        self.assertEqual(conn.tables["trade_management.managed_trade"][managed_trade_id]["state"], "OPEN")

        # 2. Observation tick -> records + publishes trade.observation.recorded.v1 -> TRADING_OBSERVATION.
        provider = FakeMarketDataProvider()
        provider.set_quote("XAUUSD", MarketQuote(instrument="XAUUSD", bid=101.0, ask=101.2,
                                                 source_timestamp="2026-09-22T12:00:00.000Z",
                                                 provider_id="fake", feed_id="f1"))
        from infrastructure.messaging.jetstream import JetStreamPublisher

        class _AsyncClientAdapter:
            def __init__(self, manager): self.manager = manager
            async def publish(self, subject, payload, **kwargs): return await self.manager.publish(subject, payload, **kwargs)
        publisher = JetStreamPublisher(_AsyncClientAdapter(js))
        summary = await observation_tick(conn, publisher, provider)
        self.assertEqual(summary["observations_recorded"], 1)
        self.assertEqual(len(conn.tables["trade_management.trade_observation"]), 1)
        observation_id = next(iter(conn.tables["trade_management.trade_observation"]))
        self.assertIn("TRADING_OBSERVATION", js.streams)
        self.assertEqual(len(js.streams["TRADING_OBSERVATION"].messages), 1)

        # 3. TM-NONE already consumed it live (subscribed before the publish) -> HOLD persisted.
        self.assertEqual(len(conn.tables["trade_management.trade_manager_decision"]), 1)
        decision_id = next(iter(conn.tables["trade_management.trade_manager_decision"]))
        decision = conn.tables["trade_management.trade_manager_decision"][decision_id]
        self.assertEqual(decision["action"], "HOLD")
        self.assertEqual(decision["observation_id"], observation_id)

        # 4. Publication gate -> WITHHELD(NOT_ACTIONABLE_HOLD). No ManagementSignal anywhere.
        gate = conn.tables["trade_management.publication_decision"][decision_id]
        self.assertEqual(gate["outcome"], "WITHHELD")
        self.assertEqual(gate["reason"], "NOT_ACTIONABLE_HOLD")
        self.assertNotIn("management_signal", "".join(conn.tables.keys()).lower())

    async def test_restart_does_not_duplicate_domain_effects(self):
        # Simulates a controlled restart: a fresh process re-bootstraps against the SAME
        # (already-populated) database/stream state.
        js = FakeJetStreamManager()
        js.streams["TRADING_CORE"] = FakeStream(subjects=["signal.entry.created.v1"])
        conn = RuntimeFakeConnection()
        seed_db(conn)
        await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        resolver = DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)
        consumer_a = make_open_consumer(lambda: conn, resolver, consumer_name=OPEN_CONSUMER)
        await subscribe_open_consumer(js, consumer_a, consumer_name=OPEN_CONSUMER)

        seed_entry_signal(conn, "SIG_RESTART")
        payload = entry_signal_envelope("SIG_RESTART")
        await js.publish("signal.entry.created.v1", payload)
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)

        # "Restart": bootstrap again (idempotent - does not recreate the consumer or move the
        # boundary) and re-subscribe a fresh consumer instance, then redeliver the same message
        # (as JetStream would for an unacked/at-least-once redelivery across a restart).
        boundary_before = load_activation_boundary(conn)
        await bootstrap_open_consumer(js, conn, consumer_name=OPEN_CONSUMER)
        boundary_after = load_activation_boundary(conn)
        self.assertEqual(boundary_before, boundary_after)

        consumer_b = make_open_consumer(lambda: conn, resolver, consumer_name=OPEN_CONSUMER)
        js.subscriptions.clear()  # the fake's naive registry; a real restart gets a fresh subscription too
        await subscribe_open_consumer(js, consumer_b, consumer_name=OPEN_CONSUMER)
        sub = js.subscriptions[0]
        await sub.cb(FakeMsg(data=payload))

        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)  # still exactly one


if __name__ == "__main__":
    unittest.main()
