import os
import uuid
import unittest

from postgres.config import PostgresConfig
from postgres.db import apply_migrations, connect, transaction
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


if __name__ == "__main__":
    unittest.main()
