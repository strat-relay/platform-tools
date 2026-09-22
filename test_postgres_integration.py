import hashlib
import os
import uuid
import unittest

from postgres.config import PostgresConfig
from postgres.db import MIGRATIONS, apply_migrations, connect, transaction
from postgres.phase6 import Phase6Store, canonical_hash


def _database_available() -> bool:
    try:
        with connect(PostgresConfig.from_env()) as conn:
            return True
    except Exception:
        return False


@unittest.skipUnless(_database_available(), "PostgreSQL is not available; run through Compose")
class PostgreSQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect(PostgresConfig.from_env())
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_expected_schemas_and_tables_exist(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name = ANY(%s)", (["platform", "research", "strategy", "orchestration", "execution", "trade_management", "audit", "telemetry"],))
            self.assertEqual({row[0] for row in cur.fetchall()}, {"platform", "research", "strategy", "orchestration", "execution", "trade_management", "audit", "telemetry"})
            cur.execute("SELECT to_regclass(%s), to_regclass(%s), to_regclass(%s)", ("strategy.phase6_setups", "strategy.phase6_economic_positions", "platform.schema_migrations"))
            self.assertEqual(cur.fetchone(), ("strategy.phase6_setups", "strategy.phase6_economic_positions", "platform.schema_migrations"))
            cur.execute("SELECT to_regclass(%s), to_regclass(%s), to_regclass(%s), to_regclass(%s), to_regclass(%s)",
                        ("strategy.symbol_progress", "strategy.setups", "strategy.setup_lifecycle",
                         "strategy.entry_opportunities", "strategy.economic_positions"))
            self.assertEqual(cur.fetchone(), ("strategy.symbol_progress", "strategy.setups", "strategy.setup_lifecycle",
                                              "strategy.entry_opportunities", "strategy.economic_positions"))
            cur.execute("SELECT to_regclass(%s)", ("strategy.entry_signal_outcomes",))
            self.assertEqual(cur.fetchone()[0], "strategy.entry_signal_outcomes")

    def test_migrations_are_idempotent_and_checksummed(self):
        self.assertEqual(apply_migrations(self.conn), [])

    def test_transaction_rolls_back(self):
        try:
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("INSERT INTO platform.system_metadata(key,value) VALUES ('rollback-test','{}'::jsonb)")
                    raise RuntimeError("intentional test rollback")
        except RuntimeError:
            pass
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM platform.system_metadata WHERE key='rollback-test'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_stable_ids_are_idempotent(self):
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO audit.execution_safety_invariants(invariant_id,description,enforced_by) VALUES ('test-id','test','test') ON CONFLICT DO NOTHING")
                cur.execute("INSERT INTO audit.execution_safety_invariants(invariant_id,description,enforced_by) VALUES ('test-id','test','test') ON CONFLICT DO NOTHING")
                cur.execute("SELECT count(*) FROM audit.execution_safety_invariants WHERE invariant_id='test-id'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("DELETE FROM audit.execution_safety_invariants WHERE invariant_id='test-id'")

    def test_working_set_rows_and_duplicate_fixture_are_idempotent(self):
        suffix = uuid.uuid4().hex[:12]
        state = {"schema": "test-schema", "strategy_version": f"TEST_STRATEGY_{suffix}",
                 "symbols": {"TESTUSD": {"last_m5": "1", "last_m15": "2", "initialized": True,
                                            "last_candle": "2026-01-01T00:00:00Z"}},
                 "runner_status": "STOPPED", "kill_switch": "OFF"}
        manifest = {"strategy_version": state["strategy_version"], "schema_version": state["schema"],
                    "configuration_hash": f"config-{suffix}", "configuration": {"test": True},
                    "freeze_timestamp": "2026-01-01T00:00:00Z"}
        hashes = {"state": canonical_hash(state), "manifest": canonical_hash(manifest), "events": f"events-{suffix}"}
        batch_id = f"batch-{suffix}"
        setup = {"setup_id": f"setup-{suffix}", "market_event_id": f"event-{suffix}", "symbol": "TESTUSD",
                 "direction": "LONG", "pattern": "TEST", "status": "FILLED", "retrace_state": "FILLED",
                 "entry_level": 100, "theoretical_entry": 100, "spread_at_detection": 1,
                 "target_completed": False, "event_bar": {}, "provenance": {}}
        opportunity = {"entry_opportunity_id": f"opp-{suffix}", "entry_attempt_id": f"attempt-{suffix}",
                       "economic_position_id": f"pos-{suffix}", "setup_id": setup["setup_id"], "symbol": "TESTUSD",
                       "direction": "LONG", "pattern": "TEST", "status": "OPEN", "stop": 95, "target": 105,
                       "executable_paper_entry": 100, "mfe_price": 0, "mae_price": 0, "fill_timestamp": 1,
                       "fill_timestamp_iso": "2026-01-01T00:00:00Z", "geometry": {}, "leg_a": {}, "leg_b": {},
                       "provenance": {}, "reentry_type": "INITIAL"}
        boundary = {"boundary_id": f"boundary-{suffix}", "captured_at": "2026-01-01T00:00:00Z",
                    "event_cutoff": "2026-01-01T00:00:00Z", "source_files": {"compact": "fixture"},
                    "source_hashes": hashes, "symbol_cursors": state["symbols"],
                    "archive_refs": {"full_state": "excluded"}}
        event = {"event_id": f"lifecycle-{suffix}", "setup_id": setup["setup_id"],
                 "economic_position_id": opportunity["economic_position_id"], "event_time": "2026-01-01T00:00:00Z",
                 "type": "FILLED", "source": "TEST"}
        observation = {"event_id": f"obs-event-{suffix}", "timestamp": "2026-01-01T00:00:00Z", "symbol": "TESTUSD",
                       "payload": "disposable"}

        def insert_fixture():
            with transaction(self.conn):
                store = Phase6Store(self.conn)
                store.batch(batch_id, hashes, {"mode": "WORKING_SET"})
                ids = store.identity(state, manifest, hashes)
                store.setup(dict(setup, _identity=dict(ids, batch_id=batch_id)), state["strategy_version"])
                store.opportunity(dict(opportunity, _identity=dict(ids, batch_id=batch_id)), legacy_phase6=False)
                store.lifecycle(event, event["event_id"], batch_id=batch_id)
                oid = store.observation(observation, kind="TEST", source_file="fixture", line=1)
                store.observation(observation, kind="TEST", source_file="fixture", line=1)
                store.working_set_boundary(boundary, {"safe_to_import": True})
                return ids, oid

        insert_fixture()
        with self.conn.cursor() as cur:
            cur.execute("""SELECT
                (SELECT count(*) FROM platform.strategy_versions WHERE strategy_version_id=%s),
                (SELECT count(*) FROM platform.configuration_versions WHERE configuration_version_id=%s),
                (SELECT count(*) FROM platform.freeze_manifests WHERE freeze_manifest_id=%s),
                (SELECT count(*) FROM strategy.setups WHERE setup_id=%s),
                (SELECT count(*) FROM strategy.entry_opportunities WHERE entry_opportunity_id=%s),
                (SELECT count(*) FROM strategy.economic_positions WHERE economic_position_id=%s),
                (SELECT count(*) FROM strategy.phase6_lifecycle_events WHERE event_id=%s),
                (SELECT count(*) FROM research.phase6_observations WHERE content_sha256=%s),
                (SELECT count(*) FROM platform.migration_boundaries WHERE boundary_id=%s)""",
                        (state["strategy_version"], manifest["configuration_hash"], canonical_hash(manifest),
                         setup["setup_id"], opportunity["entry_opportunity_id"], opportunity["economic_position_id"],
                         event["event_id"], canonical_hash(observation), boundary["boundary_id"]))
            before = cur.fetchone()
        insert_fixture()
        with self.conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM strategy.setups WHERE setup_id=%s;
                           """, (setup["setup_id"],))
            after_setup = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM strategy.entry_opportunities WHERE entry_opportunity_id=%s", (opportunity["entry_opportunity_id"],))
            after_opp = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM strategy.economic_positions WHERE economic_position_id=%s", (opportunity["economic_position_id"],))
            after_pos = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM strategy.phase6_lifecycle_events WHERE event_id=%s", (event["event_id"],))
            after_event = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM research.phase6_observations WHERE content_sha256=%s", (canonical_hash(observation),))
            after_observation = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM platform.migration_boundaries WHERE boundary_id=%s", (boundary["boundary_id"],))
            after_boundary = cur.fetchone()[0]
        self.assertEqual(before, (1, 1, 1, 1, 1, 1, 1, 1, 1))
        self.assertEqual((after_setup, after_opp, after_pos, after_event, after_observation, after_boundary), (1, 1, 1, 1, 1, 1))

    def test_working_set_transaction_failure_does_not_commit_partial_rows(self):
        suffix = uuid.uuid4().hex[:12]
        try:
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("INSERT INTO platform.system_metadata(key,value) VALUES (%s,'{}'::jsonb)", (f"partial-{suffix}",))
                    cur.execute("INSERT INTO platform.migration_boundaries(boundary_id,mode,captured_at,event_cutoff) VALUES (%s,'WORKING_SET',now(),now())", (f"boundary-{suffix}",))
                    raise RuntimeError("intentional working-set failure")
        except RuntimeError:
            pass
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM platform.system_metadata WHERE key=%s", (f"partial-{suffix}",))
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT count(*) FROM platform.migration_boundaries WHERE boundary_id=%s", (f"boundary-{suffix}",))
            self.assertEqual(cur.fetchone()[0], 0)


@unittest.skipUnless(_database_available(), "PostgreSQL is not available; run through Compose")
class TradeManagementRealPostgresTests(unittest.TestCase):
    """P4.2 real-PostgreSQL proof (architecture/p4-2-managed-trade integration): migration 013
    applies cleanly, the new tables/constraints/indexes exist, immutability/append-only
    protections actually enforce at the database level (not just in the in-process fake used by
    tests/test_trade_management_*.py), and P2's schema is untouched. No trade_management/*.py
    code is imported here - this exercises the DDL directly, independent of the application
    layer, against an isolated instance (never production PostgreSQL)."""

    @classmethod
    def setUpClass(cls):
        cls.conn = connect(PostgresConfig.from_env())
        apply_migrations(cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        # Defensive: a query error in one test (e.g. a genuine SQL bug) leaves psycopg's shared
        # class-level connection in an aborted-transaction state; without this, that failure
        # would cascade into every subsequent test in the class as a spurious
        # InFailedSqlTransaction rather than each test's own real outcome.
        self.conn.rollback()

    def _tm_version(self, suffix: str, *, status: str = "FROZEN") -> str:
        manifest_hash = hashlib.sha256(f"test-manifest-{suffix}".encode()).hexdigest()
        tm_version_id = f"TMV_{manifest_hash[:24]}"
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO trade_management.trade_manager_version
                    (tm_version_id, evaluator_id, label, manifest, manifest_hash, status)
                    VALUES (%s,%s,%s,%s::jsonb,%s,%s)""",
                           (tm_version_id, "tm-none.v1", f"TEST-{suffix}", "{}", manifest_hash, status))
        return tm_version_id

    def _entry_signal(self, suffix: str) -> str:
        evaluation_id = f"eval-{suffix}"
        signal_id = f"sig-{suffix}"
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO strategy.evaluations
                    (evaluation_id, strategy_id, instrument, decision_time, decision, trace_fidelity,
                     runtime_version, evaluator_version, canonical_payload, canonical_hash)
                    VALUES (%s,'TEST_STRAT','XAUUSD', now(), 'SIGNAL', 'L0', 'test.v1', 'test.v1',
                            '{}'::jsonb, %s)""",
                           (evaluation_id, f"canonhash-{suffix}"))
                cur.execute("""INSERT INTO strategy.entry_signals
                    (signal_id, candidate_id, evaluation_id, strategy_ref, strategy_id, strategy_version,
                     instrument, decision_time, evaluation_hash, trace_hash, terminal_state, entry_signal_hash,
                     entry_price, stop_price, target_price)
                    VALUES (%s,%s,%s,'TEST_STRAT@V1','TEST_STRAT','V1','XAUUSD', now(), %s, %s, 'ENTRY_SIGNAL_CREATED',
                            %s, 100, 99, 103)""",
                           (signal_id, f"cand-{suffix}", evaluation_id, f"evalhash-{suffix}",
                            f"tracehash-{suffix}", f"entryhash-{suffix}"))
        return signal_id

    def test_entry_signal_outcomes_fk_and_open_closed_integrity(self):
        signal_id = self._entry_signal(f"outcome-{uuid.uuid4().hex}")
        with self.conn.cursor() as cur:
            cur.execute("SAVEPOINT entry_outcome_integrity")
            try:
                cur.execute("""INSERT INTO strategy.entry_signal_outcomes
                    (signal_id, outcome_type, status, source)
                    VALUES (%s, 'ENTRY_ONLY', 'OPEN', 'CONTEXT_STRUCTURE_RETRACE_V1')""",
                            (f"missing-signal-{uuid.uuid4().hex}",))
                self.fail("outcome rows must reference an immutable EntrySignal")
            except Exception as exc:
                self.assertEqual(getattr(exc, "sqlstate", None), "23503")
                cur.execute("ROLLBACK TO SAVEPOINT entry_outcome_integrity")
            cur.execute("""SELECT entry_signal_hash FROM strategy.entry_signals
                WHERE signal_id = %s""", (signal_id,))
            entry_signal_hash = cur.fetchone()[0]

            cur.execute("""INSERT INTO strategy.entry_signal_outcomes
                (signal_id, outcome_type, status, source)
                VALUES (%s, 'ENTRY_ONLY', 'OPEN', 'CONTEXT_STRUCTURE_RETRACE_V1')""",
                        (signal_id,))

            cur.execute("SAVEPOINT entry_outcome_terminal_check")
            try:
                cur.execute("""UPDATE strategy.entry_signal_outcomes
                    SET status = 'TARGET_HIT'
                    WHERE signal_id = %s""", (signal_id,))
                self.fail("terminal outcomes require realized R and exit timestamp")
            except Exception as exc:
                self.assertEqual(getattr(exc, "sqlstate", None), "23514")
                cur.execute("ROLLBACK TO SAVEPOINT entry_outcome_terminal_check")

            cur.execute("""UPDATE strategy.entry_signal_outcomes
                SET status = 'TARGET_HIT', realized_r = 0.75,
                    exit_timestamp = now(), updated_at = now()
                WHERE signal_id = %s""", (signal_id,))
            cur.execute("""SELECT entry_signal_hash FROM strategy.entry_signals
                WHERE signal_id = %s""", (signal_id,))
            self.assertEqual(cur.fetchone()[0], entry_signal_hash)
            cur.execute("DELETE FROM strategy.entry_signal_outcomes WHERE signal_id = %s", (signal_id,))
        self.conn.commit()

    def _managed_trade(self, suffix: str, *, tm_version_id: str, signal_id: str) -> str:
        managed_trade_id = f"MT-{suffix}"
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO trade_management.managed_trade
                    (managed_trade_id, entry_signal_id, entry_signal_hash, strategy_id, strategy_version,
                     strategy_ref, instrument, direction, decision_time, reference_entry_price, initial_stop,
                     initial_target, tm_version_id, tm_binding_id, binding_hash, tm_bound_at,
                     binding_resolution, evidence_mode, eligibility)
                    VALUES (%s,%s,%s,'TEST_STRAT','V1','TEST_STRAT@V1','XAUUSD','LONG', now(), 100, 99, 103,
                            %s,%s,%s, now(), 'DEFAULT_TM_NONE', 'FORWARD', 'ELIGIBILITY_UNEVALUATED')""",
                           (managed_trade_id, signal_id, f"entryhash-{suffix}", tm_version_id,
                            f"bind-{suffix}", f"bindhash-{suffix}"))
        return managed_trade_id

    def test_migrations_through_014_are_applied_and_recorded(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT version FROM platform.schema_migrations WHERE version = ANY(%s)",
                        (["011", "012", "013", "014"],))
            self.assertEqual({row[0] for row in cur.fetchall()}, {"011", "012", "013", "014"})

    def test_tm_none_1_production_seed_is_registered_frozen_and_shadow_only(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT evaluator_id, label, manifest_hash, status
                          FROM trade_management.trade_manager_version
                          WHERE tm_version_id = 'TMV_ecaca5f080f9f79bb18cc936'""")
            row = cur.fetchone()
        self.assertIsNotNone(row, "TM-NONE-1 must be seeded by 014_tm_none_1_seed.sql")
        evaluator_id, label, manifest_hash, status = row
        self.assertEqual(evaluator_id, "tm-none.v1")
        self.assertEqual(label, "TM-NONE-1")
        self.assertEqual(manifest_hash, "ecaca5f080f9f79bb18cc936fb3867420c416eb372f998f3ec7c175369b5ac0d")
        self.assertEqual(status, "FROZEN")
        with self.conn.cursor() as cur:
            cur.execute("""SELECT publication_eligibility FROM trade_management.tm_version_promotion
                          WHERE tm_version_id = 'TMV_ecaca5f080f9f79bb18cc936'""")
            self.assertEqual([r[0] for r in cur.fetchall()], ["SHADOW_ONLY"])

    def test_tm_none_1_seed_is_idempotent_on_reapplication(self):
        # Re-running the seed's own statements directly (not through the checksum-gated
        # migration runner) must still be a safe no-op - the mission's "deterministic/
        # idempotent" requirement, proven independent of apply_migrations()'s own bookkeeping.
        seed_sql = (MIGRATIONS / "014_tm_none_1_seed.sql").read_text(encoding="utf-8")
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute(seed_sql)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM trade_management.trade_manager_version WHERE tm_version_id = 'TMV_ecaca5f080f9f79bb18cc936'")
            self.assertEqual(cur.fetchone()[0], 1)
            cur.execute("SELECT count(*) FROM trade_management.tm_version_promotion WHERE tm_version_id = 'TMV_ecaca5f080f9f79bb18cc936'")
            self.assertEqual(cur.fetchone()[0], 1)

    def test_migration_reapplication_is_a_safe_noop(self):
        self.assertEqual(apply_migrations(self.conn), [])
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM trade_management.trade_manager_version")
            cur.fetchone()  # table still queryable; reapplication did not corrupt it

    def test_expected_tables_constraints_and_indexes_exist(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT to_regclass(x) FROM unnest(%s::text[]) AS x""",
                        (["trade_management.trade_manager_version", "trade_management.tm_version_promotion",
                          "trade_management.legacy_stream_binding", "trade_management.managed_trade",
                          "trade_management.managed_trade_skip", "trade_management.managed_trade_quarantine",
                          "trade_management.market_snapshot", "trade_management.trade_observation",
                          "trade_management.trade_manager_decision", "trade_management.publication_decision"],))
            self.assertTrue(all(row[0] is not None for row in cur.fetchall()))
            cur.execute("""SELECT indexname FROM pg_indexes WHERE schemaname='trade_management'
                          AND indexname IN ('managed_trade_strategy_idx', 'managed_trade_state_idx',
                                            'market_snapshot_instrument_idx', 'trade_manager_decision_trade_idx')""")
            self.assertEqual({row[0] for row in cur.fetchall()},
                             {"managed_trade_strategy_idx", "managed_trade_state_idx",
                              "market_snapshot_instrument_idx", "trade_manager_decision_trade_idx"})

    def test_managed_trade_binding_and_geometry_columns_are_immutable(self):
        suffix = uuid.uuid4().hex[:12]
        tm_version_id = self._tm_version(suffix)
        signal_id = self._entry_signal(suffix)
        managed_trade_id = self._managed_trade(suffix, tm_version_id=tm_version_id, signal_id=signal_id)

        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE trade_management.managed_trade SET decision_time = now() WHERE managed_trade_id = %s",
                               (managed_trade_id,))
        other_tm_version_id = self._tm_version(suffix + "-other")
        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE trade_management.managed_trade SET tm_version_id = %s WHERE managed_trade_id = %s",
                               (other_tm_version_id, managed_trade_id))

        # Mutable columns (state bookkeeping) remain writable.
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE trade_management.managed_trade SET last_observation_seq = 1 WHERE managed_trade_id = %s",
                           (managed_trade_id,))
        with self.conn.cursor() as cur:
            cur.execute("SELECT last_observation_seq, decision_time FROM trade_management.managed_trade WHERE managed_trade_id = %s",
                       (managed_trade_id,))
            seq, _ = cur.fetchone()
            self.assertEqual(seq, 1)

    def test_trade_manager_version_immutable_except_frozen_to_retired(self):
        suffix = uuid.uuid4().hex[:12]
        tm_version_id = self._tm_version(suffix, status="FROZEN")

        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE trade_management.trade_manager_version SET manifest = '{\"x\":1}'::jsonb WHERE tm_version_id = %s",
                               (tm_version_id,))

        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("UPDATE trade_management.trade_manager_version SET status = 'RETIRED' WHERE tm_version_id = %s",
                           (tm_version_id,))
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM trade_management.trade_manager_version WHERE tm_version_id = %s", (tm_version_id,))
            self.assertEqual(cur.fetchone()[0], "RETIRED")

        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("DELETE FROM trade_management.trade_manager_version WHERE tm_version_id = %s",
                               (tm_version_id,))

    def test_legacy_stream_binding_is_append_only(self):
        suffix = uuid.uuid4().hex[:12]
        tm_version_id = self._tm_version(suffix)
        binding_id = f"BIND-{suffix}"
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO trade_management.legacy_stream_binding
                    (binding_id, strategy_id, tm_version_id, valid_from, binding_hash)
                    VALUES (%s,'TEST_STRAT',%s, now(), %s)""",
                           (binding_id, tm_version_id, f"bindhash-{suffix}"))
        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE trade_management.legacy_stream_binding SET binding_hash = 'changed' WHERE binding_id = %s",
                               (binding_id,))
        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("DELETE FROM trade_management.legacy_stream_binding WHERE binding_id = %s", (binding_id,))

    def test_observation_uniqueness_prevents_silent_overwrite(self):
        # No explicit trigger on trade_observation (A6/A7 rely on the content-addressed
        # observation_id PK + no ON CONFLICT clause, per docs/p4_2_managed_trade/README.md);
        # this proves that protection actually holds at the database level: a second INSERT of
        # the same observation_id is rejected, not silently accepted as an overwrite.
        suffix = uuid.uuid4().hex[:12]
        tm_version_id = self._tm_version(suffix)
        signal_id = self._entry_signal(suffix)
        managed_trade_id = self._managed_trade(suffix, tm_version_id=tm_version_id, signal_id=signal_id)
        snapshot_id = f"MSN-{suffix}"
        observation_id = f"TOBS-{suffix}"
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO trade_management.market_snapshot
                    (market_snapshot_id, provider_id, instrument, source_timestamp, bid, ask, data_status, quote_hash)
                    VALUES (%s,'test-provider','XAUUSD', now(), 100, 100.2, 'FORWARD', %s)""",
                           (snapshot_id, f"quotehash-{suffix}"))
                cur.execute("""INSERT INTO trade_management.trade_observation
                    (observation_id, managed_trade_id, observation_seq, market_snapshot_id, tm_version_id,
                     observed_at, effective_at, data_status, payload_hash)
                    VALUES (%s,%s,1,%s,%s, now(), now(), 'FORWARD', %s)""",
                           (observation_id, managed_trade_id, snapshot_id, tm_version_id, f"payloadhash-{suffix}"))
        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("""INSERT INTO trade_management.trade_observation
                        (observation_id, managed_trade_id, observation_seq, market_snapshot_id, tm_version_id,
                         observed_at, effective_at, data_status, payload_hash)
                        VALUES (%s,%s,2,%s,%s, now(), now(), 'FORWARD', %s)""",
                               (observation_id, managed_trade_id, snapshot_id, tm_version_id, "different-hash"))

    def test_trade_manager_decision_is_immutable(self):
        suffix = uuid.uuid4().hex[:12]
        tm_version_id = self._tm_version(suffix)
        signal_id = self._entry_signal(suffix)
        managed_trade_id = self._managed_trade(suffix, tm_version_id=tm_version_id, signal_id=signal_id)
        snapshot_id = f"MSN-{suffix}"
        observation_id = f"TOBS-{suffix}"
        decision_id = f"TMD-{suffix}"
        with transaction(self.conn):
            with self.conn.cursor() as cur:
                cur.execute("""INSERT INTO trade_management.market_snapshot
                    (market_snapshot_id, provider_id, instrument, source_timestamp, bid, ask, data_status, quote_hash)
                    VALUES (%s,'test-provider','XAUUSD', now(), 100, 100.2, 'FORWARD', %s)""",
                           (snapshot_id, f"quotehash-{suffix}"))
                cur.execute("""INSERT INTO trade_management.trade_observation
                    (observation_id, managed_trade_id, observation_seq, market_snapshot_id, tm_version_id,
                     observed_at, effective_at, data_status, payload_hash)
                    VALUES (%s,%s,1,%s,%s, now(), now(), 'FORWARD', %s)""",
                           (observation_id, managed_trade_id, snapshot_id, tm_version_id, f"payloadhash-{suffix}"))
                cur.execute("""INSERT INTO trade_management.trade_manager_decision
                    (decision_id, managed_trade_id, tm_version_id, observation_id, observation_seq,
                     action, decision_time, data_status)
                    VALUES (%s,%s,%s,%s,1,'HOLD', now(), 'FORWARD')""",
                           (decision_id, managed_trade_id, tm_version_id, observation_id))
                cur.execute("""INSERT INTO trade_management.publication_decision(decision_id, outcome, reason)
                              VALUES (%s,'WITHHELD','NOT_ACTIONABLE_HOLD')""", (decision_id,))
        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE trade_management.trade_manager_decision SET action = 'EXIT' WHERE decision_id = %s",
                               (decision_id,))
        with self.assertRaises(Exception):
            with transaction(self.conn):
                with self.conn.cursor() as cur:
                    cur.execute("DELETE FROM trade_management.trade_manager_decision WHERE decision_id = %s", (decision_id,))

    def test_p2_schema_and_relational_mechanisms_remain_intact(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT to_regclass('strategy.entry_signals'), to_regclass('strategy.entry_signal_mechanisms'),
                                 to_regclass('strategy.evaluations'), to_regclass('platform.outbox_events'),
                                 to_regclass('platform.inbox_events')""")
            self.assertEqual(cur.fetchone(), ("strategy.entry_signals", "strategy.entry_signal_mechanisms",
                                              "strategy.evaluations", "platform.outbox_events", "platform.inbox_events"))
            cur.execute("""SELECT column_name FROM information_schema.columns
                          WHERE table_schema='strategy' AND table_name='entry_signals' AND column_name='entry_signal_hash'""")
            self.assertIsNotNone(cur.fetchone())

    def test_no_forbidden_columns_exist_in_the_new_schema(self):
        with self.conn.cursor() as cur:
            cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                          WHERE table_schema = 'trade_management'
                            AND (column_name ILIKE '%%account%%' OR column_name ILIKE '%%ticket%%'
                                 OR column_name ILIKE '%%lot%%' OR column_name ILIKE '%%broker_position%%'
                                 OR column_name ILIKE 'execution\\_%%' OR column_name ILIKE '%%entitle%%'
                                 OR column_name ILIKE '%%subscription%%' OR column_name ILIKE '%%customer%%'
                                 OR column_name ILIKE 'published\\_%%')""")
            self.assertEqual(cur.fetchall(), [])


if __name__ == "__main__":
    unittest.main()
