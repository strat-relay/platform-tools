import json
import os
import unittest
from unittest import mock

from execution_v2.symbols import SymbolMappingError, canonical_request_fingerprint, resolve_broker_symbol


class ExecutionSymbolMappingTests(unittest.TestCase):
    def test_resolves_by_account_and_mode_without_suffix_guessing(self):
        mapping = {"real:ACC1": {"XAUUSD": "XAUUSDm", "BTCUSD": "BTCUSD.pro"}}
        with mock.patch.dict(os.environ, {"V2_BROKER_SYMBOL_MAP_JSON": json.dumps(mapping)}, clear=False):
            self.assertEqual(resolve_broker_symbol("XAUUSD", account_id="ACC1", mode="real"), "XAUUSDm")
            self.assertEqual(resolve_broker_symbol("BTCUSD", account_id="ACC1", mode="real"), "BTCUSD.pro")

    def test_missing_mapping_fails_closed(self):
        with mock.patch.dict(os.environ, {"V2_BROKER_SYMBOL_MAP_JSON": json.dumps({"real:ACC1": {}})}, clear=False):
            with self.assertRaises(SymbolMappingError):
                resolve_broker_symbol("XAUUSD", account_id="ACC1", mode="real")

    def test_account_map_wins_and_the_catalog_fills_the_gaps(self):
        mapping = {"real:ACC1": {"XAUUSD": "XAUUSD.pro"}}
        catalog = {"XAUUSD": "XAUUSDm", "ETHUSD": "ETHUSDm"}.get
        with mock.patch.dict(os.environ, {"V2_BROKER_SYMBOL_MAP_JSON": json.dumps(mapping)}, clear=False):
            self.assertEqual(resolve_broker_symbol("XAUUSD", account_id="ACC1", mode="real", catalog_lookup=catalog),
                             "XAUUSD.pro")
            self.assertEqual(resolve_broker_symbol("ETHUSD", account_id="ACC1", mode="real", catalog_lookup=catalog),
                             "ETHUSDm")
            with self.assertRaises(SymbolMappingError):
                resolve_broker_symbol("ADAUSD", account_id="ACC1", mode="real", catalog_lookup=catalog)

    def test_the_catalog_never_replaces_a_missing_account_binding(self):
        with mock.patch.dict(os.environ, {"V2_BROKER_SYMBOL_MAP_JSON": json.dumps({"real:OTHER": {"X": "Y"}})}, clear=False):
            with self.assertRaises(SymbolMappingError):
                resolve_broker_symbol("ETHUSD", account_id="ACC1", mode="real", catalog_lookup=lambda _: "ETHUSDm")

    def test_catalog_lookup_fails_closed_on_a_read_error(self):
        from execution_v2.symbols import catalog_symbol_lookup

        def broken(readonly=False):
            raise RuntimeError("db down")
        self.assertIsNone(catalog_symbol_lookup(broken)("ETHUSD"))

    def test_canonical_wire_fingerprint_is_stable_for_numeric_spelling(self):
        base = {"schema_version": 1, "action": 1, "magic": 0, "symbol": "XAUUSDm",
                "volume": 0.01, "price": 4320.0, "sl": 4300.0, "tp": 4400.0,
                "deviation": 50, "type": 0, "type_filling": 1, "type_time": 0,
                "expiration": 0, "comment": "SRV2:ATT_1"}
        alternate = dict(base, price=4320, sl=4300, tp=4400)
        from execution_v2.symbols import canonical_request_text
        self.assertEqual(canonical_request_fingerprint(base), canonical_request_fingerprint(alternate))
        self.assertIn("symbol=XAUUSDm", canonical_request_text(base))

