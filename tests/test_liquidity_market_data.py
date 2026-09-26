import unittest

from liquidity_market_data import ReadOnlyLiquidityMarketData
from trade_management.runtime.market_data_live import build_bridge_client


class FakeReadClient:
    def __init__(self):
        self.calls = []

    def quote(self, symbol):
        self.calls.append(("quote", symbol))
        return {"bid": 100, "ask": 100.2, "timestamp": 1790156058}

    def symbol_info(self, symbol):
        self.calls.append(("symbol_info", symbol))
        return {"point": 0.01, "stops_level": 0}

    def rates(self, symbol, timeframe, limit=20):
        self.calls.append(("rates", symbol, timeframe, limit))
        return {"rates": [{"time": 1790155758, "open": 99, "high": 101, "low": 98, "close": 100},
                           {"time": 1790156058, "open": 99, "high": 101, "low": 98, "close": 100}]}


class LiquidityMarketDataTests(unittest.TestCase):
    def test_execution_bridge_is_rejected(self):
        with self.assertRaises(ValueError):
            build_bridge_client("http://127.0.0.1:22348/mcp")
    def test_snapshot_uses_explicit_provider_mapping_and_only_reads(self):
        client = FakeReadClient()
        snapshot = ReadOnlyLiquidityMarketData(client).snapshot("BTCUSD", "BTCUSD.pro")
        self.assertEqual(snapshot.canonical_instrument, "BTCUSD")
        self.assertEqual(snapshot.provider_symbol, "BTCUSD.pro")
        self.assertEqual(len(snapshot.M5), 1)
        self.assertEqual(len(snapshot.M15), 1)
        self.assertEqual([call[0] for call in client.calls], ["quote", "symbol_info", "rates", "rates"])

    def test_missing_payload_is_not_filled_from_paper_state(self):
        class Empty(FakeReadClient):
            def rates(self, symbol, timeframe, limit=20):
                return []
        snapshot = ReadOnlyLiquidityMarketData(Empty()).snapshot("XAUUSD", "XAUUSDm")
        self.assertEqual(snapshot.source_kind, "LIVE_MARKET")
        self.assertEqual(snapshot.M5, ())


if __name__ == "__main__":
    unittest.main()
