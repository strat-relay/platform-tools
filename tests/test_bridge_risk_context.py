"""Bridge risk path with open positions (execution_v2/runtime/bridge_client.read_risk_context).

Before: any open broker position aborted risk construction ("open-position exposure cannot be
calculated safely" -> RISK_STATE_UNAVAILABLE), so one open trade - even a manual one - blocked
every signal. Now platform positions (their ticket is a V2 fill's broker order ticket) count toward
concurrent positions and their stop risk toward account exposure; manual positions are excluded
from both; FREE_MARGIN sizing applies risk_per_trade to free margin; and a failed risk read records
its reason in the risk evidence.
"""
from __future__ import annotations

import json
import math
import unittest
from contextlib import contextmanager
from unittest import mock

from execution_v2.intent import create_execution_intent
from execution_v2.runtime.bridge_client import HttpBridgeFenceClient, position_stop_risk
from execution_v2.runtime.service import platform_broker_tickets
from test_risk_state import ACCOUNT, BTC_POSITION, ETH_SIGNAL, NOW, REFERENCE, policy
from execution_v2.fakes import FakeConnection

MANUAL_XAU = {"ticket": 257422615, "symbol": "XAUUSDm", "type": 0, "volume": 0.02, "price_open": 4174.671,
              "sl": 0.0, "tp": 0.0, "profit": -4.08, "time": 1790577861}
PLATFORM_TICKETS = frozenset({str(BTC_POSITION["ticket"])})
# ETH signal: 5.014 USD of risk per lot (5.014 / 0.01 tick * 0.01 tick value).
ETH_PER_LOT = (ETH_SIGNAL["entry_price"] - ETH_SIGNAL["stop_price"]) / 0.01 * 0.01


class Broker:
    def __init__(self, positions=(), orders=(), equity=430.0, free_margin=100.0):
        self.positions, self.orders, self.equity, self.free_margin = list(positions), list(orders), equity, free_margin

    def __call__(self, tool, arguments):
        account = {"login": int(ACCOUNT), "equity": self.equity, "balance": self.equity}
        if self.free_margin is not None:
            account["free_margin"] = self.free_margin
        return {"mt5_account_info": account, "mt5_positions": self.positions, "mt5_orders": self.orders,
                "mt5_history": [], "mt5_symbol_info": REFERENCE.get((arguments or {}).get("symbol"))}[tool]


class BridgeRiskContextTests(unittest.TestCase):
    @contextmanager
    def client(self, broker: Broker):
        client = HttpBridgeFenceClient(base_url="http://bridge:22348/mcp", read_base_url="http://bridge:22347/mcp")
        with mock.patch.object(HttpBridgeFenceClient, "_read_tool", side_effect=lambda tool, args: broker(tool, args)):
            yield client

    def evaluate(self, broker: Broker, *, basis="EQUITY", tickets=PLATFORM_TICKETS, **policy_overrides):
        conn = FakeConnection()
        conn.seed_entry_signal(**ETH_SIGNAL)
        with self.client(broker) as client:
            result = create_execution_intent(
                conn, signal_id=ETH_SIGNAL["signal_id"], account_id=ACCOUNT, now_utc=NOW,
                risk_policy=policy(**{"max_concurrent_positions": 20, "max_volume": 100.0, **policy_overrides}),
                risk_context_provider=lambda r: client.read_risk_context(broker_symbol="ETHUSDm", as_of=NOW,
                                                                         platform_tickets=tickets, sizing_basis=basis))
        evidence = next(iter(conn.view("execution_v2.execution_risk_evidence").values()), None)
        intent = next(iter(conn.view("execution_v2.execution_intent").values()))
        return result, intent, evidence, json.loads(evidence["diagnostics"]) if evidence else None

    def test_manual_position_without_a_stop_no_longer_blocks(self):
        result, intent, evidence, diag = self.evaluate(Broker(positions=[MANUAL_XAU]))
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(evidence["concurrent_positions_used"], 0)
        self.assertEqual(diag["positions"], {"platform": 0, "manual": 1, "platform_orders": 0, "manual_orders": 0,
                                             "exposure_unbounded": False})
        self.assertEqual(diag["account_exposure"], 0.0)

    def test_platform_position_counts_toward_slots_and_exposure(self):
        client_broker = Broker(positions=[BTC_POSITION, MANUAL_XAU])
        with self.client(client_broker) as client:
            ctx = client.read_risk_context(broker_symbol="ETHUSDm", as_of=NOW, platform_tickets=PLATFORM_TICKETS)
        btc_risk = 0.53 * (84130.2 - 84042.38) / 0.01 * 0.01
        self.assertEqual(ctx["state"]["concurrent_positions"], 1)
        self.assertAlmostEqual(ctx["state"]["account_exposure"], btc_risk)
        result, _, _, _ = self.evaluate(client_broker)
        self.assertEqual(result.status, "CREATED")                   # 1 of 20 slots, 46.5 of 1000 exposure
        blocked, _, _, _ = self.evaluate(client_broker, max_concurrent_positions=1)
        self.assertEqual(blocked.reason, "MAX_CONCURRENT_POSITIONS_EXCEEDED")
        capped, _, _, _ = self.evaluate(client_broker, max_account_exposure=40.0)
        self.assertEqual(capped.reason, "MAX_ACCOUNT_EXPOSURE_EXCEEDED")

    def test_platform_position_without_a_stop_is_unbounded_exposure(self):
        unstopped = {**BTC_POSITION, "sl": 0.0}
        result, _, _, diag = self.evaluate(Broker(positions=[unstopped]))
        self.assertEqual(result.reason, "MAX_ACCOUNT_EXPOSURE_EXCEEDED")
        self.assertIsNone(diag["account_exposure"])                 # JSON-safe, flagged instead
        self.assertTrue(diag["account_exposure_unbounded"])

    def test_free_margin_sizing_uses_free_margin(self):
        eq, eq_intent, eq_ev, _ = self.evaluate(Broker(equity=430.0, free_margin=100.0), basis="EQUITY")
        fm, fm_intent, fm_ev, diag = self.evaluate(Broker(equity=430.0, free_margin=100.0), basis="FREE_MARGIN")
        self.assertAlmostEqual(float(eq_intent["approved_volume"]), math.floor(430.0 * 0.20 / ETH_PER_LOT * 10) / 10)
        self.assertAlmostEqual(float(fm_intent["approved_volume"]), math.floor(100.0 * 0.20 / ETH_PER_LOT * 10) / 10)
        self.assertAlmostEqual(float(fm_ev["risk_budget_usd"]), 20.0)
        self.assertEqual((diag["sizing_basis"], diag["sizing_capital"]), ("FREE_MARGIN", 100.0))

    def test_missing_free_margin_fails_closed_and_records_why(self):
        result, _, evidence, diag = self.evaluate(Broker(free_margin=None), basis="FREE_MARGIN")
        self.assertEqual((result.status, result.reason), ("BLOCKED", "RISK_STATE_UNAVAILABLE"))
        self.assertEqual(diag["risk_context_failure"]["stage"], "BRIDGE_RISK_CONTEXT")
        self.assertIn("free margin", diag["risk_context_failure"]["error"])

    def test_manual_pending_order_does_not_take_the_order_slot(self):
        manual_order = {"ticket": 999, "symbol": "EURUSDm", "type": 2, "volume": 0.01}
        result, _, evidence, _ = self.evaluate(Broker(orders=[manual_order]))
        self.assertEqual(result.status, "CREATED")
        self.assertEqual(evidence["concurrent_orders_used"], 0)

    def test_stop_risk_is_adverse_distance_only(self):
        spec = {"tick_size": 0.01, "tick_value": 0.01}
        self.assertAlmostEqual(position_stop_risk({"volume": 1, "price_open": 100, "sl": 99, "type": 0}, spec), 1.0)
        self.assertEqual(position_stop_risk({"volume": 1, "price_open": 100, "sl": 101, "type": 0}, spec), 0.0)   # stop in profit
        self.assertAlmostEqual(position_stop_risk({"volume": 1, "price_open": 100, "sl": 102, "type": 1}, spec), 2.0)
        self.assertEqual(position_stop_risk({"volume": 1, "price_open": 100, "sl": 0, "type": 1}, spec), math.inf)

    def test_platform_tickets_come_from_v2_results(self):
        rows = [("5001", None), (None, "7001"), ("5002", "5002")]
        cur = mock.MagicMock()
        cur.fetchall.return_value = rows
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        tickets = platform_broker_tickets(lambda readonly=False: conn, ACCOUNT)
        self.assertEqual(tickets, frozenset({"5001", "7001", "5002"}))
        sql, params = cur.execute.call_args[0]
        self.assertIn("NOT IN ('REJECTED', 'BLOCKED')", sql)
        self.assertEqual(params, (ACCOUNT,))


if __name__ == "__main__":
    unittest.main()
