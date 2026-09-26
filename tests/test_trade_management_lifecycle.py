"""ManagedTrade terminal lifecycle (migration 026, trade_management/lifecycle.py).

The strategy owns outcome: a ManagedTrade closes only when strategy.entry_signal_outcomes is
TARGET_HIT or STOPPED for its EntrySignal. The Trade Manager never evaluates TP/SL itself, never
needs broker identity to observe a trade, and never rewrites observations or decisions.
"""
from __future__ import annotations

import ast
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from postgres.config import PostgresConfig
from postgres.db import apply_migrations, connect

from trade_management.binding import DefaultTmNoneResolver
from trade_management.decision_engine import record_decision
from trade_management.fakes import FakeConnection
from trade_management.lifecycle import (REASON_STRATEGY_OUTCOME, close_terminal_trades, lifecycle_event_id,
                                        reconcile_strategy_outcomes)
from trade_management.managed_trade import create_managed_trade
from trade_management.market_data import FakeMarketDataProvider, MarketQuote
from trade_management.mode import OFF, SHADOW
from trade_management.observation import record_observation
from trade_management.runtime.fakes import RuntimeFakeConnection
from trade_management.runtime.observation_runtime import observation_tick
from trade_management.versions import TM_NONE_1_MANIFEST

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
EXIT = datetime(2026, 9, 26, 11, 30, 0, tzinfo=timezone.utc)
TM_NONE_ID = TM_NONE_1_MANIFEST.tm_version_id()
ROOT = Path(__file__).resolve().parents[1]
EVENTS = "trade_management.managed_trade_lifecycle_event"
TRADES = "trade_management.managed_trade"


def seed_signal(conn, signal_id, *, instrument="XAUUSD", direction="LONG", stop=99.0, target=103.0):
    conn.seed_tm_version(tm_version_id=TM_NONE_ID, manifest_hash=TM_NONE_1_MANIFEST.manifest_hash())
    conn.seed_entry_signal(signal_id=signal_id, strategy_id="CONTEXT_STRUCTURE_RETRACE_V1", strategy_version="V1",
                           strategy_ref="CONTEXT_STRUCTURE_RETRACE_V1@V1", parameter_set_ref=None,
                           parameter_set_status="LEGACY_IMPLICIT_IN_STRATEGY_ID", strategy_instance_id="phase6",
                           instrument=instrument, direction=direction, decision_time="2026-09-26T11:00:00Z",
                           entry_price=100.0, stop_price=stop, risk_distance=abs(100.0 - stop), target_price=target,
                           entry_signal_hash=f"HASH_{signal_id}")


def create(conn, signal_id):
    return create_managed_trade(conn, event_id=f"evt-{signal_id}", signal_id=signal_id,
                                resolver=DefaultTmNoneResolver(tm_version_id=TM_NONE_ID), now_utc=NOW)


def quote(bid, ts, instrument="XAUUSD"):
    return MarketQuote(instrument=instrument, bid=bid, ask=bid + 0.2, source_timestamp=ts, provider_id="fake")


class Publisher:
    def __init__(self):
        self.published = []

    async def publish(self, envelope):
        self.published.append(envelope)


class TerminalTransitionTests(unittest.TestCase):
    def open_trade(self, conn, signal_id="SIG_1"):
        seed_signal(conn, signal_id)
        result = create(conn, signal_id)
        self.assertEqual((result.status, result.reason), ("CREATED", None))
        return result.managed_trade_id

    def test_target_hit_closes_with_exit_metadata_and_reason(self):  # 1, 3, 4, 5
        conn = FakeConnection()
        trade_id = self.open_trade(conn)
        conn.seed_strategy_outcome(signal_id="SIG_1", status="TARGET_HIT", exit_timestamp=EXIT, realized_r=0.74)
        [transition] = reconcile_strategy_outcomes(conn, now_utc=NOW)
        self.assertEqual(conn.tables[TRADES][trade_id]["state"], "CLOSED")
        event = conn.tables[EVENTS][transition.lifecycle_event_id]
        self.assertEqual(event, {
            "lifecycle_event_id": lifecycle_event_id(managed_trade_id=trade_id, new_state="CLOSED"),
            "managed_trade_id": trade_id, "entry_signal_id": "SIG_1", "previous_state": "OPEN",
            "new_state": "CLOSED", "reason": REASON_STRATEGY_OUTCOME, "strategy_outcome": "TARGET_HIT",
            "outcome_source": "CONTEXT_STRUCTURE_RETRACE_V1", "exit_timestamp": EXIT, "realized_r": 0.74,
            "transitioned_at": NOW})

    def test_stopped_closes(self):  # 2
        conn = FakeConnection()
        trade_id = self.open_trade(conn)
        conn.seed_strategy_outcome(signal_id="SIG_1", status="STOPPED", exit_timestamp=EXIT, realized_r=-1.0)
        reconcile_strategy_outcomes(conn, now_utc=NOW)
        self.assertEqual(conn.tables[TRADES][trade_id]["state"], "CLOSED")
        [event] = conn.tables[EVENTS].values()
        self.assertEqual((event["strategy_outcome"], event["realized_r"]), ("STOPPED", -1.0))

    def test_time_exit_closes_without_context_behavior_change(self):
        conn = FakeConnection()
        trade_id = self.open_trade(conn)
        conn.seed_strategy_outcome(signal_id="SIG_1", status="TIME_EXIT", exit_timestamp=EXIT, realized_r=0.25)
        [transition] = reconcile_strategy_outcomes(conn, now_utc=NOW)
        self.assertEqual(conn.tables[TRADES][trade_id]["state"], "CLOSED")
        self.assertEqual(transition.strategy_outcome, "TIME_EXIT")

    def test_reconciliation_is_idempotent(self):  # 6
        conn = FakeConnection()
        self.open_trade(conn)
        conn.seed_strategy_outcome(signal_id="SIG_1", status="TARGET_HIT", exit_timestamp=EXIT, realized_r=0.5)
        self.assertEqual(len(reconcile_strategy_outcomes(conn, now_utc=NOW)), 1)
        for _ in range(3):
            self.assertEqual(reconcile_strategy_outcomes(conn, now_utc=NOW + timedelta(minutes=5)), [])
        self.assertEqual(len(conn.tables[EVENTS]), 1)
        self.assertEqual(next(iter(conn.tables[EVENTS].values()))["transitioned_at"], NOW)

    def test_unresolved_outcomes_stay_open(self):  # 12
        conn = FakeConnection()
        missing = self.open_trade(conn, "SIG_MISSING")          # no outcome row at all
        still_open = self.open_trade(conn, "SIG_OPEN")
        conn.seed_strategy_outcome(signal_id="SIG_OPEN", status="OPEN")
        self.assertEqual(reconcile_strategy_outcomes(conn, now_utc=NOW), [])
        self.assertEqual(conn.tables[TRADES][missing]["state"], "OPEN")
        self.assertEqual(conn.tables[TRADES][still_open]["state"], "OPEN")
        self.assertEqual(conn.tables.get(EVENTS, {}), {})

    def test_signal_already_terminal_at_creation_is_closed_immediately(self):  # 15
        conn = FakeConnection()
        seed_signal(conn, "SIG_EARLY")
        conn.seed_strategy_outcome(signal_id="SIG_EARLY", status="TARGET_HIT", exit_timestamp=EXIT, realized_r=0.3)
        result = create(conn, "SIG_EARLY")
        self.assertEqual((result.status, result.reason), ("CREATED", "CLOSED_ON_CREATION_STRATEGY_OUTCOME"))
        self.assertEqual(conn.tables[TRADES][result.managed_trade_id]["state"], "CLOSED")
        self.assertEqual(len(conn.tables[EVENTS]), 1)
        # Creation still emits trade.opened.v1 (the trade existed); it is simply already terminal.
        self.assertIn(f"{result.managed_trade_id}:trade.opened", conn.tables["platform.outbox_events"])


class WorkSelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = RuntimeFakeConnection()
        self.provider = FakeMarketDataProvider()
        self.publisher = Publisher()

    def trade(self, signal_id, **kw):
        seed_signal(self.conn, signal_id, **kw)
        return create(self.conn, signal_id).managed_trade_id

    def observations_for(self, trade_id):
        return [o for o in self.conn.tables.get("trade_management.trade_observation", {}).values()
                if o["managed_trade_id"] == trade_id]

    async def tick(self, bid, ts):
        self.provider.set_quote("XAUUSD", quote(bid, ts))
        return await observation_tick(self.conn, self.publisher, self.provider)

    async def test_closed_trade_leaves_the_work_set_and_open_virtual_trade_stays(self):  # 7, 9, 10
        terminal = self.trade("SIG_T")
        active = self.trade("SIG_A")
        summary = await self.tick(101.0, "2026-09-26T12:00:00Z")
        self.assertEqual((summary["open_trades"], summary["observations_recorded"]), (2, 2))

        self.conn.seed_strategy_outcome(signal_id="SIG_T", status="TARGET_HIT", exit_timestamp=EXIT, realized_r=1.0)
        summary = await self.tick(101.5, "2026-09-26T12:00:30Z")
        self.assertEqual(summary["closed_by_strategy_outcome"], 1)
        self.assertEqual((summary["open_trades"], summary["observations_recorded"]), (1, 1))
        self.assertEqual(len(self.observations_for(terminal)), 1)   # nothing after close
        self.assertEqual(len(self.observations_for(active)), 2)     # virtual trade, no broker identity
        self.assertFalse(any(t.startswith("execution") for t in self.conn.tables))

    async def test_tm_never_derives_outcome_from_prices(self):  # 13
        trade_id = self.trade("SIG_X", stop=99.0, target=103.0)
        self.conn.seed_strategy_outcome(signal_id="SIG_X", status="OPEN")
        for i, bid in enumerate((95.0, 110.0)):   # through the stop, then through the target
            await self.tick(bid, f"2026-09-26T12:0{i}:00Z")
        self.assertEqual(self.conn.tables[TRADES][trade_id]["state"], "OPEN")
        self.assertEqual(self.conn.tables.get(EVENTS, {}), {})

    async def test_history_is_untouched_by_closing(self):  # 14
        trade_id = self.trade("SIG_H")
        await self.tick(101.0, "2026-09-26T12:00:00Z")
        [obs] = self.observations_for(trade_id)
        record_decision(self.conn, observation_id=obs["observation_id"], event_id=obs["observation_id"], now_utc=NOW)
        before = {t: dict(self.conn.tables.get(t, {})) for t in (
            "trade_management.trade_observation", "trade_management.trade_manager_decision",
            "trade_management.publication_decision", "trade_management.market_snapshot")}
        self.conn.seed_strategy_outcome(signal_id="SIG_H", status="STOPPED", exit_timestamp=EXIT, realized_r=-1.0)
        reconcile_strategy_outcomes(self.conn, now_utc=NOW)
        after = {t: dict(self.conn.tables.get(t, {})) for t in before}
        self.assertEqual(before, after)
        self.assertEqual(len(before["trade_management.trade_manager_decision"]), 1)

    async def test_off_does_no_lifecycle_work_and_shadow_resumes_it(self):  # 16
        trade_id = self.trade("SIG_M")
        self.conn.seed_strategy_outcome(signal_id="SIG_M", status="TARGET_HIT", exit_timestamp=EXIT, realized_r=0.9)
        self.conn.set_tm_mode(OFF)
        self.assertEqual(await self.tick(101.0, "2026-09-26T12:00:00Z"), {"mode": OFF, "skipped": True})
        self.assertEqual(self.conn.tables[TRADES][trade_id]["state"], "OPEN")
        self.conn.set_tm_mode(SHADOW, revision=2)
        summary = await self.tick(101.0, "2026-09-26T12:00:30Z")
        self.assertEqual((summary["mode"], summary["closed_by_strategy_outcome"], summary["open_trades"]), (SHADOW, 1, 0))
        self.assertEqual(self.conn.tables[TRADES][trade_id]["state"], "CLOSED")


class DecisionTests(unittest.TestCase):
    def test_observation_queued_before_close_yields_no_decision(self):  # 8
        conn = FakeConnection()
        seed_signal(conn, "SIG_D")
        trade_id = create(conn, "SIG_D").managed_trade_id
        obs = record_observation(conn, managed_trade_id=trade_id, quote=quote(101.0, "2026-09-26T12:00:00Z"),
                                 bars=None, now_utc=NOW)
        conn.seed_strategy_outcome(signal_id="SIG_D", status="TARGET_HIT", exit_timestamp=EXIT, realized_r=0.6)
        reconcile_strategy_outcomes(conn, now_utc=NOW)
        result = record_decision(conn, observation_id=obs.observation_id, event_id=obs.observation_id, now_utc=NOW)
        self.assertEqual(result.status, "TRADE_NOT_OPEN")
        self.assertEqual(conn.tables.get("trade_management.trade_manager_decision", {}), {})
        self.assertEqual(conn.tables["platform.inbox_events"][("trade-manager-shadow", obs.observation_id)]["status"],
                         "PROCESSED")
        after_close = record_observation(conn, managed_trade_id=trade_id, quote=quote(102.0, "2026-09-26T12:01:00Z"),
                                         bars=None, now_utc=NOW)
        self.assertEqual(after_close.status, "TRADE_NOT_OPEN")


class BoundaryTests(unittest.TestCase):
    SOURCE = (ROOT / "trade_management" / "lifecycle.py").read_text()

    def test_lifecycle_reads_only_the_canonical_outcome(self):  # 13
        tree = ast.parse(self.SOURCE)
        for node in ast.walk(tree):  # code only: drop docstrings (comments are already gone)
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        self.assertIn("strategy.entry_signal_outcomes", code)
        for term in ("bid", "ask", "initial_stop", "initial_target", "market_snapshot", "trade_observation"):
            self.assertNotIn(term, code)

    def test_no_broker_path_or_broker_identity_requirement(self):  # 10, 17
        for term in ("execution_v2", "broker_position", "mt5_", "22348", "order_send", "close_position"):
            self.assertNotIn(term, self.SOURCE)

    def test_migration_is_additive_append_only_and_numbered_after_025(self):
        sql = (ROOT / "postgres" / "migrations" / "026_managed_trade_lifecycle.sql").read_text()
        for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
            self.assertNotIn(forbidden, sql)
        self.assertIn("managed_trade_lifecycle_event_append_only", sql)
        self.assertIn("reason IN ('STRATEGY_OUTCOME')", sql)
        numbers = sorted(p.name[:3] for p in (ROOT / "postgres" / "migrations").glob("*.sql"))
        self.assertEqual(numbers.count("026"), 1)


def _database_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


@unittest.skipUnless(_database_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class RealPostgresLifecycleTests(unittest.TestCase):
    ACCOUNT = "188428665"

    @classmethod
    def setUpClass(cls):
        cls.conn = connect(PostgresConfig.from_env())
        apply_migrations(cls.conn)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        self.conn.rollback()

    def _signal(self) -> str:
        signal_id, evaluation_id = f"SIG_{uuid.uuid4().hex[:12]}", f"EVAL_{uuid.uuid4().hex[:12]}"
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.evaluations (evaluation_id, strategy_id, instrument, decision_time,
                decision, trace_fidelity, runtime_version, evaluator_version, canonical_payload, canonical_hash)
                VALUES (%s,'CONTEXT_STRUCTURE_RETRACE_V1','XAUUSD', now(), 'SIGNAL','L1','t','t','{}'::jsonb,%s)""",
                        (evaluation_id, f"H_{uuid.uuid4().hex}"))
            cur.execute("""INSERT INTO strategy.entry_signals (signal_id, candidate_id, evaluation_id, strategy_ref,
                strategy_id, strategy_version, instrument, direction, decision_time, entry_price, stop_price,
                target_price, evaluation_hash, trace_hash, terminal_state, entry_signal_hash)
                VALUES (%s,%s,%s,'ref','CONTEXT_STRUCTURE_RETRACE_V1','V1','XAUUSD','LONG', now(), 2000, 1990, 2020,
                        'e','t','SIGNAL',%s)""",
                        (signal_id, f"C_{uuid.uuid4().hex[:8]}", evaluation_id, f"h_{uuid.uuid4().hex}"))
        self.conn.commit()
        return signal_id

    def _seed_outcome(self, signal_id, status, realized_r=None):
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.entry_signal_outcomes (signal_id, outcome_type, status, realized_r,
                exit_timestamp, source) VALUES (%s,'ENTRY_ONLY',%s,%s,%s,'CONTEXT_STRUCTURE_RETRACE_V1')""",
                        (signal_id, status, realized_r, EXIT if status != "OPEN" else None))
        self.conn.commit()

    def _create(self, signal_id):
        return create_managed_trade(self.conn, event_id=f"evt-{signal_id}", signal_id=signal_id,
                                    resolver=DefaultTmNoneResolver(tm_version_id=TM_NONE_ID), now_utc=NOW)

    def _state(self, trade_id):
        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM trade_management.managed_trade WHERE managed_trade_id=%s", (trade_id,))
            state = cur.fetchone()[0]
        self.conn.rollback()
        return state

    def _events(self, trade_id):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT previous_state, new_state, reason, strategy_outcome, exit_timestamp, realized_r
                          FROM trade_management.managed_trade_lifecycle_event WHERE managed_trade_id=%s""", (trade_id,))
            rows = cur.fetchall()
        self.conn.rollback()
        return rows

    def test_real_transition_idempotency_and_triggers(self):
        signal_id = self._signal()
        trade_id = self._create(signal_id).managed_trade_id
        self._seed_outcome(signal_id, "STOPPED", -1.0)
        with self.conn.cursor() as cur:
            cur.execute("SELECT 1")
        self.conn.rollback()
        transitions = [t for t in reconcile_strategy_outcomes(self.conn, now_utc=NOW) if t.managed_trade_id == trade_id]
        self.assertEqual(len(transitions), 1)
        self.assertFalse([t for t in reconcile_strategy_outcomes(self.conn, now_utc=NOW) if t.managed_trade_id == trade_id])
        self.assertEqual(self._state(trade_id), "CLOSED")
        [(prev, new, reason, outcome, exit_ts, realized_r)] = self._events(trade_id)
        self.assertEqual((prev, new, reason, outcome, exit_ts, float(realized_r)),
                         ("OPEN", "CLOSED", "STRATEGY_OUTCOME", "STOPPED", EXIT, -1.0))
        for statement in ("UPDATE trade_management.managed_trade_lifecycle_event SET reason = reason WHERE managed_trade_id = %s",
                          "DELETE FROM trade_management.managed_trade_lifecycle_event WHERE managed_trade_id = %s",
                          "UPDATE trade_management.managed_trade SET state = 'OPEN' WHERE managed_trade_id = %s"):
            with self.assertRaises(Exception, msg=statement):
                with self.conn.cursor() as cur:
                    cur.execute(statement, (trade_id,))
            self.conn.rollback()

    def test_real_terminal_at_creation_and_open_outcome(self):
        early = self._signal()
        self._seed_outcome(early, "TARGET_HIT", 0.74)
        result = self._create(early)
        self.assertEqual((result.reason, self._state(result.managed_trade_id)),
                         ("CLOSED_ON_CREATION_STRATEGY_OUTCOME", "CLOSED"))
        pending = self._signal()
        self._seed_outcome(pending, "OPEN")
        trade_id = self._create(pending).managed_trade_id
        reconcile_strategy_outcomes(self.conn, now_utc=NOW)
        self.assertEqual(self._state(trade_id), "OPEN")
        self.assertEqual(self._events(trade_id), [])

    def test_real_broker_backed_trade_uses_the_same_lifecycle(self):  # 11
        signal_id = self._signal()
        trade_id = self._create(signal_id).managed_trade_id
        intent = f"EI_{uuid.uuid4().hex[:12]}"
        attempt = f"EA_{uuid.uuid4().hex[:12]}"
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO execution_v2.execution_intent (execution_intent_id, entry_signal_id,
                entry_signal_hash, strategy_id, strategy_version, strategy_ref, instrument, direction, stop_price,
                target_price, approved_volume, account_id, idempotency_key, status)
                SELECT %s, signal_id, entry_signal_hash, strategy_id, strategy_version, strategy_ref, instrument,
                       direction, stop_price, target_price, 0.01, %s, %s, 'COMPLETED'
                FROM strategy.entry_signals WHERE signal_id=%s""", (intent, self.ACCOUNT, f"k-{intent}", signal_id))
            cur.execute("""INSERT INTO execution_v2.execution_attempt (attempt_id, execution_intent_id, account_id,
                resource, generation, state, terminal_at) VALUES (%s,%s,%s,%s,1,'CONFIRMED',now())""",
                        (attempt, intent, self.ACCOUNT, f"execution:real:{self.ACCOUNT}"))
            cur.execute("""INSERT INTO execution_v2.execution_result (execution_result_id, attempt_id,
                execution_intent_id, outcome, account_id, broker_order_id, broker_deal_id, broker_position_id,
                symbol, volume, actual_price, submitted_at, confirmed_at)
                VALUES (%s,%s,%s,'FILLED',%s,'1','2','3','XAUUSD',0.01,2000,now(),now())""",
                        (f"ER_{uuid.uuid4().hex[:12]}", attempt, intent, self.ACCOUNT))
        self.conn.commit()
        reconcile_strategy_outcomes(self.conn, now_utc=NOW)
        self.assertEqual(self._state(trade_id), "OPEN")        # still managed while the strategy says OPEN
        self._seed_outcome(signal_id, "TARGET_HIT", 1.2)
        reconcile_strategy_outcomes(self.conn, now_utc=NOW)
        self.assertEqual(self._state(trade_id), "CLOSED")


if __name__ == "__main__":
    unittest.main()
