import unittest
from pathlib import Path

from research.multitimeframe_liquidity_sniper.symbol_mapping import resolve_from_read_probes, resolve_from_symbol_info


class PagedExportProtocolTests(unittest.TestCase):
    def test_read_only_endpoint_is_present_in_both_protocol_layers(self):
        bridge = Path("mt5_bridge/server.py").read_text()
        ea = Path("ea/MT5TradingBridge.mq5").read_text()
        self.assertIn('"mt5_rates_range"', bridge)
        self.assertIn('RatesRangeJson', ea)
        self.assertIn('start<=candle_timestamp<end', ea)
        self.assertIn('tool=="mt5_rates_range"', ea)

    def test_exporter_has_no_execution_tool_names(self):
        source = Path("research/multitimeframe_liquidity_sniper/paged_export.py").read_text()
        self.assertNotIn("mt5_market_order", source)
        self.assertNotIn("mt5_pending_order", source)
        self.assertIn("mt5_rates_range", source)

    def test_research_bridge_exposes_only_read_tools(self):
        bridge = Path("mt5_bridge/server.py").read_text()
        self.assertIn("RESEARCH_READ_ONLY_TOOLS", bridge)
        for forbidden in ("mt5_market_order", "mt5_pending_order", "mt5_close_position", "mt5_cancel_pending_order", "mt5_canonical_order_send"):
            self.assertIn(forbidden, bridge)

    def test_symbol_mapping_prefers_unsuffixed_research_symbol(self):
        symbol, info = resolve_from_symbol_info("USDJPY", {
            "USDJPY": {"symbol": "USDJPY", "digits": 3},
            "USDJPYm": {"error": "symbol unavailable", "symbol": "USDJPYm"},
        })
        self.assertEqual(symbol, "USDJPY")
        self.assertEqual(info["symbol"], "USDJPY")

    def test_symbol_mapping_supports_suffix_only_account(self):
        symbol, _ = resolve_from_symbol_info("USDJPY", {
            "USDJPY": {"error": "symbol unavailable", "symbol": "USDJPY"},
            "USDJPYm": {"symbol": "USDJPYm", "digits": 3},
        })
        self.assertEqual(symbol, "USDJPYm")

    def test_symbol_mapping_fails_closed(self):
        with self.assertRaises(LookupError):
            resolve_from_symbol_info("USDJPY", {
                "USDJPY": {"error": "symbol unavailable", "symbol": "USDJPY"},
                "USDJPYm": None,
            })

    def test_symbol_mapping_prefers_quote_usable_alias(self):
        symbol, _ = resolve_from_read_probes("BTCUSD", {
            "BTCUSD": {"symbol": "BTCUSD", "digits": 2},
            "BTCUSDm": {"error": "symbol unavailable", "symbol": "BTCUSDm"},
        }, {
            "BTCUSD": {"error": "quote unavailable", "symbol": "BTCUSD"},
            "BTCUSDm": {"symbol": "BTCUSDm", "bid": 1.0, "ask": 1.1},
        })
        self.assertEqual(symbol, "BTCUSDm")


if __name__ == "__main__":
    unittest.main()
