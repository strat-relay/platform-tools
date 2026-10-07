"""Cross-feature integration on real PostgreSQL (migrations 024 TM mode, 025 instrument membership,
026 ManagedTrade lifecycle) plus the Context runner -> Trade Manager outcome contract.

The runner and the Trade Manager are not coupled directly: the runner's canonical outcome
projection writes strategy.entry_signal_outcomes, and TM lifecycle reconciliation reads it.
Each test class runs in its own freshly created database. Skipped without TRADING_POSTGRES_DSN.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from postgres.config import PostgresConfig  # noqa: E402
from postgres.db import MIGRATIONS, apply_migrations, connect  # noqa: E402

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
CUTOFF = "integration-cutoff"
STRATEGY = "CONTEXT_STRUCTURE_RETRACE_V1"
TM_NONE_1 = "TMV_ecaca5f080f9f79bb18cc936"


def _server_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


class FreshDatabase:
    """CREATE DATABASE on the configured server; yields a DSN for it; drops it afterwards."""

    def __init__(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        base = PostgresConfig.from_env().dsn
        self.name = f"integ_{uuid.uuid4().hex[:10]}"
        self.admin = psycopg.connect(base, autocommit=True)
        self.admin.execute(f'CREATE DATABASE "{self.name}"')
        self.dsn = make_conninfo(**{**conninfo_to_dict(base), "dbname": self.name})

    def connect(self, readonly: bool = False):
        return connect(PostgresConfig(dsn=self.dsn), readonly=readonly)

    def drop(self):
        self.admin.execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')
        self.admin.close()


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class MigrationChainTests(unittest.TestCase):
    def test_clean_database_migrates_through_028(self):
        db = FreshDatabase()
        try:
            with db.connect() as conn:
                applied = apply_migrations(conn)
            numbers = [name[:3] for name in applied]
            self.assertEqual(numbers[-6:], ["030", "031", "032", "033", "034", "035"])
            self.assertEqual(len(numbers), len(set(numbers)))
        finally:
            db.drop()

    def test_database_at_023_upgrades_through_028_and_mode_code_needs_024(self):
        from trade_management.mode import TradeManagerModeUnavailable, current_mode
        db = FreshDatabase()
        try:
            with tempfile.TemporaryDirectory() as td:
                for sql in sorted(MIGRATIONS.glob("*.sql")):
                    if sql.name[:3] <= "023":
                        shutil.copy(sql, td)
                with db.connect() as conn:
                    apply_migrations(conn, Path(td))
                    # The deploy-order hazard: mode-gated code against a 023 database fails closed.
                    with self.assertRaises(Exception) as raised:
                        current_mode(conn)
                    conn.rollback()
                    self.assertTrue(isinstance(raised.exception, TradeManagerModeUnavailable)
                                    or "trade_manager_mode" in str(raised.exception))
            with db.connect() as conn:
                upgraded = apply_migrations(conn)
                self.assertEqual([name[:3] for name in upgraded], ["024", "025", "026", "027", "028", "029",
                                                                    "030", "031", "032", "033", "034", "035"])
                self.assertEqual(current_mode(conn), "SHADOW")                     # 024 seed
                with conn.cursor() as cur:
                    cur.execute("""SELECT canonical_instrument FROM strategy.instrument_membership
                                  WHERE strategy_instance_id='phase6' AND state='ACTIVE' ORDER BY 1""")
                    self.assertEqual([r[0] for r in cur.fetchall()], ["BTCUSD", "ETHBTC", "EURUSD", "USDJPY", "XAUUSD"])  # 025/035 seed
                    cur.execute("SELECT to_regclass('trade_management.managed_trade_lifecycle_event')")
                    self.assertIsNotNone(cur.fetchone()[0])                         # 026
                    cur.execute("""SELECT canonical_instrument, provider_symbol FROM platform.instrument_provider_mapping
                                  WHERE provider='MT5' AND state='ACTIVE' ORDER BY 1""")
                    self.assertEqual(cur.fetchall(), [("BTCUSD", "BTCUSDm"), ("ETHBTC", "ETHBTCm"),
                                                      ("EURUSD", "EURUSDm"), ("USDJPY", "USDJPYm"),
                                                      ("XAUUSD", "XAUUSDm")])  # 027/035 seed
                self.assertEqual(apply_migrations(conn), [])                        # idempotent
        finally:
            db.drop()

    def test_database_at_037_upgrades_through_040_and_repairs_outbox(self):
        """Run the real migration runner from the production pre-039 schema.

        The legacy event type is seeded before 039 so this proves the repair uses
        the canonical outbox column without rewriting payload/history.  The
        second runner invocation proves checksum verification and idempotence.
        """
        db = FreshDatabase()
        try:
            with tempfile.TemporaryDirectory() as td:
                for sql in sorted(MIGRATIONS.glob("*.sql")):
                    if sql.name[:3] <= "037":
                        shutil.copy(sql, td)
                with db.connect() as conn:
                    applied = apply_migrations(conn, Path(td))
                    self.assertEqual([name[:3] for name in applied][-1], "037")
                    with conn.cursor() as cur:
                        cur.execute(
                            """INSERT INTO platform.outbox_events
                               (event_id, event_type, aggregate_type, aggregate_id,
                                schema_version, payload, occurred_at, publish_status,
                                attempts, last_error)
                            VALUES ('migration-039-regression', 'system.status_changed',
                                    'execution_authority', 'current', 'event-envelope.v1',
                                    '{\"preserved\": true}'::jsonb, now(), 'FAILED', 2,
                                    'unknown subject')"""
                        )
                    conn.commit()

                    upgraded = apply_migrations(conn)
                    self.assertEqual([name[:3] for name in upgraded], ["038", "039", "040"])
                    with conn.cursor() as cur:
                        cur.execute("""SELECT event_type, payload, publish_status, attempts, last_error
                                         FROM platform.outbox_events
                                        WHERE event_id='migration-039-regression'""")
                        self.assertEqual(cur.fetchone(),
                                         ("system.status_changed.v1", {"preserved": True},
                                          "FAILED", 2, "unknown subject"))
                        cur.execute("""SELECT column_name FROM information_schema.columns
                                        WHERE table_schema='trade_management'
                                          AND table_name='managed_trade'
                                          AND column_name IN ('time_exit_minutes', 'time_exit_at')
                                        ORDER BY column_name""")
                        self.assertEqual([row[0] for row in cur.fetchall()],
                                         ["time_exit_at", "time_exit_minutes"])
                        cur.execute("SELECT checksum_sha256 FROM platform.schema_migrations WHERE version='039'")
                        self.assertEqual(cur.fetchone()[0], hashlib.sha256(
                            (MIGRATIONS / "039_rename_system_status_changed_subject.sql").read_bytes()
                        ).hexdigest())
                    self.assertEqual(apply_migrations(conn), [])
        finally:
            db.drop()


class _Publisher:
    def __init__(self):
        self.published = []

    async def publish(self, envelope):
        self.published.append(envelope)


class _Provider:
    """Quote source for GBPUSD only; other instruments raise (the tick tolerates per-instrument failure)."""

    def __init__(self):
        self.bid = 1.2710
        self.ts = 0

    def quote(self, instrument):
        from trade_management.market_data import MarketQuote
        if instrument != "GBPUSD":
            raise LookupError(instrument)
        self.ts += 1
        return MarketQuote(instrument="GBPUSD", bid=self.bid, ask=self.bid + 0.0001,
                           source_timestamp=f"2026-09-26T12:00:{self.ts:02d}Z", provider_id="integration")

    def bars(self, instrument, timeframe="M5"):
        return None


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class CrossFeatureFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = FreshDatabase()
        cls.conn = cls.db.connect()
        apply_migrations(cls.conn)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.db.drop()

    def setUp(self):
        self.conn.rollback()

    # -- helpers -------------------------------------------------------------------------------

    def _signal(self, *, instrument="GBPUSD", entry=1.2700, stop=1.2685, target=1.2730):
        tag = uuid.uuid4().hex[:10]
        signal_id, position_id, opportunity_id = f"SIG_{tag}", f"POS_{tag}", f"OP_{tag}"
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.evaluations (evaluation_id, strategy_id, instrument, decision_time,
                decision, trace_fidelity, runtime_version, evaluator_version, canonical_payload, canonical_hash)
                VALUES (%s,%s,%s,%s,'SIGNAL','L1','t','t','{}'::jsonb,%s)""",
                        (f"EVAL_{tag}", STRATEGY, instrument, NOW, f"H_{tag}"))
            cur.execute("""INSERT INTO strategy.entry_signals (signal_id, candidate_id, evaluation_id, strategy_ref,
                strategy_id, strategy_version, strategy_instance_id, instrument, direction, decision_time, entry_price,
                stop_price, target_price, economic_position_id, entry_opportunity_id, cutoff_id,
                evaluation_hash, trace_hash, terminal_state, entry_signal_hash)
                VALUES (%s,%s,%s,%s,%s,'V1','phase6',%s,'LONG',%s,%s,%s,%s,%s,%s,%s,'e','t','ENTRY_SIGNAL_CREATED',%s)""",
                        (signal_id, f"CAND_{tag}", f"EVAL_{tag}", f"{STRATEGY}@V1", STRATEGY, instrument, NOW,
                         entry, stop, target, position_id, opportunity_id, CUTOFF, f"hash_{tag}"))
        self.conn.commit()
        return signal_id, position_id, opportunity_id

    def _create(self, signal_id):
        from trade_management.binding import DefaultTmNoneResolver
        from trade_management.managed_trade import create_managed_trade
        return create_managed_trade(self.conn, event_id=f"evt-{signal_id}", signal_id=signal_id,
                                    resolver=DefaultTmNoneResolver(tm_version_id=TM_NONE_1), now_utc=NOW)

    def _tick(self, provider):
        from trade_management.runtime.observation_runtime import observation_tick
        return asyncio.run(observation_tick(self.conn, _Publisher(), provider))

    def _one(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        self.conn.rollback()
        return row

    def _count(self, table, trade_id):
        return self._one(f"SELECT count(*) FROM trade_management.{table} WHERE managed_trade_id=%s", (trade_id,))[0]

    def _runner_target_hit(self, position_id, opportunity_id):
        """The corrected Context runner closes an OPEN position after its setup was invalidated, then
        projects the canonical outcome through the unchanged projector."""
        import context_structure_retrace_forward as fwd
        from context_structure_retrace_compact_state import project_state
        from context_structure_retrace_outcome_projector import project_entry_only_outcomes
        from test_context_runner_position_lifecycle import FILL, TARGET_R, bar, position, state_with_filled_setup
        state = state_with_filled_setup()
        setup = state["setups"]["SETUP1"]
        pos = setup["opportunities"][0]
        pos.update({"economic_position_id": position_id, "entry_opportunity_id": opportunity_id})
        state["positions"] = {position_id: pos}
        with tempfile.TemporaryDirectory() as td, \
                patch.object(fwd, "STATE", Path(td) / "state.json"), patch.object(fwd, "EVENTS", Path(td) / "events.jsonl"):
            fwd._EVENT_IDENTITY_CACHE_PATH, fwd._EVENT_IDENTITIES = None, set()
            fwd._process_bar(state, "EURUSDm", bar(FILL + 300, 1.0998, 1.1012, 1.1004), 0, [], {}, {}, "LIVE_FORWARD")
            self.assertEqual(setup["status"], "INVALIDATED_NO_REENTRY")          # would have orphaned before 6df818c
            state = json.loads(json.dumps(project_state(state), default=str))    # checkpoint + restart
            fwd._process_bar(state, "EURUSDm", bar(FILL + 600, 1.1001, 1.1032, 1.1030), 0, [], {}, {}, "LIVE_FORWARD")
        result = project_entry_only_outcomes(project_state(state), connect_fn=lambda: self.db.connect(),
                                             environ={"ENTRY_OUTCOME_SIGNAL_CUTOFF_ID": CUTOFF}, clock=lambda: NOW)
        self.assertGreaterEqual(result["projected"], 1)
        return TARGET_R, FILL + 600

    # -- the flow ------------------------------------------------------------------------------

    def test_membership_signal_shadow_trade_runner_outcome_closes_and_stops_work(self):
        from platform_api.instrument_membership import InstrumentMembershipRepository
        from trade_management.decision_engine import record_decision
        from trade_management.mode import current_mode
        import context_structure_retrace_forward as fwd

        # 1. Provider mapping in the database (027), then the instrument added & enabled for the
        #    instance (025, canonical identity). The runner resolves the provider symbol from the DB.
        repo = InstrumentMembershipRepository(connect_fn=lambda readonly=False: self.db.connect(readonly=readonly))
        mapped = repo.save_mapping("GBPUSD", "GBPUSDm", "FX", actor="integration")
        self.assertIn("GBPUSD", [c.canonical_instrument for c in repo.list_catalog()])
        self.assertEqual(mapped["revision"], 1)
        saved = repo.save_membership(STRATEGY, "phase6", "gbpusd", "ACTIVE", None, "integration")
        self.assertEqual((saved["canonical_instrument"], saved["state"], saved["revision"]), ("GBPUSD", "ACTIVE", 1))
        with patch.dict(os.environ, {"TRADING_POSTGRES_DSN": self.db.dsn}):
            symbols, revision = fwd.load_active_membership(Namespace(symbols=["XAUUSDm"]))
        self.assertIn("GBPUSDm", symbols)
        self.assertEqual(revision, 1)

        # 2. Signal -> ManagedTrade in SHADOW (virtual: no execution rows, no broker identity).
        self.assertEqual(current_mode(self.conn), "SHADOW")
        self.conn.rollback()
        signal_id, position_id, opportunity_id = self._signal()
        created = self._create(signal_id)
        trade_id = created.managed_trade_id
        self.assertEqual((created.status, created.reason), ("CREATED", None))
        self.assertEqual(self._one("SELECT state, record_mode FROM trade_management.managed_trade WHERE managed_trade_id=%s",
                                   (trade_id,)), ("OPEN", "SHADOW"))
        self.assertEqual(self._one("SELECT count(*) FROM execution_v2.execution_intent WHERE entry_signal_id=%s",
                                   (signal_id,))[0], 0)

        # 3. Virtual trade observed and evaluated.
        provider = _Provider()
        summary = self._tick(provider)
        self.assertEqual(summary["mode"], "SHADOW")
        self.assertEqual(self._count("trade_observation", trade_id), 1)
        obs_id = self._one("""SELECT observation_id FROM trade_management.trade_observation
                             WHERE managed_trade_id=%s""", (trade_id,))[0]
        self.assertEqual(record_decision(self.conn, observation_id=obs_id, event_id=obs_id, now_utc=NOW).status, "RECORDED")
        provider.bid = 1.2712
        self._tick(provider)                        # a second observation, queued but not yet decided
        self.assertEqual(self._count("trade_observation", trade_id), 2)
        queued = self._one("""SELECT observation_id FROM trade_management.trade_observation
                             WHERE managed_trade_id=%s AND observation_seq=2""", (trade_id,))[0]

        # 4. Runner -> canonical TARGET_HIT -> TM reconciliation closes the trade before work selection.
        target_r, exit_epoch = self._runner_target_hit(position_id, opportunity_id)
        status, realized_r, exit_ts = self._one("""SELECT status, realized_r, exit_timestamp
            FROM strategy.entry_signal_outcomes WHERE signal_id=%s""", (signal_id,))
        self.assertEqual(status, "TARGET_HIT")
        self.assertAlmostEqual(float(realized_r), target_r, places=9)
        provider.bid = 1.2740
        summary = self._tick(provider)
        self.assertEqual(summary["closed_by_strategy_outcome"], 1)
        event = self._one("""SELECT previous_state, new_state, reason, strategy_outcome, realized_r, exit_timestamp
            FROM trade_management.managed_trade_lifecycle_event WHERE managed_trade_id=%s""", (trade_id,))
        self.assertEqual(event[:4], ("OPEN", "CLOSED", "STRATEGY_OUTCOME", "TARGET_HIT"))
        self.assertAlmostEqual(float(event[4]), target_r, places=9)
        self.assertEqual(event[5], datetime.fromtimestamp(exit_epoch, timezone.utc))

        # 5. No subsequent observation or decision; history intact.
        self._tick(provider)
        self.assertEqual(self._count("trade_observation", trade_id), 2)
        self.assertEqual(record_decision(self.conn, observation_id=queued, event_id=queued, now_utc=NOW).status,
                         "TRADE_NOT_OPEN")
        self.assertEqual(self._count("trade_manager_decision", trade_id), 1)

    def test_off_prevents_processing_and_shadow_resumes(self):
        from trade_management.mode import read_mode_record, set_mode
        record = read_mode_record(self.conn)
        self.conn.rollback()
        set_mode(self.conn, "OFF", expected_revision=int(record["revision"]), changed_by="integration")
        try:
            signal_id, *_ = self._signal()
            skipped = self._create(signal_id)
            self.assertEqual((skipped.status, skipped.reason), ("SKIPPED", "TRADE_MANAGER_OFF"))
            self.assertEqual(self._tick(_Provider()), {"mode": "OFF", "skipped": True})
            self.assertEqual(self._one("SELECT count(*) FROM trade_management.managed_trade WHERE entry_signal_id=%s",
                                       (signal_id,))[0], 0)
        finally:
            current = read_mode_record(self.conn)
            self.conn.rollback()
            set_mode(self.conn, "SHADOW", expected_revision=int(current["revision"]), changed_by="integration")


if __name__ == "__main__":
    unittest.main()
