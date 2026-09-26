"""trade_management/decision_engine.py: dispatch-by-binding, exercised end to end against the
same FakeConnection/record_observation/create_managed_trade harness
tests/test_trade_management_observation_and_tm_none.py already uses.

Two things this file exists to prove:
  1. A trade still bound to TM-NONE-1 gets byte-for-byte the same outcome through the new
     generalized engine as it got through tm_none.record_decision directly (no regression for
     every trade already running today).
  2. A trade bound to TM-BREAKEVEN-TRAIL-1 gets real, varying decisions - and the publication
     gate still withholds every one of them, exactly as it does for TM-NONE's HOLDs.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from trade_management.binding import DefaultTmNoneResolver, LegacyStaticResolver, ChainedResolver
from trade_management.decision_engine import DECISION_CONSUMER_NAME, UnknownEvaluator, record_decision
from trade_management.fakes import FakeConnection
from trade_management.managed_trade import create_managed_trade
from trade_management.market_data import MarketQuote
from trade_management.observation import record_observation
from trade_management.tm_none import record_decision as tm_none_record_decision
from trade_management.versions import TM_NONE_1_MANIFEST, tm_breakeven_trail_manifest

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
TM_NONE_ID = TM_NONE_1_MANIFEST.tm_version_id()

BREAKEVEN_MANIFEST = tm_breakeven_trail_manifest(breakeven_trigger_r=1.0, trail_trigger_r=1.5,
                                                 trail_distance_r=0.5, label="TM-BREAKEVEN-TRAIL-TEST")
BREAKEVEN_ID = BREAKEVEN_MANIFEST.tm_version_id()


def quote(*, instrument="XAUUSD", bid=101.0, ask=101.2, ts="2026-09-22T12:00:00.000Z") -> MarketQuote:
    return MarketQuote(instrument=instrument, bid=bid, ask=ask, source_timestamp=ts, provider_id="fake-provider",
                      feed_id="feed-1")


def make_tm_none_trade(conn: FakeConnection, *, signal_id="SIG_1", instrument="XAUUSD") -> str:
    conn.seed_tm_version(tm_version_id=TM_NONE_ID, manifest_hash=TM_NONE_1_MANIFEST.manifest_hash(),
                         evaluator_id="tm-none.v1", manifest=TM_NONE_1_MANIFEST.identity_manifest())
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


def make_breakeven_trail_trade(conn: FakeConnection, *, signal_id="SIG_BE", instrument="XAUUSD",
                               strategy_id="STRAT_BE", direction="LONG", entry_price=100.0,
                               stop_price=99.0) -> str:
    """Binds via LegacyStaticResolver + a seeded legacy_stream_binding row - the same mechanism
    real per-strategy configuration uses (scripts/seed_tm_bindings.py), not test-only shortcut
    wiring."""
    conn.seed_tm_version(tm_version_id=BREAKEVEN_ID, manifest_hash=BREAKEVEN_MANIFEST.manifest_hash(),
                         evaluator_id="tm-breakeven-trail.v1", manifest=BREAKEVEN_MANIFEST.identity_manifest())
    conn.seed_entry_signal(signal_id=signal_id, strategy_id=strategy_id, strategy_version="V1",
                           strategy_ref=f"{strategy_id}@V1", parameter_set_ref=None,
                           parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID", strategy_instance_id="inst-1",
                           instrument=instrument, direction=direction, decision_time="2026-09-22T11:00:00Z",
                           entry_price=entry_price, stop_price=stop_price, risk_distance=abs(entry_price - stop_price),
                           target_price=entry_price + (3 if direction == "LONG" else -3) * abs(entry_price - stop_price),
                           entry_signal_hash=f"HASH_{signal_id}")
    conn.seed_legacy_binding(binding_id=f"BIND_{strategy_id}", strategy_id=strategy_id,
                             tm_version_id=BREAKEVEN_ID, valid_from="2026-09-01T00:00:00Z",
                             binding_hash="test-binding-hash")
    resolver = ChainedResolver([LegacyStaticResolver(), DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)])
    result = create_managed_trade(conn, event_id=f"evt-{signal_id}", signal_id=signal_id, resolver=resolver,
                                  now_utc=NOW)
    assert result.status == "CREATED", result
    assert result.tm_version_id == BREAKEVEN_ID, "test setup bug: trade bound to the wrong version"
    return result.managed_trade_id


def observe(conn: FakeConnection, trade_id: str, **quote_kwargs) -> str:
    result = record_observation(conn, managed_trade_id=trade_id, quote=quote(**quote_kwargs), bars=None, now_utc=NOW)
    assert result.status == "RECORDED", result
    return result.observation_id


def parameters_of(row: dict) -> dict:
    """FakeConnection stores whatever was passed to the `::jsonb`-cast column verbatim - a JSON
    string, since decision_engine.py encodes it before the INSERT the same way it encodes the
    outbox payload. A real driver auto-deserializes jsonb on read; this mirrors that for
    assertions, the same defensive parse decision_engine.py's own _load_latest_decision uses."""
    value = row["parameters"]
    return json.loads(value) if isinstance(value, str) else value


class TmNoneParityTests(unittest.TestCase):
    """The new engine must be indistinguishable from calling tm_none.record_decision directly,
    for every trade still bound to TM-NONE-1."""

    def test_same_action_and_reason_as_the_direct_tm_none_call(self):
        conn_old, conn_new = FakeConnection(), FakeConnection()
        trade_old, trade_new = make_tm_none_trade(conn_old), make_tm_none_trade(conn_new)
        obs_old, obs_new = observe(conn_old, trade_old), observe(conn_new, trade_new)

        old = tm_none_record_decision(conn_old, observation_id=obs_old, event_id="dec-evt-1", now_utc=NOW)
        new = record_decision(conn_new, observation_id=obs_new, event_id="dec-evt-1", now_utc=NOW)

        self.assertEqual(old.status, new.status)
        self.assertEqual(old.action, new.action)
        self.assertEqual(set(old.reason_codes), set(new.reason_codes))
        self.assertEqual(old.action, "HOLD")

    def test_gate_still_withholds_tm_none_via_the_new_engine(self):
        conn = FakeConnection()
        trade_id = make_tm_none_trade(conn)
        obs_id = observe(conn, trade_id)
        result = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        gate_row = conn.tables["trade_management.publication_decision"][result.decision_id]
        self.assertEqual(gate_row["outcome"], "WITHHELD")
        self.assertEqual(gate_row["reason"], "NOT_ACTIONABLE_HOLD")

    def test_duplicate_and_inbox_dedup_semantics_are_unchanged(self):
        conn = FakeConnection()
        trade_id = make_tm_none_trade(conn)
        obs_id = observe(conn, trade_id)
        first = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        second = record_decision(conn, observation_id=obs_id, event_id="dec-evt-1", now_utc=NOW)
        third = record_decision(conn, observation_id=obs_id, event_id="dec-evt-2", now_utc=NOW)
        self.assertEqual((first.status, second.status, third.status), ("RECORDED", "INBOX_DUPLICATE", "DUPLICATE"))


class BreakevenTrailDispatchTests(unittest.TestCase):
    def test_below_trigger_holds(self):
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)
        obs_id = observe(conn, trade_id, bid=100.3, ask=100.5)  # r ~ 0.3-0.5, below 1.0 trigger
        result = record_decision(conn, observation_id=obs_id, event_id="e1", now_utc=NOW)
        self.assertEqual(result.action, "HOLD")

    def test_reaching_the_trigger_moves_to_breakeven_and_is_durable(self):
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)  # entry=100, stop=99, risk_distance=1
        obs_id = observe(conn, trade_id, bid=101.0, ask=101.2)  # r=1.0 on bid (LONG uses bid)
        result = record_decision(conn, observation_id=obs_id, event_id="e1", now_utc=NOW)
        self.assertEqual(result.status, "RECORDED")
        self.assertEqual(result.action, "MOVE_TO_BREAKEVEN")
        row = conn.tables["trade_management.trade_manager_decision"][result.decision_id]
        self.assertEqual(parameters_of(row)["new_stop"], 100.0)
        self.assertEqual(row["tm_version_id"], BREAKEVEN_ID)

    def test_a_non_hold_decision_is_still_withheld_by_the_publication_gate(self):
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)
        obs_id = observe(conn, trade_id, bid=101.0, ask=101.2)
        result = record_decision(conn, observation_id=obs_id, event_id="e1", now_utc=NOW)
        self.assertEqual(result.action, "MOVE_TO_BREAKEVEN")
        gate_row = conn.tables["trade_management.publication_decision"][result.decision_id]
        self.assertEqual(gate_row["outcome"], "WITHHELD")
        self.assertEqual(gate_row["reason"], "TM_VERSION_NOT_PUBLISHABLE")

    def test_second_observation_after_breakeven_can_advance_the_trail(self):
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)
        first_obs = observe(conn, trade_id, bid=101.0, ask=101.2, ts="2026-09-22T12:00:00.000Z")
        first = record_decision(conn, observation_id=first_obs, event_id="e1", now_utc=NOW)
        self.assertEqual(first.action, "MOVE_TO_BREAKEVEN")

        second_obs = observe(conn, trade_id, bid=101.5, ask=101.7, ts="2026-09-22T12:05:00.000Z")
        second = record_decision(conn, observation_id=second_obs, event_id="e2", now_utc=NOW)
        self.assertEqual(second.status, "RECORDED")
        self.assertEqual(second.action, "TRAIL_STOP")
        row = conn.tables["trade_management.trade_manager_decision"][second.decision_id]
        self.assertEqual(parameters_of(row)["new_stop"], 101.0)  # 101.5 - 0.5*1.0

    def test_no_execution_or_broker_table_is_ever_touched(self):
        conn = FakeConnection()
        trade_id = make_breakeven_trail_trade(conn)
        obs_id = observe(conn, trade_id, bid=101.0, ask=101.2)
        record_decision(conn, observation_id=obs_id, event_id="e1", now_utc=NOW)
        for table in conn.tables:
            self.assertNotIn("execution", table)
            self.assertNotIn("broker", table)

    def test_two_trades_never_share_stop_state(self):
        conn = FakeConnection()
        trade_a = make_breakeven_trail_trade(conn, signal_id="SIG_A", strategy_id="STRAT_A_BE")
        trade_b = make_breakeven_trail_trade(conn, signal_id="SIG_B", strategy_id="STRAT_B_BE")
        obs_a = observe(conn, trade_a, bid=101.0, ask=101.2, ts="2026-09-22T12:00:00.000Z")
        result_a = record_decision(conn, observation_id=obs_a, event_id="ea", now_utc=NOW)
        self.assertEqual(result_a.action, "MOVE_TO_BREAKEVEN")

        obs_b = observe(conn, trade_b, bid=100.3, ask=100.5, ts="2026-09-22T12:00:00.000Z")
        result_b = record_decision(conn, observation_id=obs_b, event_id="eb", now_utc=NOW)
        self.assertEqual(result_b.action, "HOLD")  # trade_b's own stop is untouched by trade_a's move


class UnknownEvaluatorTests(unittest.TestCase):
    def test_a_binding_to_an_unregistered_evaluator_fails_closed_and_records_nothing(self):
        conn = FakeConnection()
        conn.seed_tm_version(tm_version_id="TMV_unknown000000000000", evaluator_id="tm-not-implemented.v1",
                             manifest_hash="unknown", manifest={})
        conn.seed_entry_signal(signal_id="SIG_X", strategy_id="STRAT_X", strategy_version="V1",
                               strategy_ref="STRAT_X@V1", parameter_set_ref=None,
                               parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID", strategy_instance_id="inst-1",
                               instrument="XAUUSD", direction="LONG", decision_time="2026-09-22T11:00:00Z",
                               entry_price=100.0, stop_price=99.0, risk_distance=1.0, target_price=103.0,
                               entry_signal_hash="HASH_SIG_X")
        conn.seed_legacy_binding(binding_id="BIND_X", strategy_id="STRAT_X", tm_version_id="TMV_unknown000000000000",
                                 valid_from="2026-09-01T00:00:00Z", binding_hash="h")
        resolver = ChainedResolver([LegacyStaticResolver(), DefaultTmNoneResolver(tm_version_id=TM_NONE_ID)])
        created = create_managed_trade(conn, event_id="evt-x", signal_id="SIG_X", resolver=resolver, now_utc=NOW)
        self.assertEqual(created.status, "CREATED")
        obs_id = observe(conn, created.managed_trade_id)

        with self.assertRaises(UnknownEvaluator):
            record_decision(conn, observation_id=obs_id, event_id="e1", now_utc=NOW)
        self.assertEqual(len(conn.tables["trade_management.trade_manager_decision"]), 0)
        self.assertEqual(conn.rollbacks, 1)

    def test_decision_for_missing_observation_is_reported_not_raised(self):
        conn = FakeConnection()
        result = record_decision(conn, observation_id="TOBS_doesnotexist00000000", event_id="e1", now_utc=NOW)
        self.assertEqual(result.status, "OBSERVATION_MISSING")


if __name__ == "__main__":
    unittest.main()
