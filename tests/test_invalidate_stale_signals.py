"""Operator invalidation of stale signals (migration 035 + scripts/invalidate_stale_signals.py).

Proves: only signals decided before the cutoff with an OPEN or missing outcome are invalidated;
terminal outcomes and today's signals are untouched; every invalidation is audited append-only;
their managed trades close through the normal lifecycle; runners can never overwrite INVALIDATED;
stats count INVALIDATED separately (not as a closed trade, no R); dry run changes nothing.
"""
from __future__ import annotations

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from postgres.config import PostgresConfig  # noqa: E402
from postgres.db import apply_migrations, connect  # noqa: E402

STRATEGY = "CONTEXT_STRUCTURE_RETRACE_V1"
NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)


def _server_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class InvalidateStaleSignalsTests(unittest.TestCase):
    def setUp(self):
        from test_integration_tm_membership_runner import FreshDatabase
        self.db = FreshDatabase()
        with self.db.connect() as conn:
            apply_migrations(conn)
            conn.commit()
        self.ids = {}
        with self.db.connect() as conn, conn.cursor() as cur:
            # old OPEN, old missing outcome, old STOPPED, today's OPEN
            for name, when, status in (("old_open", CUTOFF - timedelta(hours=5), "OPEN"),
                                       ("old_missing", CUTOFF - timedelta(days=2), None),
                                       ("old_stopped", CUTOFF - timedelta(hours=3), "STOPPED"),
                                       ("today_open", CUTOFF + timedelta(hours=2), "OPEN")):
                self.ids[name] = self._signal(cur, when, status)
            conn.commit()

    def tearDown(self):
        self.db.drop()

    def _signal(self, cur, when, status):
        tag = uuid.uuid4().hex[:10]
        sid = f"SIG_{tag}"
        cur.execute("""INSERT INTO strategy.evaluations (evaluation_id, strategy_id, instrument, decision_time,
            decision, trace_fidelity, runtime_version, evaluator_version, canonical_payload, canonical_hash)
            VALUES (%s,%s,'EURUSD',%s,'SIGNAL','L1','t','t','{}'::jsonb,%s)""", (f"E{tag}", STRATEGY, when, f"H{tag}"))
        cur.execute("""INSERT INTO strategy.entry_signals (signal_id, candidate_id, evaluation_id, strategy_ref,
            strategy_id, strategy_version, instrument, direction, decision_time, signal_emitted_at, entry_price,
            stop_price, target_price, target_r, evaluation_hash, trace_hash, terminal_state, entry_signal_hash)
            VALUES (%s,%s,%s,%s,%s,'V1','EURUSD','LONG',%s,%s,1.1,1.09,1.105,0.5,'e','t','ENTRY_SIGNAL_CREATED',%s)""",
                    (sid, f"C{tag}", f"E{tag}", f"{STRATEGY}@V1", STRATEGY, when, when, f"h{tag}"))
        if status is not None:
            stopped = status == "STOPPED"
            cur.execute("""INSERT INTO strategy.entry_signal_outcomes (signal_id, outcome_type, status, realized_r,
                exit_timestamp, source) VALUES (%s,'ENTRY_ONLY',%s,%s,%s,%s)""",
                        (sid, status, -1.0 if stopped else None, when + timedelta(hours=1) if stopped else None, STRATEGY))
        return sid

    def _managed_trades(self):
        from trade_management.binding import DefaultTmNoneResolver
        from trade_management.managed_trade import create_managed_trade
        from test_trade_manager_live_real_postgres import TM_NONE_1
        with self.db.connect() as conn:
            for sid in self.ids.values():
                create_managed_trade(conn, event_id=f"evt-{sid}", signal_id=sid,
                                     resolver=DefaultTmNoneResolver(tm_version_id=TM_NONE_1), now_utc=CUTOFF - timedelta(days=3))
            conn.commit()

    def _run(self, apply=True):
        from scripts.invalidate_stale_signals import invalidate
        return invalidate(self.db.connect, before=CUTOFF, reason="stale pre-today signals", operator="op",
                          apply=apply, now=NOW)

    def _outcomes(self):
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT signal_id, status, realized_r FROM strategy.entry_signal_outcomes")
            return {s: (st, r) for s, st, r in cur.fetchall()}

    def test_only_stale_open_or_missing_signals_are_invalidated_and_audited(self):
        report = self._run()
        self.assertEqual(report["invalidated"], {STRATEGY: {"was_open": 1, "was_missing": 1}})
        outcomes = self._outcomes()
        self.assertEqual(outcomes[self.ids["old_open"]], ("INVALIDATED", None))
        self.assertEqual(outcomes[self.ids["old_missing"]], ("INVALIDATED", None))
        self.assertEqual(outcomes[self.ids["old_stopped"]][0], "STOPPED")      # terminal untouched
        self.assertEqual(outcomes[self.ids["today_open"]][0], "OPEN")          # today untouched
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT signal_id, previous_status, operator FROM strategy.entry_signal_outcome_override")
            audit = {s: (p, o) for s, p, o in cur.fetchall()}
            self.assertEqual(audit, {self.ids["old_open"]: ("OPEN", "op"), self.ids["old_missing"]: (None, "op")})
            with self.assertRaises(Exception):
                cur.execute("DELETE FROM strategy.entry_signal_outcome_override")
            conn.rollback()
        self.assertEqual(self._run()["invalidated"], {})                        # idempotent

    def test_dry_run_changes_nothing(self):
        before = self._outcomes()
        report = self._run(apply=False)
        self.assertEqual(report["invalidated"], {STRATEGY: {"was_open": 1, "was_missing": 1}})
        self.assertEqual(self._outcomes(), before)

    def test_managed_trades_of_invalidated_signals_close_and_others_stay_open(self):
        self._managed_trades()
        report = self._run()
        # The old STOPPED signal's trade is created already CLOSED (terminal outcome), so only the 2
        # invalidated trades close here.
        self.assertEqual(report["managed_trades_closed"], 2)
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT entry_signal_id, state FROM trade_management.managed_trade")
            states = dict(cur.fetchall())
            cur.execute("""SELECT entry_signal_id, strategy_outcome, realized_r
                           FROM trade_management.managed_trade_lifecycle_event""")
            events = {s: (o, r) for s, o, r in cur.fetchall()}
        self.assertEqual(states[self.ids["today_open"]], "OPEN")
        self.assertEqual(states[self.ids["old_open"]], "CLOSED")
        self.assertEqual(events[self.ids["old_missing"]], ("INVALIDATED", None))

    def test_runners_cannot_overwrite_an_invalidated_outcome(self):
        self._run()
        from liquidity_outcomes import project_liquidity_outcome  # noqa: F401  (writer guarded on OPEN)
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.entry_signal_outcomes (signal_id, outcome_type, status, realized_r,
                exit_timestamp, source) VALUES (%s,'ENTRY_ONLY','TARGET_HIT',0.5,%s,%s)
                ON CONFLICT (signal_id) DO UPDATE SET status = EXCLUDED.status, realized_r = EXCLUDED.realized_r,
                exit_timestamp = EXCLUDED.exit_timestamp WHERE strategy.entry_signal_outcomes.status = 'OPEN'""",
                        (self.ids["old_open"], NOW, STRATEGY))
            conn.commit()
        self.assertEqual(self._outcomes()[self.ids["old_open"]][0], "INVALIDATED")

    def test_stats_count_invalidated_separately(self):
        self._run()
        from platform_api.strategy_catalog import StrategyCatalogRepository
        repo = StrategyCatalogRepository(connect_fn=lambda readonly=False: self.db.connect(readonly=readonly))
        [row] = [s for s in repo.list_strategies() if s["strategy_id"] == STRATEGY]
        out = row["outcomes"]
        self.assertEqual((out["invalidated"], out["closed"], out["stops"], out["open"]), (2, 1, 1, 1))
        self.assertEqual(out["win_rate"], 0.0)                                    # 0 of 1 real exit

    def test_future_cutoff_is_refused(self):
        from scripts.invalidate_stale_signals import invalidate
        with self.assertRaises(ValueError):
            invalidate(self.db.connect, before=NOW + timedelta(days=1), reason="x", operator="op", apply=False, now=NOW)


if __name__ == "__main__":
    unittest.main()
