import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import MagicMock

from scripts.audit_pending_outcome_evidence import audit, classification, coverage, missing_intervals


class PendingEvidenceAuditTests(unittest.TestCase):
    def test_internal_archive_coverage_is_distinct_from_prefix_recovery(self):
        result = coverage([0, 900, 2700], [1800], entry=0, end=3600, step=900)
        self.assertEqual(result['missing_candle_slots'], 0)
        self.assertEqual(result['archive_only_candles_inside_cache_window'], 1)
        result = coverage([1800, 2700], [0, 900], entry=0, end=3600, step=900)
        self.assertEqual(result['archive_only_candles_inside_cache_window'], 0)
        self.assertEqual(result['archive_only_candles'], 2)

    def test_missing_entry_and_tail_are_not_hidden(self):
        result = coverage([900, 1800], [], entry=0, end=3600, step=900)
        self.assertFalse(result['entry_candle_available'])
        self.assertEqual(result['missing_candle_slots'], 2)
        self.assertEqual(result['missing_interval_count'], 2)

    def test_partial_entry_candle_is_reported_without_inventing_a_gap(self):
        result = coverage([0, 900], [], entry=300, end=1800, step=900)
        self.assertFalse(result['entry_aligned_to_timeframe'])
        self.assertTrue(result['entry_candle_available'])
        self.assertEqual(result['missing_candle_slots'], 0)

    def test_gap_is_never_excused_by_weekend_or_crypto_calendar(self):
        friday = int(datetime(2026, 10, 16, 21, tzinfo=timezone.utc).timestamp())
        result = coverage([friday], [], entry=friday, end=friday+172800, step=900)
        self.assertGreater(result['missing_candle_slots'], 0)
        self.assertEqual(result['missing_intervals'][0]['session_status'], 'UNVERIFIED_NO_BROKER_CALENDAR')

    def test_unspecified_is_not_assumed_genuinely_open(self):
        self.assertEqual(classification('INSUFFICIENT_DATA', {}), 'NEEDS_INDIVIDUAL_REVIEW')
        self.assertEqual(classification('INSUFFICIENT_DATA', {'status':'OPEN_AT_DATASET_END'}),
                         'OPEN_THROUGH_LAST_EVALUATED_CANDLE')
        self.assertEqual(classification('AMBIGUOUS_INTRABAR', {}), 'AMBIGUOUS_INTRABAR')

    def test_refuses_read_write_connection(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchone.return_value = ('off',)
        with self.assertRaisesRegex(RuntimeError, 'read-only'):
            audit(conn, MagicMock(), now=3600)
        cur = conn.cursor.return_value.__enter__.return_value
        self.assertEqual(cur.execute.call_count, 1)

    def test_complete_inventory_uses_get_and_select_only(self):
        conn, client = MagicMock(), MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ('on',)
        row = ('s', 'TEST', 'BTCUSD', 'LONG', 100, 99, 102,
               datetime.fromtimestamp(0, timezone.utc), 'MARKET', {}, {'provider_symbol':'BTCUSDm'},
               'INSUFFICIENT_DATA', {'reason':'CANDLE_GAP'}, None, 2, None, None, None, None)
        cur.fetchall.side_effect = [[row], [('MT5',0), ('MT5',900), ('MT5',1800)]]
        conn.transaction.return_value = nullcontext()
        client.get.return_value = '[{"time":0},{"time":1800},{"time":2700}]'
        report = audit(conn, client, now=3600)
        client.get.assert_called_once_with('md:bars:BTCUSDm:M15')
        self.assertEqual(len(report['signals']), 1)
        self.assertEqual(report['signals'][0]['coverage_audit']['archive_only_candles_inside_cache_window'], 1)
        for call in cur.execute.call_args_list:
            self.assertIn(call.args[0].lstrip().split()[0], ('SHOW', 'SELECT'))
        conn.commit.assert_not_called()

    def test_multiple_providers_are_not_stitched(self):
        conn, client = MagicMock(), MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ('on',)
        row = ('s', 'TEST', 'BTCUSD', 'LONG', 100, 99, 102,
               datetime.fromtimestamp(0, timezone.utc), 'MARKET', {}, {},
               'INSUFFICIENT_DATA', {}, None, 1, None, None, None, None)
        cur.fetchall.side_effect = [[row], [('A',0), ('B',900)]]
        conn.transaction.return_value = nullcontext()
        client.get.return_value = None
        report = audit(conn, client, now=1800)
        result = report['signals'][0]['coverage_audit']
        self.assertEqual(result['missing_candle_slots'], 2)
        self.assertTrue(result['source_errors_or_truncation'])

    def test_unaligned_bounds_fail_explicitly(self):
        with self.assertRaises(ValueError):
            missing_intervals([], 1, 1800, 900)
