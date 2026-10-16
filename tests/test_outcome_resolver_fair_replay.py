"""Incident regressions: blocked oldest batch and non-resumable path replay."""
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from outcome_resolver import Candle
from outcome_resolver_runtime import OutcomeResolverRuntime, ResolverSignal

UTC = timezone.utc
T0 = datetime(2026, 10, 12, tzinfo=UTC)


class SQLiteConnection:
    """Execute the production selection query against real relational data."""
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute("ATTACH DATABASE ':memory:' AS strategy")
        self.db.execute("ATTACH DATABASE ':memory:' AS platform")
        self.db.executescript('''
            CREATE TABLE strategy.entry_signals (
                signal_id TEXT, strategy_id TEXT, instrument TEXT, direction TEXT,
                entry_price REAL, stop_price REAL, target_price REAL,
                decision_time TEXT, entry_type TEXT, strategy_metadata TEXT,
                source_provenance TEXT);
            CREATE TABLE strategy.entry_signal_outcomes (signal_id TEXT, status TEXT);
            CREATE TABLE platform.outcome_resolver_signal_state (
                signal_id TEXT, updated_at TEXT);
        ''')
    def cursor(self):
        outer = self
        class QueryCursor:
            def execute(self, sql, params):
                self.cursor = outer.db.execute(sql.replace('%s', '?'), params)
            def fetchall(self): return self.cursor.fetchall()
            def __enter__(self): return self
            def __exit__(self, *args): self.cursor.close()
        return QueryCursor()


class FairReplayTests(unittest.TestCase):
    def signal(self):
        return ResolverSignal('S', 'TEST', 'EURUSD', 'LONG', 100, 99, 102,
                              T0, 'MARKET', {}, {})

    def test_blocked_oldest_100_do_not_starve_23_new_signals(self):
        conn = SQLiteConnection()
        self.addCleanup(conn.db.close)
        for i in range(123):
            stamp = (T0 + timedelta(minutes=i)).isoformat()
            conn.db.execute('INSERT INTO strategy.entry_signals VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                            (str(i), 'TEST', 'EURUSD', 'LONG', 100, 99, 102,
                             stamp, 'MARKET', '{}', '{}'))
        runtime = OutcomeResolverRuntime(conn=conn, candle_store=None)
        first = runtime._signals(100)
        self.assertEqual(len(first), 100)
        for signal in first:
            conn.db.execute('INSERT INTO platform.outcome_resolver_signal_state VALUES (?,?)',
                            (signal.signal_id, (T0 + timedelta(days=1)).isoformat()))
        second = runtime._signals(100)
        unseen = {str(i) for i in range(100, 123)}
        self.assertTrue(unseen.issubset({s.signal_id for s in second}))
        # Once all have attempt timestamps, oldest attempts rotate ahead again.
        for signal in second:
            conn.db.execute('DELETE FROM platform.outcome_resolver_signal_state WHERE signal_id=?',
                            (signal.signal_id,))
            conn.db.execute('INSERT INTO platform.outcome_resolver_signal_state VALUES (?,?)',
                            (signal.signal_id, (T0 + timedelta(days=2)).isoformat()))
        third = runtime._signals(100)
        self.assertTrue({s.signal_id for s in first} - {s.signal_id for s in second}
                        <= {s.signal_id for s in third})

    def runtime(self, candles, cursor):
        conn, store = MagicMock(), MagicMock()
        store.candles.return_value = candles
        store.archive_candles.return_value = []
        runtime = OutcomeResolverRuntime(conn=conn, candle_store=store)
        runtime._cursor = lambda _: cursor
        runtime._prior_resolution = lambda _: ('INSUFFICIENT_DATA', {})
        runtime._next_attempt = lambda _: 2
        runtime._authority_lock = lambda _: None
        runtime._assert_lease = lambda _: None
        runtime._persist_state = MagicMock()
        return runtime, store

    def test_restart_replays_entry_path_instead_of_inventing_initial_gap(self):
        bars = [Candle(T0 + timedelta(minutes=15*i), T0 + timedelta(minutes=15*(i+1)),
                       101 if i < 2 else 103, 99.5, 100.5) for i in range(3)]
        runtime, _ = self.runtime(bars, T0 + timedelta(minutes=30))
        with patch('outcome_resolver_runtime.persist_outcome_row'):
            result = runtime.resolve_one(self.signal())
        self.assertEqual(result.resolution_state, 'RESOLVED')
        self.assertEqual(result.realized_r, 2)

    def test_gap_does_not_retain_a_cursor_beyond_unprocessed_history(self):
        bar = Candle(T0 + timedelta(days=1), T0 + timedelta(days=1, minutes=15),
                     101, 99.5, 100)
        runtime, store = self.runtime([bar], T0 + timedelta(hours=12))
        with patch('outcome_resolver_runtime.persist_outcome_row'):
            result = runtime.resolve_one(self.signal())
        self.assertEqual(result.evidence['reason'], 'CANDLE_GAP')
        self.assertEqual(result.evidence['observed_candles'], 0)
        store.archive_candles.assert_called_once()
        self.assertEqual(store.archive_candles.call_args.kwargs['since'], T0)
        self.assertEqual(runtime._persist_state.call_args.args[3], [])
        self.assertIsNone(runtime._persist_state.call_args.kwargs['prior_cursor'])

    def test_archive_is_read_even_when_redis_has_no_candles(self):
        runtime, store = self.runtime([], None)
        store.archive_candles.return_value = [Candle(T0, T0 + timedelta(minutes=15),
                                                     103, 99.5, 102)]
        with patch('outcome_resolver_runtime.persist_outcome_row'):
            result = runtime.resolve_one(self.signal())
        self.assertEqual(result.resolution_state, 'RESOLVED')
        store.archive_candles.assert_called_once()

    def test_limit_activation_before_cursor_is_replayed(self):
        from dataclasses import replace
        signal = replace(self.signal(), entry_type='LIMIT', strategy_metadata={
            'outcome_contract': {'activation': 'ENTRY_PRICE_TOUCH'}})
        bars = [Candle(T0, T0 + timedelta(minutes=15), 101, 99.5, 100.5),
                Candle(T0 + timedelta(minutes=15), T0 + timedelta(minutes=30),
                       103, 100.5, 102)]
        runtime, _ = self.runtime(bars, T0 + timedelta(minutes=15))
        with patch('outcome_resolver_runtime.persist_outcome_row'):
            result = runtime.resolve_one(signal)
        self.assertEqual(result.resolution_state, 'RESOLVED')
        self.assertEqual(result.realized_r, 2)

    def test_proven_ambiguity_survives_cache_eviction(self):
        runtime, _ = self.runtime([], T0 + timedelta(minutes=15))
        evidence = {'reason': 'ENTRY_AND_EXIT_IN_SAME_CANDLE'}
        runtime._prior_resolution = lambda _: ('AMBIGUOUS_INTRABAR', evidence)
        with patch('outcome_resolver_runtime.persist_outcome_row'):
            result = runtime.resolve_one(self.signal())
        self.assertEqual(result.resolution_state, 'AMBIGUOUS_INTRABAR')
        self.assertEqual(result.evidence, evidence)
