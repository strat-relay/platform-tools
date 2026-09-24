from __future__ import annotations

import json
import inspect
import tempfile
import threading
import unittest
from datetime import datetime
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
    "outcome_type": "ENTRY_ONLY",
    "outcome": "TARGET_HIT",
    "realized_r": 0.75,
    "exit_timestamp": "2026-09-18T12:30:00+00:00",
    "outcome_source": "CONTEXT_STRUCTURE_RETRACE_V1",
}

# Column order platform_api/signals.py's `_execution_audit` SELECT uses - a plain tuple stands in
# for a real cursor.description entry (`column[0]` is used when a column has no `.name`, matching
# how psycopg's own description entries are accessed elsewhere in this fake).
_AUDIT_COLUMNS = (
    "entry_signal_id", "execution_intent_id", "account_id", "intent_status", "block_reason",
    "risk_policy_version", "policy_fingerprint", "risk_per_trade", "account_equity",
    "risk_budget_usd", "stop_distance", "broker_volume_min", "broker_volume_step",
    "broker_volume_max", "calculated_volume", "submitted_volume", "estimated_loss_usd",
    "daily_loss_used", "concurrent_positions_used", "concurrent_orders_used",
    "signal_age_seconds", "max_signal_age_seconds", "canary_consumed", "canary_max",
    "attempt_id", "attempt_state", "result_outcome", "broker_order_id", "broker_deal_id",
)


def audit_row(**overrides) -> tuple:
    """One execution_v2.execution_intent (+ risk_evidence/attempt/result) join row, in
    _AUDIT_COLUMNS order. Every field not given defaults to None, matching a LEFT JOIN that found
    nothing on that side."""
    values = {col: None for col in _AUDIT_COLUMNS}
    values.update(overrides)
    return tuple(values[col] for col in _AUDIT_COLUMNS)


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
        stripped = sql.lstrip()
        if sql == "SET TRANSACTION READ ONLY":
            return
        if "platform.schema_migrations" in sql:
            version = params[0]
            self.rows = [(version,)] if self.connection.schema_ready and version in {"012", "015", "021"} else []
        elif stripped.startswith("SELECT s.signal_id"):
            self.rows = list(self.connection.rows)
            if "ORDER BY (CASE WHEN outcomes.status IS NULL" in sql:
                # Simulates the real ORDER BY, since test rows are dicts already shaped like the
                # real projected output (an "outcome" key, ISO-8601 "decision_time" strings which
                # sort correctly lexicographically) - applied as three stable sorts, least to
                # most significant, since the real clause mixes ascending (open-first) and
                # descending (newest decision_time first) directions.
                self.rows = sorted(self.rows, key=lambda r: r.get("signal_id") or "")
                self.rows = sorted(self.rows, key=lambda r: r.get("decision_time") or "", reverse=True)
                self.rows = sorted(self.rows, key=lambda r: 0 if r.get("outcome") in (None, "OPEN") else 1)
            if "LIMIT %s OFFSET %s" in sql:
                limit, offset = params[-2:]
                self.rows = self.rows[offset:offset + limit]
        elif stripped.startswith("SELECT COUNT(*)"):
            # This fake doesn't simulate WHERE filtering (tests pre-set exactly the rows a
            # given case cares about) - the count query gets the same unfiltered total.
            self.rows = [(len(self.connection.rows),)]
        elif "SELECT VALUE FROM PLATFORM.SYSTEM_METADATA" in sql.upper():
            self.rows = [(self.connection.audit_cutoff,)] if self.connection.audit_cutoff is not None else []
        elif stripped.startswith("SELECT i.entry_signal_id"):
            self.description = [(name,) for name in _AUDIT_COLUMNS]
            self.rows = list(self.connection.audit_rows)
        elif "SELECT SIGNAL_ID, DECISION_TIME FROM STRATEGY.ENTRY_SIGNALS" in sql.upper():
            ids = set(params[0])
            # A real timestamptz column comes back as a datetime, not the ISO string CANONICAL_ROW
            # stores it as - matters here specifically because the audit-cutoff comparison
            # (`decision_time < cutoff_dt`) would silently no-op (caught TypeError -> before_cutoff
            # = False) against a string, masking exactly the behavior under test below.
            self.rows = [
                (r["signal_id"], datetime.fromisoformat(str(r["decision_time"]).replace("Z", "+00:00")))
                for r in self.connection.rows if r["signal_id"] in ids and r.get("decision_time")
            ]
        else:
            raise AssertionError(f"unexpected SQL in test: {sql}")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows=(), *, schema_ready=True, audit_rows=(), audit_cutoff=None):
        self.rows = list(rows)
        self.schema_ready = schema_ready
        self.statements = []
        # Every existing test predates the execution-audit join (platform_api/signals.py's
        # _execution_audit) - defaulting to "no execution_v2 rows at all" reproduces exactly what
        # those tests already assume (every signal is NOT_EVALUATED) without having to touch them.
        self.audit_rows = list(audit_rows)
        self.audit_cutoff = audit_cutoff

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return FakeCursor(self)


class PlatformSignalApiTests(unittest.TestCase):
    def make_api(self, rows=(CANONICAL_ROW,), *, schema_ready=True, audit_rows=(), audit_cutoff=None):
        self.connection = FakeConnection(rows, schema_ready=schema_ready, audit_rows=audit_rows,
                                         audit_cutoff=audit_cutoff)
        repository = CanonicalSignalRepository(lambda **_kwargs: self.connection)
        return PlatformSignalApi(repository)

    def test_list_reads_postgres_and_exposes_provenance(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals")
        self.assertEqual(status, 200)
        self.assertEqual([row["signal_id"] for row in body["data"]], [CANONICAL_ROW["signal_id"]])
        self.assertEqual(body["source"], "canonical_postgres")
        self.assertEqual(body["schema_version"], "012")
        self.assertEqual(body["outcome_schema_version"], "015")
        self.assertEqual(body["data"][0]["outcome"], "TARGET_HIT")
        self.assertEqual(body["data"][0]["realized_r"], 0.75)
        self.assertEqual(body["data"][0]["exit_timestamp"], "2026-09-18T12:30:00+00:00")
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
        self.assertEqual(body["meta"]["pagination"],
                         {"limit": 7, "offset": 2, "returned": 0, "has_more": False, "total": 1})
        select_sql = next(sql for sql, _ in self.connection.statements if "SELECT s.signal_id" in sql)
        self.assertIn(
            "ORDER BY (CASE WHEN outcomes.status IS NULL OR outcomes.status = 'OPEN' THEN 0 ELSE 1 END),"
            " s.decision_time DESC, s.signal_id ASC LIMIT %s OFFSET %s",
            select_sql,
        )
        count_sql = next(sql for sql, _ in self.connection.statements if sql.lstrip().startswith("SELECT COUNT(*)"))
        self.assertIn("FROM strategy.entry_signals AS s", count_sql)

    def test_open_signals_sort_before_resolved_ones_regardless_of_decision_time(self):
        # An older OPEN signal must still sort before a newer resolved one.
        older_open = {**CANONICAL_ROW, "signal_id": "SIG_OLDER_OPEN", "decision_time": "2026-01-01T00:00:00+00:00", "outcome": None}
        newer_resolved = {**CANONICAL_ROW, "signal_id": "SIG_NEWER_RESOLVED", "decision_time": "2026-09-01T00:00:00+00:00", "outcome": "TARGET_HIT"}
        api = self.make_api(rows=(newer_resolved, older_open))
        status, body = api.execute("GET", "/api/v1/signals")
        self.assertEqual(status, 200)
        self.assertEqual([row["signal_id"] for row in body["data"]], ["SIG_OLDER_OPEN", "SIG_NEWER_RESOLVED"])

    def test_explicit_open_outcome_sorts_the_same_as_a_never_evaluated_signal(self):
        explicit_open = {**CANONICAL_ROW, "signal_id": "SIG_EXPLICIT_OPEN", "decision_time": "2026-01-01T00:00:00+00:00", "outcome": "OPEN"}
        resolved = {**CANONICAL_ROW, "signal_id": "SIG_RESOLVED", "decision_time": "2026-09-01T00:00:00+00:00", "outcome": "STOPPED"}
        api = self.make_api(rows=(resolved, explicit_open))
        status, body = api.execute("GET", "/api/v1/signals")
        self.assertEqual(status, 200)
        self.assertEqual([row["signal_id"] for row in body["data"]], ["SIG_EXPLICIT_OPEN", "SIG_RESOLVED"])

    def test_within_the_open_group_newest_decision_time_sorts_first(self):
        older_open = {**CANONICAL_ROW, "signal_id": "SIG_OLDER_OPEN", "decision_time": "2026-01-01T00:00:00+00:00", "outcome": None}
        newer_open = {**CANONICAL_ROW, "signal_id": "SIG_NEWER_OPEN", "decision_time": "2026-06-01T00:00:00+00:00", "outcome": None}
        api = self.make_api(rows=(older_open, newer_open))
        status, body = api.execute("GET", "/api/v1/signals")
        self.assertEqual(status, 200)
        self.assertEqual([row["signal_id"] for row in body["data"]], ["SIG_NEWER_OPEN", "SIG_OLDER_OPEN"])

    def test_list_rows_carry_an_execution_summary_derived_from_the_join(self):
        rows = (audit_row(entry_signal_id=CANONICAL_ROW["signal_id"], execution_intent_id="INTENT_1",
                          account_id="188428665", intent_status="BLOCKED",
                          block_reason="MINIMUM_LOT_EXCEEDS_RISK_LIMIT"),)
        api = self.make_api(audit_rows=rows)
        status, body = api.execute("GET", "/api/v1/signals")
        self.assertEqual(status, 200)
        summary = body["data"][0]["executionSummary"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["decision"], "REJECTED")
        self.assertEqual(summary[0]["reason"], "MINIMUM_LOT_EXCEEDS_RISK_LIMIT")
        self.assertEqual(summary[0]["account"], "•••••8665")

    def test_detail_reads_canonical_postgres_row(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals/SIG_POST_T0_CANONICAL")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["source_id"], "signal-orchestrator")
        self.assertEqual(body["data"]["symbol"], "XAUUSDm")
        self.assertEqual(body["data"]["entry_price"], 2500.0)

    def test_detail_with_no_execution_intent_is_not_evaluated_not_rejected(self):
        api = self.make_api()
        status, body = api.execute("GET", "/api/v1/signals/SIG_POST_T0_CANONICAL")
        self.assertEqual(status, 200)
        evaluations = body["data"]["executionEvaluations"]
        self.assertEqual(len(evaluations), 1)
        self.assertEqual(evaluations[0]["decision"], "NOT_EVALUATED")
        self.assertEqual(evaluations[0]["reason"], "NO_EXECUTION_INTENT")

    def test_detail_before_the_audit_cutoff_is_not_evaluated_for_a_documented_reason(self):
        api = self.make_api(audit_cutoff={"cutoff_utc": "2026-09-19T00:00:00+00:00"})
        # CANONICAL_ROW's decision_time (2026-09-18) is before the configured cutoff.
        status, body = api.execute("GET", "/api/v1/signals/SIG_POST_T0_CANONICAL")
        self.assertEqual(status, 200)
        evaluations = body["data"]["executionEvaluations"]
        self.assertEqual(evaluations[0]["decision"], "NOT_EVALUATED")
        self.assertEqual(evaluations[0]["reason"], "HISTORICAL_AUDIT_UNAVAILABLE")

    def test_detail_exposes_full_risk_evidence_and_broker_result(self):
        rows = (audit_row(entry_signal_id=CANONICAL_ROW["signal_id"], execution_intent_id="INTENT_1",
                          account_id="188428665", intent_status="COMPLETED",
                          risk_policy_version=3, risk_per_trade=0.02, account_equity=10000.0,
                          risk_budget_usd=200.0, calculated_volume=0.01, submitted_volume=0.01,
                          attempt_id="ATT_1", attempt_state="CONFIRMED",
                          result_outcome="FILLED", broker_order_id="BRK_1"),)
        api = self.make_api(audit_rows=rows)
        status, body = api.execute("GET", "/api/v1/signals/SIG_POST_T0_CANONICAL")
        self.assertEqual(status, 200)
        evaluation = body["data"]["executionEvaluations"][0]
        self.assertEqual(evaluation["decision"], "EXECUTED")
        self.assertEqual(evaluation["policy"]["version"], 3)
        self.assertEqual(evaluation["riskEvaluation"]["risk_budget_usd"], 200.0)
        self.assertEqual(evaluation["sizing"]["submitted_volume"], 0.01)
        self.assertEqual(evaluation["execution"]["attemptId"], "ATT_1")
        self.assertEqual(evaluation["brokerResult"]["broker_order_id"], "BRK_1")

    def test_execution_authority_disabled_is_skipped_not_rejected(self):
        rows = (audit_row(entry_signal_id=CANONICAL_ROW["signal_id"], execution_intent_id="INTENT_1",
                          account_id="188428665", intent_status="BLOCKED",
                          block_reason="EXECUTION_AUTHORITY_DISABLED"),)
        api = self.make_api(audit_rows=rows)
        status, body = api.execute("GET", "/api/v1/signals/SIG_POST_T0_CANONICAL")
        self.assertEqual(status, 200)
        evaluation = body["data"]["executionEvaluations"][0]
        self.assertEqual(evaluation["decision"], "SKIPPED")
        self.assertEqual(evaluation["humanReason"], "Execution authority disabled")

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
