from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from infrastructure.messaging.contracts import EventEnvelope
from trade_management.binding import ChainedResolver, DefaultTmNoneResolver, LegacyStaticResolver, TmVersionUnavailable
from trade_management.fakes import FakeConnection
from trade_management.ids import managed_trade_id
from trade_management.managed_trade import EntrySignalRecordMissing, create_managed_trade
from trade_management.open_consumer import ManagedTradeOpenConsumer
from trade_management.versions import TM_NONE_1_MANIFEST

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
TM_NONE_ID = TM_NONE_1_MANIFEST.tm_version_id()


def seed(conn: FakeConnection, *, signal_id="SIG_1", strategy_id="STRAT_A", strategy_version="V1",
        instrument="XAUUSD", entry_signal_hash="HASH_1", decision_time="2026-09-22T11:00:00Z",
        strategy_instance_id="inst-1", entry_price=100.0, stop_price=99.0, target_price=103.0) -> None:
    conn.seed_tm_version(tm_version_id=TM_NONE_ID, manifest_hash=TM_NONE_1_MANIFEST.manifest_hash())
    conn.seed_entry_signal(signal_id=signal_id, strategy_id=strategy_id, strategy_version=strategy_version,
                           strategy_ref=f"{strategy_id}@{strategy_version}", parameter_set_ref=None,
                           parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID",
                           strategy_instance_id=strategy_instance_id, instrument=instrument, direction="LONG",
                           decision_time=decision_time, entry_price=entry_price, stop_price=stop_price,
                           risk_distance=abs(entry_price - stop_price), target_price=target_price,
                           entry_signal_hash=entry_signal_hash)


def resolver() -> DefaultTmNoneResolver:
    return DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)


class ManagedTradeCreationTests(unittest.TestCase):
    def test_canonical_entry_signal_creates_exactly_one_managed_trade(self):
        conn = FakeConnection()
        seed(conn)
        result = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(result.managed_trade_id, managed_trade_id("SIG_1"))
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)
        row = conn.tables["trade_management.managed_trade"][result.managed_trade_id]
        self.assertEqual(row["strategy_id"], "STRAT_A")
        self.assertEqual(row["strategy_version"], "V1")
        self.assertEqual(row["strategy_ref"], "STRAT_A@V1")
        self.assertEqual(row["tm_version_id"], TM_NONE_ID)
        self.assertEqual(row["reference_entry_price"], 100.0)
        self.assertEqual(row["initial_stop"], 99.0)
        self.assertEqual(row["state"], "OPEN")
        self.assertEqual(row["record_mode"], "SHADOW")

    def test_deterministic_managed_trade_id(self):
        conn = FakeConnection()
        seed(conn)
        result = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(result.managed_trade_id, managed_trade_id("SIG_1"))

    def test_frozen_strategy_and_version_identity_is_preserved(self):
        conn = FakeConnection()
        seed(conn, strategy_id="STRAT_B", strategy_version="V2")
        result = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        row = conn.tables["trade_management.managed_trade"][result.managed_trade_id]
        self.assertEqual(row["strategy_id"], "STRAT_B")
        self.assertEqual(row["strategy_version"], "V2")
        self.assertEqual(row["strategy_ref"], "STRAT_B@V2")
        # TM-NONE binding was resolved and frozen at creation.
        self.assertEqual(row["tm_version_id"], TM_NONE_ID)
        self.assertEqual(row["binding_resolution"], "DEFAULT_TM_NONE")

    def test_duplicate_event_id_is_absorbed_by_inbox(self):
        conn = FakeConnection()
        seed(conn)
        first = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        second = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(first.status, "CREATED")
        self.assertEqual(second.status, "INBOX_DUPLICATE")
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)

    def test_redelivery_with_a_different_event_id_still_yields_one_row(self):
        # Simulates: JetStream redelivers under a *different* delivery attempt but the same
        # logical signal - a distinct event_id can still occur (e.g. a replay tool). The
        # managed_trade unique(entry_signal_id) constraint is the real backstop.
        conn = FakeConnection()
        seed(conn)
        first = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        second = create_managed_trade(conn, event_id="evt-2", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(first.status, "CREATED")
        self.assertEqual(second.status, "DUPLICATE")
        self.assertEqual(first.managed_trade_id, second.managed_trade_id)
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)
        self.assertEqual(len(conn.tables["platform.outbox_events"]), 1)  # only the first creation published

    def test_same_signal_id_different_hash_is_quarantined_never_overwritten(self):
        conn = FakeConnection()
        seed(conn, entry_signal_hash="HASH_ORIGINAL")
        first = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(first.status, "CREATED")
        original_row = dict(conn.tables["trade_management.managed_trade"][first.managed_trade_id])

        # Now the stored EntrySignal has different content under the same signal_id (should not
        # happen upstream, but must never silently overwrite here).
        conn.tables["strategy.entry_signals"]["SIG_1"]["entry_signal_hash"] = "HASH_CHANGED"
        second = create_managed_trade(conn, event_id="evt-2", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(second.status, "QUARANTINED")
        self.assertEqual(len(conn.tables["trade_management.managed_trade_quarantine"]), 1)
        unchanged_row = conn.tables["trade_management.managed_trade"][first.managed_trade_id]
        self.assertEqual(unchanged_row, original_row)  # original untouched

    def test_event_claimed_hash_mismatching_the_stored_record_is_quarantined_before_creation(self):
        conn = FakeConnection()
        seed(conn, entry_signal_hash="HASH_REAL")
        result = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(),
                                      now_utc=NOW, claimed_entry_signal_hash="HASH_WRONG")
        self.assertEqual(result.status, "QUARANTINED")
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 0)
        self.assertEqual(len(conn.tables["trade_management.managed_trade_quarantine"]), 1)

    def test_missing_entry_signal_record_raises_and_never_creates_from_the_event_alone(self):
        conn = FakeConnection()
        conn.seed_tm_version(tm_version_id=TM_NONE_ID, manifest_hash=TM_NONE_1_MANIFEST.manifest_hash())
        with self.assertRaises(EntrySignalRecordMissing):
            create_managed_trade(conn, event_id="evt-1", signal_id="SIG_MISSING", resolver=resolver(), now_utc=NOW)
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 0)
        # The inbox claim itself rolled back too - eligible for a real retry later.
        self.assertEqual(len(conn.tables["platform.inbox_events"]), 0)

    def test_tm_none_1_not_registered_fails_closed_no_creation(self):
        conn = FakeConnection()
        seed(conn)
        conn.tables["trade_management.trade_manager_version"].clear()  # simulate "not registered"
        with self.assertRaises(TmVersionUnavailable):
            create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 0)
        self.assertEqual(len(conn.tables["platform.inbox_events"]), 0)  # rolled back, retryable

    def test_independence_creation_succeeds_with_no_broker_or_bridge_credentials_present(self):
        # The static import audit (which forbidden modules this package's own call graph may
        # import) lives in test_trade_management_isolation.py and is process-order-independent,
        # unlike a sys.modules check here: unittest discovery imports every test module up
        # front, so by the time this test runs, sys.modules may already contain "execution"
        # etc. from an entirely unrelated test file in the same process - that would be a false
        # positive, not a real dependency of this call graph. What we can assert here is the
        # behavioural half: no broker/bridge environment variable or credential is read or
        # required for creation to succeed.
        conn = FakeConnection()
        seed(conn)
        for var in ("MT5_BRIDGE_URL", "MT5_BRIDGE_TOKEN", "BROKER_API_KEY"):
            self.assertNotIn(var, os.environ)
        result = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(result.status, "CREATED")

    def test_unpublished_and_unexecuted_signal_still_yields_a_managed_trade(self):
        # No PublishedSignal concept and no execution intent exist anywhere in this call graph;
        # ManagedTrade creation does not require either (A6 06 section 5).
        conn = FakeConnection()
        seed(conn)
        result = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=resolver(), now_utc=NOW)
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(len(conn.tables.get("execution.execution_intents", {})), 0)

    def test_binding_resolved_once_and_a_later_binding_table_change_does_not_affect_the_trade(self):
        conn = FakeConnection()
        seed(conn, strategy_id="STRAT_C")
        legacy_id = "BIND_1"
        conn.seed_legacy_binding(binding_id=legacy_id, strategy_id="STRAT_C", tm_version_id=TM_NONE_ID,
                                 valid_from="2020-01-01T00:00:00Z", binding_hash="H1")
        chained = ChainedResolver([LegacyStaticResolver(), DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)])
        result = create_managed_trade(conn, event_id="evt-1", signal_id="SIG_1", resolver=chained, now_utc=NOW)
        row = conn.tables["trade_management.managed_trade"][result.managed_trade_id]
        self.assertEqual(row["binding_resolution"], "LEGACY_STATIC")
        self.assertEqual(row["tm_binding_id"], legacy_id)

        # A later change to the binding table must never rebind the already-created trade.
        conn.tables["trade_management.legacy_stream_binding"][legacy_id]["tm_version_id"] = "TMV_somethingelse00000"
        row_after = conn.tables["trade_management.managed_trade"][result.managed_trade_id]
        self.assertEqual(row_after["tm_version_id"], TM_NONE_ID)  # unchanged


class OpenConsumerTests(unittest.TestCase):
    def _envelope(self, *, event_id="evt-1", signal_id="SIG_1", entry_signal_hash="HASH_1") -> EventEnvelope:
        return EventEnvelope(event_id=event_id, event_type="signal.entry.created.v1", aggregate_type="signal",
                             aggregate_id=signal_id, aggregate_version=1, occurred_at="2026-09-22T11:00:01Z",
                             payload={"signal_id": signal_id, "entry_signal_hash": entry_signal_hash})

    def test_consumer_creates_exactly_one_managed_trade_from_the_canonical_subject(self):
        conn = FakeConnection()
        seed(conn)
        consumer = ManagedTradeOpenConsumer(lambda: conn, resolver(), clock=lambda: NOW)
        result = consumer.handle_envelope(self._envelope())
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)

    def test_consumer_duplicate_redelivery_remains_one_managed_trade(self):
        conn = FakeConnection()
        seed(conn)
        consumer = ManagedTradeOpenConsumer(lambda: conn, resolver(), clock=lambda: NOW)
        consumer.handle_envelope(self._envelope(event_id="evt-1"))
        consumer.handle_envelope(self._envelope(event_id="evt-1"))
        consumer.handle_envelope(self._envelope(event_id="evt-2"))  # different delivery, same signal
        self.assertEqual(len(conn.tables["trade_management.managed_trade"]), 1)

    def test_consumer_via_payload_bytes_matches_wire_format(self):
        conn = FakeConnection()
        seed(conn)
        consumer = ManagedTradeOpenConsumer(lambda: conn, resolver(), clock=lambda: NOW)
        payload = self._envelope().canonical_bytes()
        result = consumer.handle_payload(payload)
        self.assertEqual(result.status, "CREATED")


if __name__ == "__main__":
    unittest.main()
