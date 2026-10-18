"""Managed instance discovery supports both historical JSONB instrument shapes."""
import unittest
from unittest.mock import MagicMock

from orchestration.config import _load_v2_instances_from_database


class ManagedInstanceInstrumentTests(unittest.TestCase):
    def load(self, instruments, membership=None):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = [
            ('instance-id', 'price_action_sr_retest_v1', 'Observer', True,
             membership or [], instruments, {}, 'fingerprint', None, 'OFF', 0)]
        return _load_v2_instances_from_database(conn)[0]

    def test_string_instruments_used_by_library_registrations(self):
        row = self.load(['XAUUSD', 'BTCUSD', 'USDJPY'])
        self.assertEqual(row['active_instruments'], ['XAUUSD', 'BTCUSD', 'USDJPY'])
        self.assertEqual(row['execution_mode'], 'OFF')

    def test_object_and_mixed_historical_instruments(self):
        self.assertEqual(self.load([{'canonical_instrument': 'XAUUSD'}, 'BTCUSD'])[
            'active_instruments'], ['XAUUSD', 'BTCUSD'])

    def test_membership_remains_authoritative(self):
        self.assertEqual(self.load(['BTCUSD'], ['USDJPY'])['active_instruments'], ['USDJPY'])

    def test_malformed_entries_do_not_become_instrument_names(self):
        self.assertEqual(self.load([None, '', {}, {'canonical_instrument': None}, 12, True,
                                    {'canonical_instrument': ['BTCUSD']}, 'XAUUSD'])[
            'active_instruments'], ['XAUUSD'])

    def test_non_list_json_does_not_become_instrument_names(self):
        for raw in (None, 'BTCUSD', {'canonical_instrument': 'BTCUSD'}):
            with self.subTest(raw=raw):
                self.assertEqual(self.load(raw)['active_instruments'], [])
