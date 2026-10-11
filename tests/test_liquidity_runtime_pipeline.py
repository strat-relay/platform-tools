import unittest

from liquidity_live_runtime import LiquidityLiveRuntime, open_liquidity_entries
from orchestration.liquidity_live import PARAMETER_SETS
from tests.test_liquidity_live_runtime import FakeStrategy, snapshot
from liquidity_market_data import LiveMarketSnapshot


class Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sqls = []
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=None):
        self.sql = sql
        self.sqls.append(sql)
    def fetchall(self):
        return [] if "entry_signal_outcomes" in self.sql else self.rows
    def fetchone(self):
        return None  # no orchestrator lock active in unit tests


class Conn:
    def __init__(self, rows):
        self.rows = rows; self.commits = 0; self.cursor_obj = Cursor(rows)
    def cursor(self): return self.cursor_obj
    def commit(self): self.commits += 1


class OpenEntriesCursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql, params=None): self.sql = sql
    def fetchall(self): return self.rows


class OpenEntriesConn:
    def __init__(self, rows): self.cursor_obj = OpenEntriesCursor(rows)
    def cursor(self): return self.cursor_obj


class Publisher:
    def __init__(self): self.signals = []
    def publish(self, signal):
        self.signals.append(signal)
        return signal, len(self.signals) == 1


class LiquidityRuntimePipelineTests(unittest.TestCase):
    def test_open_entries_resolve_provider_symbol_from_active_mapping(self):
        conn = OpenEntriesConn([(
            "sig-1", "liquidity-btc25", "BTCUSD", "BTCUSDm", "LONG",
            100.0, 99.0, 102.0, "2026-09-26T12:00:00Z",
        )])

        rows = open_liquidity_entries(conn)

        self.assertEqual(rows[0]["canonical_instrument"], "BTCUSD")
        self.assertEqual(rows[0]["provider_symbol"], "BTCUSDm")
        self.assertIn("JOIN platform.instrument_provider_mapping", conn.cursor_obj.sql)

    def test_membership_mapping_to_canonical_signal_has_no_broker_write(self):
        conn = Conn([("liquidity-xau-base", "XAUUSD", "XAUUSD.pro")])
        publisher = Publisher()
        runtime = LiquidityLiveRuntime(
            conn=conn,
            snapshot_reader=lambda canonical, provider: LiveMarketSnapshot(**{**snapshot().__dict__, "provider_symbol": provider}),
            publisher=publisher,
        )
        runtime.evaluators["liquidity-xau-base"] = __import__(
            "orchestration.liquidity_live", fromlist=["LiquidityLiveEvaluator"]
        ).LiquidityLiveEvaluator(PARAMETER_SETS["liquidity-xau-base"], strategy=FakeStrategy())
        runtime._restored.add("liquidity-xau-base")
        result = runtime.tick(evaluation_time="2026-09-26T12:00:00Z")
        self.assertEqual(result["production_broker_writes"], 0)
        self.assertEqual(result["published"], [publisher.signals[0].signal_id])
        self.assertEqual(publisher.signals[0].symbol, "XAUUSD.pro")
        self.assertEqual(conn.commits, 1)
        self.assertTrue(any("entry_signal_outcomes" in sql and
                            "instrument_provider_mapping" in sql
                            for sql in conn.cursor_obj.sqls))


if __name__ == "__main__":
    unittest.main()
