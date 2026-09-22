from __future__ import annotations

import json
import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from platform_api import signals as signal_api_module
from platform_api.signals import (
    CanonicalSignalRepository,
    CanonicalSourceUnavailable,
    PlatformSignalApi,
)


CANONICAL_ROW = {
    "signal_id": "SIG_POST_T0_CANONICAL",
    "candidate_id": "candidate-1",
    "evaluation_id": "eval-1",
    "strategy_ref": "LIQUIDITY_DISPLACEMENT_SCALP_V1@V1",
    "strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
    "strategy_version": "V1",
    "parameter_set_ref": None,
    "parameter_set_status": "LEGACY_IMPLICIT_IN_STRATEGY_ID",
    "strategy_instance_id": "instance-1",
    "instrument": "XAUUSDm",
    "direction": "LONG",
    "decision_time": "2026-09-18T12:00:00+00:00",
    "signal_emitted_at": "2026-09-18T12:00:01+00:00",
    "ingested_at": "2026-09-18T12:00:01+00:00",
    "entry_type": "MARKET",
    "entry_price": 2500.0,
    "stop_price": 2495.0,
    "risk_distance": 5.0,
    "target_price": 2510.0,
    "target_distance": 10.0,
    "target_r": 2.0,
    "economic_position_id": "position-1",
    "entry_opportunity_id": "opportunity-1",
    "setup_id": "setup-1",
    "source_event_id": "event-1",
    "source_id": "signal-orchestrator",
    "source_offset": None,
    "evidence_class": "CANONICAL_RUNTIME",
    "cutoff_id": "p2-cutoff-1",
    "source_provenance": {"source_state_reference": "runtime-state"},
    "evaluation_hash": "eval-hash",
    "trace_hash": "trace-hash",
    "terminal_state": "ENTRY_SIGNAL_CREATED",
    "strategy_metadata": {"entry_fraction": 0.5},
    "entry_signal_hash": "signal-hash",
    "entry_mechanisms": ["DEPTH_ONLY", "REJECTION_WICK"],
    "publication_state": "PUBLISHED",
    "published_at": "2026-09-18T12:00:01+00:00",
}


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = []
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.connection.statements.append((sql, params))
        if sql == "SET TRANSACTION READ ONLY":
            return
        if "platform.schema_migrations" in sql:
            self.rows = [("012",)] if self.connection.schema_ready else []
        elif sql.lstrip().startswith("SELECT s.signal_id"):
            self.rows = list(self.connection.rows)
            if "LIMIT %s OFFSET %s" in sql:
                limit, offset = params[-2:]
                self.rows = self.rows[offset:offset + limit]
        else:
            raise AssertionError(f"unexpected SQL in test: {sql}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows=(), *, schema_ready=True):
        self.rows = list(rows)
        self.schema_ready = schema_ready
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return FakeCursor(self)


class PlatformSignalApiTests(unittest.TestCase):
    def make_api(self, rows=(CANONICAL_ROW,), *, schema_ready=True):
        self.connection = FakeConnection(rows, schema_ready=schema_ready)
        repository = CanonicalSignalRepository(lambda **_kwargs: self.connection)
        return PlatformSignalApi(repository)

    def test_list_reads_postgres_and_exposes_provenance(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals")
        self.assertEqual(status, 200)
        self.assertEqual([row["signal_id"] for row in body["data"]], [CANONICAL_ROW["signal_id"]])
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["schema_version"], "012")
        self.assertEqual(self.connection.statements[0][0], "SET TRANSACTION READ ONLY")
        select_sql = next(sql for sql, _ in self.connection.statements if "SELECT s.signal_id" in sql)
        self.assertIn("FROM strategy.entry_signals AS s", select_sql)

    def test_relational_mechanisms_preserved_without_duplicate_parent_rows(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["data"]), 1)
        self.assertEqual(body["data"][0]["entry_mechanisms"], ["DEPTH_ONLY", "REJECTION_WICK"])
        select_sql = next(sql for sql, _ in self.connection.statements if "SELECT s.signal_id" in sql)
        self.assertIn("array_agg(m.mechanism ORDER BY m.position)", select_sql)
        self.assertIn("LEFT JOIN LATERAL", select_sql)

    def test_ordering_and_pagination_are_explicit(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals?limit=7&offset=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["meta"]["pagination"], {"limit": 7, "offset": 2, "returned": 0, "has_more": False})
        select_sql = next(sql for sql, _ in self.connection.statements if "SELECT s.signal_id" in sql)
        self.assertIn("ORDER BY s.decision_time DESC, s.signal_id ASC LIMIT %s OFFSET %s", select_sql)

    def test_detail_reads_canonical_postgres_row(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals/SIG_POST_T0_CANONICAL")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["source_id"], "signal-orchestrator")
        self.assertEqual(body["data"]["symbol"], "XAUUSDm")
        self.assertEqual(body["data"]["entry_price"], 2500.0)

    def test_unknown_id_is_explicit_not_found(self):
        api = self.make_api(rows=())
        status, body = api.execute("GET", "/api/v1/signals/unknown")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "RESOURCE_NOT_FOUND")

    def test_postgres_unavailable_does_not_fall_back_to_legacy_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_path = Path(directory) / "signals.jsonl"
            legacy_path.write_text(json.dumps({"signal_id": "LEGACY_PRE_T0"}) + "\n", encoding="utf-8")

            def unavailable(**_kwargs):
                raise OSError("database unavailable")

            api = PlatformSignalApi(CanonicalSignalRepository(unavailable))
            status, body = api.execute("GET", "/api/v1/signals")
            self.assertEqual(status, 503)
            self.assertEqual(body["error"], "SOURCE_UNAVAILABLE")
            self.assertNotIn("data", body)
            self.assertEqual(body["source"], "canonical_postgres")
            self.assertNotIn("LEGACY_PRE_T0", json.dumps(body))
            status, detail = api.execute("GET", "/api/v1/signals/LEGACY_PRE_T0")
            self.assertEqual(status, 503)
            self.assertNotIn("LEGACY_PRE_T0", json.dumps(detail))
            self.assertTrue(legacy_path.exists())
            self.assertNotIn("signals.jsonl", inspect.getsource(signal_api_module))

    def test_wrong_database_schema_is_unavailable(self):
        api = self.make_api(schema_ready=False)
        with self.assertRaises(CanonicalSourceUnavailable):
            api.repository.list_signals({})

    def test_invalid_pagination_is_rejected(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals?limit=10000")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "INVALID_QUERY")

    def test_http_server_allows_console_origin_and_preflight_only(self):
        api = self.make_api()
        server = signal_api_module.create_server(
            "127.0.0.1", 0, api=api,
            allowed_origins={"https://console.stratrelay.app"},
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = Request(
                f"{base}/api/v1/signals",
                headers={"Origin": "https://console.stratrelay.app"},
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Access-Control-Allow-Origin"],
                                 "https://console.stratrelay.app")
                self.assertEqual(response.headers["Vary"], "Origin")

            preflight = Request(
                f"{base}/api/v1/signals/SIG_POST_T0_CANONICAL",
                method="OPTIONS",
                headers={
                    "Origin": "https://console.stratrelay.app",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization,content-type",
                },
            )
            with urlopen(preflight) as response:
                self.assertEqual(response.status, 204)
                self.assertEqual(response.headers["Access-Control-Allow-Methods"], "GET, OPTIONS")
                self.assertEqual(response.headers["Access-Control-Allow-Headers"],
                                 "Authorization, Content-Type")

            denied = Request(
                f"{base}/api/v1/signals",
                headers={"Origin": "https://untrusted.example"},
            )
            with urlopen(denied) as response:
                self.assertEqual(response.status, 200)
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))

            rejected_header = Request(
                f"{base}/api/v1/signals",
                method="OPTIONS",
                headers={
                    "Origin": "https://console.stratrelay.app",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "x-unapproved-header",
                },
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(rejected_header)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
