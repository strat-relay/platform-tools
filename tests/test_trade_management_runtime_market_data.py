from __future__ import annotations

import unittest

from trade_management.runtime.market_data_live import LiveMarketDataProvider, build_bridge_client


class FakeReadOnlyBridge:
    """Duck-typed stand-in for `contracts.mt5_bridge.Mt5ReadClient` - only `.quote()`/`.rates()`,
    matching the real client's own hard restriction to READ_TOOLS."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def quote(self, symbol: str):
        self.calls.append(("quote", (symbol,)))
        return {"bid": 100.0, "ask": 100.2, "quote_age_ms": 10}

    def rates(self, symbol: str, timeframe: str, limit: int = 20):
        self.calls.append(("rates", (symbol, timeframe, limit)))
        return [{"time": "2026-09-22T11:55:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5}] * 3


class LiveMarketDataProviderTests(unittest.TestCase):
    def test_quote_uses_only_the_read_only_client(self):
        bridge = FakeReadOnlyBridge()
        provider = LiveMarketDataProvider(bridge)
        quote = provider.quote("XAUUSD")
        self.assertEqual(quote.bid, 100.0)
        self.assertEqual(quote.ask, 100.2)
        self.assertEqual(quote.instrument, "XAUUSD")
        self.assertEqual(bridge.calls, [("quote", ("XAUUSD",))])

    def test_bars_uses_only_the_read_only_client(self):
        bridge = FakeReadOnlyBridge()
        provider = LiveMarketDataProvider(bridge)
        bars = provider.bars("XAUUSD")
        self.assertEqual(bars.timeframe, "M5")
        self.assertEqual(bars.count, 3)
        self.assertTrue(bars.digest.startswith("sha256:"))
        self.assertEqual(bridge.calls, [("rates", ("XAUUSD", "M5", 1))])

    def test_no_broker_position_or_order_method_is_ever_called(self):
        class StrictBridge(FakeReadOnlyBridge):
            def __getattr__(self, name):
                raise AssertionError(f"unexpected bridge method accessed: {name}")
        provider = LiveMarketDataProvider(StrictBridge())
        provider.quote("XAUUSD")
        provider.bars("XAUUSD")  # neither call touches positions/orders/execution

    def test_build_bridge_client_refuses_the_execution_port(self):
        with self.assertRaises(ValueError):
            build_bridge_client("http://127.0.0.1:22348/mcp")

    def test_build_bridge_client_targets_the_research_port_by_default(self):
        client = build_bridge_client("http://127.0.0.1:22347/mcp")
        from contracts.mt5_bridge import Mt5ReadClient
        self.assertIsInstance(client, Mt5ReadClient)
        self.assertNotIn("22348", client.endpoint.url)
        self.assertEqual(client.endpoint.profile, "research")

    def test_provider_never_exposes_a_write_capable_client_type(self):
        client = build_bridge_client("http://127.0.0.1:22347/mcp")
        from contracts.mt5_bridge import Mt5ExecutionClient
        self.assertNotIsInstance(client, Mt5ExecutionClient)


if __name__ == "__main__":
    unittest.main()
