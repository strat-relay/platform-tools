"""Real-PostgreSQL proof of the live Trade Manager lifecycle (fixtures only, no broker):

    V2 FILLED result with broker_position_id  ->  ManagedTrade linked via entry_signal_id
    -> broker read shows position open        ->  ACTIVE, observations populate the card
    -> broker read no longer shows it          ->  leaves ACTIVE, retained as CLOSED history

and that a ManagedTrade with no broker position id is never ACTIVE. The ManagedTrade and its
observations are produced by the real `create_managed_trade` / `record_observation`; the V2
rows are inserted exactly as `execution_v2/worker.py::_persist_result` writes them. Skipped
when no PostgreSQL is reachable (set TRADING_POSTGRES_DSN).
"""
from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone

from postgres.config import PostgresConfig
from postgres.db import apply_migrations, connect

from execution_v2.ids import attempt_id as _attempt_id
from execution_v2.ids import execution_intent_id as _intent_id
from execution_v2.ids import execution_result_id as _result_id
from platform_api.control import PlatformControlRepository
from platform_api.trade_manager_live import LIVE, TradeManagerLiveProjection
from trade_management.binding import DefaultTmNoneResolver
from trade_management.managed_trade import create_managed_trade
from trade_management.market_data import MarketQuote
from trade_management.observation import record_observation

TM_NONE_1 = "TMV_ecaca5f080f9f79bb18cc936"
ACCOUNT = "188428665"


def _database_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


class FakeReadOnlyBridge:
    def __init__(self):
        self.positions: list[dict] = []
        self.calls: list[str] = []

    def call(self, tool, arguments=None):
        self.calls.append(tool)
        return {"mt5_account_info": {"login": int(ACCOUNT), "type": 2},
                "mt5_positions": self.positions, "mt5_orders": []}[tool]


@unittest.skipUnless(_database_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class LiveTradeManagerRealPostgresTests(unittest.TestCase):
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
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def _signal(self, *, instrument="XAUUSD", direction="LONG") -> str:
        signal_id = f"SIG_{uuid.uuid4().hex[:12]}"
        evaluation_id = f"EVAL_{uuid.uuid4().hex[:12]}"
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.evaluations
                (evaluation_id, strategy_id, instrument, decision_time, decision, trace_fidelity,
                 runtime_version, evaluator_version, canonical_payload, canonical_hash)
                VALUES (%s,'STRAT1',%s, %s, 'SIGNAL', 'L1', 'test', 'test', '{}'::jsonb, %s)""",
                        (evaluation_id, instrument, self.now - timedelta(minutes=15), f"HASH_{uuid.uuid4().hex}"))
            cur.execute("""INSERT INTO strategy.entry_signals
                (signal_id, candidate_id, evaluation_id, strategy_ref, strategy_id, strategy_version,
                 instrument, direction, decision_time, entry_price, stop_price, target_price,
                 evaluation_hash, trace_hash, terminal_state, entry_signal_hash)
                VALUES (%s,%s,%s,'strat-ref','STRAT1','1',%s,%s,%s, 2000.0, 1990.0, 2020.0,
                        'evalhash','tracehash','SIGNAL',%s)""",
                        (signal_id, f"CAND_{uuid.uuid4().hex[:8]}", evaluation_id, instrument, direction,
                         self.now - timedelta(minutes=15), f"hash_{uuid.uuid4().hex}"))
        self.conn.commit()
        return signal_id

    def _managed_trade(self, signal_id: str) -> str:
        result = create_managed_trade(self.conn, event_id=f"evt-{signal_id}", signal_id=signal_id,
                                      resolver=DefaultTmNoneResolver(tm_version_id=TM_NONE_1),
                                      now_utc=self.now - timedelta(minutes=14))
        self.assertEqual(result.status, "CREATED")
        return result.managed_trade_id

    def _v2_fill(self, signal_id: str, *, position_id: str | None) -> None:
        intent_id = _intent_id(entry_signal_id=signal_id, account_id=ACCOUNT)
        att_id = _attempt_id(execution_intent_id=intent_id)
        confirmed = self.now - timedelta(minutes=10)
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO execution_v2.execution_intent
                (execution_intent_id, entry_signal_id, entry_signal_hash, strategy_id, strategy_version,
                 strategy_ref, instrument, direction, stop_price, target_price, approved_volume, account_id,
                 idempotency_key, status)
                SELECT %s, signal_id, entry_signal_hash, strategy_id, strategy_version, strategy_ref,
                       instrument, direction, stop_price, target_price, 0.01, %s, %s, 'COMPLETED'
                FROM strategy.entry_signals WHERE signal_id = %s""",
                        (intent_id, ACCOUNT, f"idem-{intent_id}", signal_id))
            cur.execute("""INSERT INTO execution_v2.execution_attempt
                (attempt_id, execution_intent_id, account_id, resource, generation, state, terminal_at)
                VALUES (%s,%s,%s,%s,1,'CONFIRMED',%s)""",
                        (att_id, intent_id, ACCOUNT, f"execution:real:{ACCOUNT}", confirmed))
            cur.execute("""INSERT INTO execution_v2.execution_result
                (execution_result_id, attempt_id, execution_intent_id, outcome, account_id, broker_order_id,
                 broker_deal_id, broker_position_id, symbol, volume, actual_price, submitted_at, confirmed_at)
                VALUES (%s,%s,%s,'FILLED',%s,%s,%s,%s,'XAUUSD',0.01,2001.0,%s,%s)""",
                        (_result_id(attempt_id=att_id), att_id, intent_id, ACCOUNT, position_id,
                         f"D{position_id}", position_id, confirmed, confirmed))
        self.conn.commit()

    def _observe(self, managed_trade_id: str, *, bid: float, at: datetime) -> None:
        quote = MarketQuote(instrument="XAUUSD", bid=bid, ask=bid + 0.4,
                            source_timestamp=at.isoformat().replace("+00:00", "Z"), provider_id="test-feed")
        self.assertEqual(record_observation(self.conn, managed_trade_id=managed_trade_id, quote=quote,
                                            bars=None, now_utc=at).status, "RECORDED")

    def _project(self, bridge):
        return TradeManagerLiveProjection(PlatformControlRepository(), bridge, clock=lambda: self.now).project()

    def test_real_fill_to_active_to_closed_history(self):
        position_id = str(900000 + int(uuid.uuid4().int % 99999))
        signal_id = self._signal()
        trade_id = self._managed_trade(signal_id)
        # Observation before the fill must not count toward MFE/MAE.
        self._observe(trade_id, bid=2050.0, at=self.now - timedelta(minutes=12))
        self._v2_fill(signal_id, position_id=position_id)
        self._observe(trade_id, bid=2011.0, at=self.now - timedelta(minutes=6))
        self._observe(trade_id, bid=1996.0, at=self.now - timedelta(minutes=3))
        self._observe(trade_id, bid=2006.0, at=self.now - timedelta(seconds=30))

        historical_trade = self._managed_trade(self._signal())  # internal OPEN, never executed

        bridge = FakeReadOnlyBridge()
        bridge.positions = [{"ticket": int(position_id), "symbol": "XAUUSDm", "type": 0, "volume": 0.01,
                             "price_open": 2001.0, "sl": 1990.0, "tp": 2020.0, "profit": 5.0, "time": 0}]
        live = self._project(bridge)
        active = {t["managed_trade_id"]: t for t in live["active"]}
        self.assertIn(trade_id, active)
        self.assertNotIn(historical_trade, active)
        card = active[trade_id]
        self.assertEqual(card["broker"]["position_id"], position_id)
        self.assertEqual(card["environment"], "REAL")
        self.assertEqual(card["current_price"], 2006.0)
        self.assertEqual(card["current_stop"], 1990.0)
        self.assertEqual(card["age_seconds"], 600)
        self.assertEqual(card["observation"]["status"], "FRESH")
        self.assertEqual(card["observation"]["count_since_open"], 3)
        self.assertAlmostEqual(card["excursion"]["mfe_price"], 10.0)
        self.assertAlmostEqual(card["excursion"]["mae_price"], -5.0)
        self.assertGreaterEqual(live["historical_unreconciled"]["by_linkage"].get("NO_EXECUTION_INTENT", 0), 1)

        bridge.positions = []
        after = self._project(bridge)
        self.assertNotIn(trade_id, {t["managed_trade_id"] for t in after["active"]})
        closed = {t["managed_trade_id"]: t for t in after["closed"]}
        self.assertEqual(closed[trade_id]["broker_status"], "CLOSED")
        with self.conn.cursor() as cur:
            cur.execute("SELECT state FROM trade_management.managed_trade WHERE managed_trade_id = %s", (trade_id,))
            self.assertEqual(cur.fetchone()[0], "OPEN")  # history is never rewritten by the projection
        self.conn.rollback()
        self.assertEqual(set(bridge.calls), {"mt5_account_info", "mt5_positions", "mt5_orders"})

    def test_fill_without_position_id_is_not_linked(self):
        signal_id = self._signal()
        trade_id = self._managed_trade(signal_id)
        self._v2_fill(signal_id, position_id=None)
        bridge = FakeReadOnlyBridge()
        live = self._project(bridge)
        ids = {t["managed_trade_id"] for t in live["active"] + live["closed"] + live["unresolved"]}
        self.assertNotIn(trade_id, ids)
        self.assertGreaterEqual(live["historical_unreconciled"]["by_linkage"]["BROKER_RESULT_WITHOUT_POSITION_ID"], 1)

    def test_zero_broker_positions_is_zero_active(self):
        self._managed_trade(self._signal())
        live = self._project(FakeReadOnlyBridge())
        self.assertEqual(live["summary"]["active_trades"], 0)
        self.assertEqual(live["system_state"], LIVE)


if __name__ == "__main__":
    unittest.main()
