"""Status endpoints must not scan the large history tables on every request: exact count(*) over
the event and Trade Manager tables (hundreds of thousands of rows) queued every other API request
behind them. Counts are PostgreSQL estimates; the Trade Manager summary is computed at most once
per TTL, single-flight; reachability checks never count."""
from __future__ import annotations

import re
import threading
import time
import unittest

from platform_api import control
from platform_api.control import PlatformControlApi, PlatformControlRepository, TtlCache

HISTORY_TABLES = ("platform.outbox_events", "platform.inbox_events", "trade_management.trade_observation",
                  "trade_management.trade_manager_decision")


class SqlRecorder(PlatformControlRepository):
    def __init__(self):
        self.sql: list[str] = []

    def query(self, sql, params=()):
        self.sql.append(sql)
        return [{"outbox_count": 1, "inbox_count": 1, "orchestrator_running": 1}]

    def _trade_management_query(self, sql, params=()):
        self.sql.append(sql)
        return [{"total_managed_trades": 1}]


class StatusCostTests(unittest.TestCase):
    def test_no_exact_count_over_history_tables(self):
        repo = SqlRecorder()
        repo.platform_status()
        repo._trade_manager_summary()
        text = " ".join(" ".join(s.split()) for s in repo.sql)
        for table in HISTORY_TABLES:
            self.assertNotRegex(text, rf"count\(\*\) FROM {re.escape(table)}\b", table)
        self.assertTrue(repo.platform_status()["event_counts_estimated"])

    def test_reachability_endpoints_never_count(self):
        calls = []

        class Repo(SqlRecorder):
            def platform_status(self, *, include_event_counts=True):
                calls.append(include_event_counts)
                return {"outbox_count": 0, "inbox_count": 0, "orchestrator_running": 0}

            def execution_runtime_status(self):
                return {"risk_policy": {}, "canary": None, "worker_status": "UNKNOWN",
                        "execution_bridge": {}, "broker_account": {}}
        api = PlatformControlApi(repository=Repo(), environ={}, bridge_reader=object(), v2_risk_api=object(),
                                 trade_manager_mode_api=object())
        for path in ("/readyz", "/api/v1/connections"):
            api.execute("GET", path)
        self.assertEqual(calls, [False, False])

    def test_ttl_cache_is_single_flight_and_does_not_cache_failures(self):
        now = {"t": 0.0}
        cache = TtlCache(60, clock=lambda: now["t"])
        computed = []

        def slow():
            computed.append(1)
            time.sleep(0.2)
            return len(computed)
        threads = [threading.Thread(target=lambda: cache.get("k", slow)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(computed), 1)
        self.assertEqual(cache.get("k", slow), 1)
        now["t"] = 61
        self.assertEqual(cache.get("k", slow), 2)
        with self.assertRaises(RuntimeError):
            cache.get("x", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        self.assertEqual(cache.get("x", lambda: "ok"), "ok")

    def test_tm_summary_ttl_is_configurable_default_60s(self):
        self.assertEqual(control.TM_SUMMARY_TTL_SECONDS, 60.0)


if __name__ == "__main__":
    unittest.main()
