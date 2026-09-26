"""Live, broker-backed Trade Manager projection (platform_api/trade_manager_live.py).

Broker truth comes only from the read-only bridge; ManagedTrade rows alone never make a trade
ACTIVE. Every test also asserts that no non-read bridge tool was ever called.
"""
from __future__ import annotations

import unittest
import time
from datetime import datetime, timedelta, timezone

from platform_api.control import READ_ONLY_BROKER_TOOLS, PlatformControlApi, ReadOnlyBridgeReader
from platform_api.trade_manager_live import (DISCONNECTED, DEGRADED, LIVE, TradeManagerLiveProjection,
                                             broker_environment, execution_environment)

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)
ACCOUNT = "188428665"
READ_TOOLS = {tool for tool, _ in READ_ONLY_BROKER_TOOLS.values()}


def linked_row(**overrides):
    row = {
        "managed_trade_id": "MT_live", "entry_signal_id": "SIG_live", "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
        "strategy_version": "V1", "instrument": "XAUUSD", "direction": "LONG",
        "reference_entry_price": 2000.0, "initial_stop": 1990.0, "initial_target": 2020.0,
        "tm_version_id": "TMV_ecaca5f080f9f79bb18cc936",
        "execution_intent_id": "EI_1", "attempt_id": "EA_1", "attempt_resource": f"execution:real:{ACCOUNT}",
        "execution_result_id": "ER_1", "outcome": "FILLED", "account_id": ACCOUNT,
        "broker_order_id": "5001", "broker_deal_id": "7001", "broker_position_id": "5001",
        "fill_volume": 0.01, "fill_price": 2001.0,
        "submitted_at": NOW - timedelta(minutes=45), "confirmed_at": NOW - timedelta(minutes=45),
        "observation_count": 30, "latest_observed_at": NOW - timedelta(seconds=20),
        "latest_quote_timestamp": NOW - timedelta(seconds=21),
        "latest_bid": 2006.0, "latest_ask": 2006.4, "max_bid": 2011.0, "min_bid": 1996.0,
        "max_ask": 2011.4, "min_ask": 1996.4,
        "latest_action": "HOLD", "latest_reason_codes": ["NO_MANAGEMENT_POLICY"],
        "latest_decision_at": NOW - timedelta(seconds=20),
    }
    row.update(overrides)
    return row


def position(ticket="5001", **overrides):
    value = {"ticket": int(ticket), "symbol": "XAUUSDm", "type": 0, "volume": 0.01, "price_open": 2001.0,
             "sl": 1990.0, "tp": 2020.0, "profit": 5.0, "time": 1790000000}
    value.update(overrides)
    return value


class FakeRepository:
    def __init__(self, linked=None, counts=None):
        self.linked = linked or []
        self.counts = counts if counts is not None else {"NO_EXECUTION_INTENT": 233}

    def broker_linked_managed_trades(self):
        return list(self.linked)

    def managed_trade_linkage_counts(self):
        return dict(self.counts)

    def execution_runtime_status(self):  # required by PlatformControlApi's authority wiring only
        return {}


class RecordingBridge:
    def __init__(self, *, positions=(), orders=(), account=None, fail=False):
        self.positions, self.orders, self.fail = list(positions), list(orders), fail
        self.account = account if account is not None else {"login": int(ACCOUNT), "server": "Exness-MT5Real27", "type": 2}
        self.calls: list[str] = []

    def call(self, tool, arguments=None):
        self.calls.append(tool)
        if tool not in READ_TOOLS:
            raise AssertionError(f"non-read bridge tool called: {tool}")
        if self.fail:
            raise RuntimeError("bridge unreachable")
        return {"mt5_account_info": self.account, "mt5_positions": self.positions,
                "mt5_orders": self.orders}[tool]


def project(repository, bridge, **kwargs):
    return TradeManagerLiveProjection(repository, bridge, clock=lambda: NOW, **kwargs).project()


class LiveActiveAuthorityTests(unittest.TestCase):
    def tearDown(self):
        for tool in getattr(self, "bridge", RecordingBridge()).calls:
            self.assertIn(tool, READ_TOOLS)

    def test_zero_broker_positions_means_zero_live_active_trades(self):
        self.bridge = RecordingBridge(positions=[])
        result = project(FakeRepository(counts={"NO_EXECUTION_INTENT": 82, "INTENT_NOT_SENT": 105,
                                                 "ATTEMPT_WITHOUT_RESULT": 45,
                                                 "BROKER_RESULT_WITHOUT_POSITION_ID": 1}), self.bridge)
        self.assertEqual(result["system_state"], LIVE)
        self.assertEqual(result["summary"], {"active_trades": 0, "protected_trades": 0, "actions_pending": 0,
                                             "holding_or_no_action": 0, "stale_observations": 0})
        self.assertEqual(result["active"], [])
        self.assertEqual(result["broker"]["open_positions"], 0)
        self.assertEqual(result["historical_unreconciled"]["count"], 233)
        self.assertEqual(result["managed_trade_count"], 233)

    def test_historical_open_managed_trade_without_broker_position_is_excluded(self):
        # 233 internal-OPEN ManagedTrades, none broker-linked, and an unrelated live position.
        self.bridge = RecordingBridge(positions=[position("9999", symbol="BTCUSDm", type=1)])
        result = project(FakeRepository(), self.bridge)
        self.assertEqual(result["summary"]["active_trades"], 0)
        self.assertEqual([p["position_id"] for p in result["unlinked_broker_positions"]], ["9999"])
        self.assertEqual(result["system_state"], DEGRADED)
        self.assertIn("BROKER_POSITION_WITHOUT_MANAGED_TRADE", result["degraded_reasons"])

    def test_current_broker_position_linkage_is_included(self):
        self.bridge = RecordingBridge(positions=[position()])
        result = project(FakeRepository(linked=[linked_row()], counts={"BROKER_LINKED": 1}), self.bridge)
        self.assertEqual(result["system_state"], LIVE)
        self.assertEqual(result["summary"]["active_trades"], 1)
        trade = result["active"][0]
        self.assertEqual(trade["broker_status"], "OPEN")
        self.assertEqual(trade["broker"]["position_id"], "5001")
        self.assertEqual(trade["broker"]["account_id"], ACCOUNT)
        self.assertEqual(trade["execution"]["execution_result_id"], "ER_1")
        self.assertEqual(result["historical_unreconciled"]["count"], 0)

    def test_broker_closure_removes_from_active_and_retains_history(self):
        repository = FakeRepository(linked=[linked_row()], counts={"BROKER_LINKED": 1})
        self.bridge = RecordingBridge(positions=[position()])
        self.assertEqual(project(repository, self.bridge)["summary"]["active_trades"], 1)
        self.bridge = RecordingBridge(positions=[])
        result = project(repository, self.bridge)
        self.assertEqual(result["summary"]["active_trades"], 0)
        self.assertEqual(result["active"], [])
        self.assertEqual([t["managed_trade_id"] for t in result["closed"]], ["MT_live"])
        self.assertEqual(result["closed"][0]["broker_status"], "CLOSED")
        self.assertEqual(result["closed"][0]["reconciliation_reason"], "BROKER_POSITION_ABSENT")
        self.assertEqual(result["managed_trade_count"], 1)

    def test_broker_position_id_is_authoritative_not_symbol(self):
        # Same symbol and direction, different ticket: must NOT be treated as this trade.
        self.bridge = RecordingBridge(positions=[position("5002")])
        result = project(FakeRepository(linked=[linked_row()], counts={"BROKER_LINKED": 1}), self.bridge)
        self.assertEqual(result["summary"]["active_trades"], 0)
        self.assertEqual(result["closed"][0]["managed_trade_id"], "MT_live")
        self.assertEqual([p["position_id"] for p in result["unlinked_broker_positions"]], ["5002"])

    def test_position_on_a_different_account_is_unresolved_not_active(self):
        self.bridge = RecordingBridge(positions=[position()], account={"login": 111, "type": 2})
        result = project(FakeRepository(linked=[linked_row()], counts={"BROKER_LINKED": 1}), self.bridge)
        self.assertEqual(result["summary"]["active_trades"], 0)
        self.assertEqual(result["unresolved"][0]["reconciliation_reason"], "ACCOUNT_NOT_OBSERVED")
        self.assertEqual(result["system_state"], DEGRADED)

    def test_direction_mismatch_is_an_identity_conflict(self):
        self.bridge = RecordingBridge(positions=[position(type=1)])
        result = project(FakeRepository(linked=[linked_row()], counts={"BROKER_LINKED": 1}), self.bridge)
        self.assertEqual(result["summary"]["active_trades"], 0)
        self.assertEqual(result["identity_conflicts"][0]["reason"], "DIRECTION_MISMATCH")
        self.assertIn("BROKER_IDENTITY_CONFLICT", result["degraded_reasons"])

    def test_broker_unreachable_is_disconnected_and_asserts_nothing_open(self):
        self.bridge = RecordingBridge(fail=True)
        result = project(FakeRepository(linked=[linked_row()], counts={"BROKER_LINKED": 1}), self.bridge)
        self.assertEqual(result["system_state"], DISCONNECTED)
        self.assertIsNone(result["summary"])
        self.assertEqual(result["active"], [])
        self.assertEqual(result["unresolved"][0]["broker_status"], "UNRESOLVED")
        self.assertIsNone(result["broker"]["open_positions"])

    def test_missing_account_identity_is_disconnected(self):
        self.bridge = RecordingBridge(positions=[position()], account={"server": "x"})
        result = project(FakeRepository(linked=[linked_row()]), self.bridge)
        self.assertEqual(result["system_state"], DISCONNECTED)

    def test_stalled_bridge_reads_are_parallel_and_bounded(self):
        class SlowBridge(RecordingBridge):
            timeout = 0.05

            def call(self, tool, arguments=None):
                time.sleep(0.2)
                return super().call(tool, arguments)

        self.bridge = SlowBridge()
        started = time.monotonic()
        result = project(FakeRepository(), self.bridge)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.15)
        self.assertEqual(result["system_state"], DISCONNECTED)
        self.assertIsNone(result["broker"]["open_positions"])


class LiveObservationTests(unittest.TestCase):
    def active(self, row=None, pos=None, **kwargs):
        bridge = RecordingBridge(positions=[pos or position()])
        result = project(FakeRepository(linked=[row or linked_row()], counts={"BROKER_LINKED": 1}), bridge, **kwargs)
        self.assertEqual(set(bridge.calls), {"mt5_account_info", "mt5_positions", "mt5_orders"})
        return result, result["active"][0]

    def test_environment_projection_from_execution_context(self):
        _, trade = self.active()
        self.assertEqual(trade["environment"], "REAL")
        self.assertEqual(trade["environment_source"], "EXECUTION_ATTEMPT_RESOURCE")
        self.assertEqual(execution_environment("execution:demo:1"), "DEMO")
        self.assertIsNone(execution_environment(None))
        self.assertEqual(broker_environment({"type": 2}), "REAL")
        self.assertEqual(broker_environment({"type": 0}), "DEMO")

    def test_environment_conflict_is_not_guessed(self):
        result, trade = self.active(row=linked_row(attempt_resource=f"execution:demo:{ACCOUNT}"))
        self.assertIsNone(trade["environment"])
        self.assertTrue(trade["environment_conflict"])
        self.assertIn("ENVIRONMENT_CONFLICT", result["degraded_reasons"])

    def test_current_price_uses_close_side(self):
        _, trade = self.active()
        self.assertEqual(trade["current_price"], 2006.0)
        self.assertEqual(trade["current_price_side"], "BID")
        _, short = self.active(row=linked_row(direction="SHORT"), pos=position(type=1, sl=2011.0))
        self.assertEqual(short["current_price"], 2006.4)
        self.assertEqual(short["current_price_side"], "ASK")

    def test_age_from_confirmed_fill(self):
        _, trade = self.active()
        self.assertEqual(trade["age_seconds"], 45 * 60)
        self.assertEqual(trade["opened_at"], "2026-09-26T11:15:00Z")

    def test_mfe_and_mae(self):
        _, trade = self.active()
        # entry = broker price_open 2001; risk = |2001 - 1990| = 11
        exc = trade["excursion"]
        self.assertEqual(trade["entry"], 2001.0)
        self.assertAlmostEqual(exc["current_r"], 5 / 11)
        self.assertAlmostEqual(exc["mfe_price"], 10.0)
        self.assertAlmostEqual(exc["mfe_r"], 10 / 11)
        self.assertAlmostEqual(exc["mae_price"], -5.0)
        self.assertAlmostEqual(exc["mae_r"], -5 / 11)
        _, short = self.active(row=linked_row(direction="SHORT", initial_stop=2012.0), pos=position(type=1))
        # SHORT: close side is ask; best = min ask 1996.4, worst = max ask 2011.4, entry 2001
        self.assertAlmostEqual(short["excursion"]["mfe_price"], 4.6)
        self.assertAlmostEqual(short["excursion"]["mae_price"], -10.4)

    def test_current_stop_and_target_come_from_broker_position(self):
        _, trade = self.active(pos=position(sl=2001.5, tp=2030.0))
        self.assertEqual(trade["current_stop"], 2001.5)
        self.assertEqual(trade["current_stop_source"], "BROKER_POSITION")
        self.assertEqual(trade["target"], 2030.0)
        self.assertTrue(trade["protected"])
        _, unprotected = self.active(pos=position(sl=0.0, tp=0.0))
        self.assertIsNone(unprotected["current_stop"])
        self.assertIsNone(unprotected["target"])
        self.assertFalse(unprotected["protected"])

    def test_protected_and_action_pending_counts(self):
        result, _ = self.active(row=linked_row(latest_action="MOVE_TO_BREAKEVEN"), pos=position(sl=2001.0))
        self.assertEqual(result["summary"], {"active_trades": 1, "protected_trades": 1, "actions_pending": 1,
                                             "holding_or_no_action": 0, "stale_observations": 0})

    def test_stale_observation_is_explicit_and_degrades(self):
        result, trade = self.active(row=linked_row(latest_observed_at=NOW - timedelta(minutes=10)))
        self.assertEqual(trade["observation"]["status"], "STALE")
        self.assertEqual(trade["observation"]["age_seconds"], 600)
        self.assertEqual(result["system_state"], DEGRADED)
        self.assertEqual(result["summary"]["stale_observations"], 1)

    def test_no_observation_since_fill_is_unavailable_not_blank(self):
        row = linked_row(observation_count=0, latest_observed_at=None, latest_bid=None, latest_ask=None,
                         max_bid=None, min_bid=None, max_ask=None, min_ask=None)
        result, trade = self.active(row=row)
        self.assertEqual(trade["observation"]["status"], "UNAVAILABLE")
        self.assertIsNone(trade["current_price"])
        self.assertIsNone(trade["excursion"]["mfe_r"])
        self.assertEqual(result["system_state"], DEGRADED)

    def test_stale_threshold_is_configurable(self):
        _, trade = self.active(row=linked_row(latest_observed_at=NOW - timedelta(minutes=10)),
                               stale_after_seconds=900)
        self.assertEqual(trade["observation"]["status"], "FRESH")


class OrderTicketIdentityTests(unittest.TestCase):
    """MT5 order_send returns order + deal tickets only, so real fills record no position id."""

    HEDGING = {"login": int(ACCOUNT), "server": "Exness-MT5Real27", "type": 2,
               "account_margin_mode_raw": 2, "position_mode": "HEDGING"}
    NETTING = {"login": int(ACCOUNT), "server": "Exness-MT5Real27", "type": 2,
               "account_margin_mode_raw": 0, "position_mode": "NETTING"}

    def order_only(self, **overrides):
        return linked_row(broker_position_id=None, broker_order_id="257161530", broker_deal_id="209800687",
                          **overrides)

    def tearDown(self):
        for tool in self.bridge.calls:
            self.assertIn(tool, READ_TOOLS)

    def test_hedging_account_links_the_open_position_by_its_opening_order_ticket(self):
        self.bridge = RecordingBridge(positions=[position("257161530", symbol="BTCUSDm")], account=self.HEDGING)
        result = project(FakeRepository(linked=[self.order_only()],
                                        counts={"BROKER_FILLED_ORDER_TICKET_ONLY": 1}), self.bridge)
        self.assertEqual(result["unlinked_broker_positions"], [])
        self.assertEqual(result["system_state"], LIVE)
        trade = result["active"][0]
        self.assertEqual((trade["broker"]["position_id"], trade["broker"]["position_id_source"]),
                         ("257161530", "ORDER_TICKET_HEDGING"))
        self.assertEqual(result["historical_unreconciled"], {"count": 0, "by_linkage": {}})

    def test_hedging_account_closed_order_ticket_fill_is_history(self):
        self.bridge = RecordingBridge(positions=[], account=self.HEDGING)
        result = project(FakeRepository(linked=[self.order_only()]), self.bridge)
        self.assertEqual([t["broker_status"] for t in result["closed"]], ["CLOSED"])
        self.assertEqual(result["active"], [])

    def test_netting_account_never_links_by_order_ticket(self):
        # On netting a deal may join an existing position with a different ticket.
        self.bridge = RecordingBridge(positions=[position("257161530", symbol="BTCUSDm")], account=self.NETTING)
        result = project(FakeRepository(linked=[self.order_only()],
                                        counts={"BROKER_FILLED_ORDER_TICKET_ONLY": 1}), self.bridge)
        self.assertEqual(result["active"], [])
        self.assertEqual([p["position_id"] for p in result["unlinked_broker_positions"]], ["257161530"])
        self.assertEqual(result["historical_unreconciled"]["by_linkage"], {"BROKER_FILLED_ORDER_TICKET_ONLY": 1})

    def test_unknown_margin_mode_never_links_by_order_ticket(self):
        self.bridge = RecordingBridge(positions=[position("257161530", symbol="BTCUSDm")])
        result = project(FakeRepository(linked=[self.order_only()]), self.bridge)
        self.assertEqual(result["active"], [])
        self.assertEqual(len(result["unlinked_broker_positions"]), 1)

    def test_recorded_position_id_wins_over_the_order_ticket(self):
        self.bridge = RecordingBridge(positions=[position("5001")], account=self.HEDGING)
        row = linked_row(broker_order_id="4999", broker_position_id="5001")
        result = project(FakeRepository(linked=[row], counts={"BROKER_LINKED": 1}), self.bridge)
        self.assertEqual(result["active"][0]["broker"]["position_id_source"], "EXECUTION_RESULT")

    def test_disconnected_broker_does_not_assert_order_ticket_links(self):
        self.bridge = RecordingBridge(fail=True, account=self.HEDGING)
        result = project(FakeRepository(linked=[self.order_only()]), self.bridge)
        self.assertEqual((result["system_state"], result["unresolved"]), (DISCONNECTED, []))


class LiveRouteTests(unittest.TestCase):
    def test_route_serves_the_projection_read_only(self):
        bridge = RecordingBridge(positions=[])
        api = PlatformControlApi(repository=FakeRepository(), environ={}, bridge_reader=bridge, v2_risk_api=object())
        status, body = api.execute("GET", "/api/v1/trade-manager/live")
        self.assertEqual(status, 200)
        self.assertTrue(body["read_only"])
        self.assertEqual(body["data"]["system_state"], LIVE)
        self.assertEqual(body["data"]["summary"]["active_trades"], 0)
        self.assertEqual(body["data"]["broker_writes"], 0)
        self.assertEqual(api.execute("POST", "/api/v1/trade-manager/live")[0], 405)

    def test_route_reports_disconnected_as_degraded_envelope_with_data(self):
        api = PlatformControlApi(repository=FakeRepository(), environ={}, bridge_reader=RecordingBridge(fail=True),
                                 v2_risk_api=object())
        status, body = api.execute("GET", "/api/v1/trade-manager/live")
        self.assertEqual(status, 200)
        self.assertTrue(body["degraded"])
        self.assertEqual(body["data"]["system_state"], DISCONNECTED)

    def test_read_only_bridge_reader_refuses_write_tools(self):
        reader = ReadOnlyBridgeReader("http://127.0.0.1:1")
        for tool in ("mt5_canonical_order_send", "mt5_close_position", "mt5_trailing_stop", "mt5_market_order"):
            with self.assertRaises(ValueError):
                reader.call(tool)

    def test_projection_module_has_no_broker_write_vocabulary(self):
        from pathlib import Path
        source = Path(__file__).resolve().parents[1].joinpath("platform_api", "trade_manager_live.py").read_text()
        for term in ("order_send", "mt5_close_position", "mt5_trailing_stop", "mt5_market_order",
                     "mt5_pending_order", "mt5_cancel_pending_order", "22348"):
            self.assertFalse(term in source, f"forbidden broker-write term {term!r}")


if __name__ == "__main__":
    unittest.main()
