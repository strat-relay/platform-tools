"""Dynamic Strategy Creation Pipeline tests.

Tests cover:
  - StrategyDefinition CRUD
  - StrategyVersion create, lifecycle, immutability once frozen
  - ParameterSet create and immutability once frozen
  - BacktestRun creation, async status transitions, deterministic result fingerprint
  - Discovery vs Validation distinction
  - Freeze: succeeds for valid artifacts, frozen version cannot mutate
  - New change requires new version / parameter_set
  - StrategyInstance creation
  - New instance defaults OFFLINE + execution ineligible
  - ONLINE toggle does not change execution_eligibility
  - Execution eligibility does not silently change ONLINE
  - Instrument configuration is instance-scoped
  - No broker writes

Unit tests run without PostgreSQL.  Integration tests are guarded by _server_available().
"""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
import uuid
from datetime import timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

from platform_api.strategy_mgmt import (  # noqa: E402
    BacktestJobRunner,
    StrategyMgmtApi,
    StrategyMgmtRepository,
    _fingerprint,
)


# ─── unit helpers ─────────────────────────────────────────────────────────────

class InMemoryDb:
    """Minimal in-memory replacement for a real DB connection; used for pure-unit tests."""

    def __init__(self):
        self._tables: dict[str, list[dict[str, Any]]] = {
            "strategy_definition": [],
            "strategy_version": [],
            "parameter_schema": [],
            "parameter_set": [],
            "backtest_run": [],
            "strategy_instance_v2": [],
        }

    # adapter used by tests only
    def insert(self, table: str, row: dict[str, Any]) -> None:
        self._tables[table].append(row)

    def find(self, table: str, **kwargs) -> dict[str, Any] | None:
        for row in self._tables[table]:
            if all(row.get(k) == v for k, v in kwargs.items()):
                return row
        return None

    def all(self, table: str) -> list[dict[str, Any]]:
        return list(self._tables[table])


def _make_repo() -> StrategyMgmtRepository:
    """Return a repository backed by a stub connect_fn for pure unit tests."""
    repo = StrategyMgmtRepository.__new__(StrategyMgmtRepository)
    repo._connect = None  # will be overridden per test
    return repo


def _api_with_stub_repo(repo: StrategyMgmtRepository) -> StrategyMgmtApi:
    runner = MagicMock()
    runner.submit = MagicMock()
    return StrategyMgmtApi(repository=repo, job_runner=runner)


# ─── pure unit tests (no postgres) ─────────────────────────────────────────

class FingerprintTests(unittest.TestCase):
    def test_deterministic(self):
        a = _fingerprint({"x": 1, "y": [2, 3]})
        b = _fingerprint({"y": [2, 3], "x": 1})
        self.assertEqual(a, b)

    def test_different_values_differ(self):
        self.assertNotEqual(_fingerprint({"x": 1}), _fingerprint({"x": 2}))

    def test_hex_sha256(self):
        fp = _fingerprint({"k": "v"})
        self.assertEqual(len(fp), 64)
        int(fp, 16)  # must be valid hex


class ApiRouteTests(unittest.TestCase):
    """Verify that strategy_mgmt routes don't collide with existing platform routes."""

    def setUp(self):
        self.repo = MagicMock()
        self.runner = MagicMock()
        self.api = StrategyMgmtApi(repository=self.repo, job_runner=self.runner)

    def test_unowned_paths_return_none(self):
        for path in ("/api/v1/strategies", "/api/v1/instruments",
                     "/api/v1/system", "/healthz", "/api/v1/signals"):
            result = self.api.handle("GET", path, None)
            self.assertIsNone(result, f"path {path!r} should not be owned by strategy_mgmt")

    def test_method_not_allowed_on_definitions_list(self):
        self.repo.list_definitions.return_value = []
        code, body = self.api.handle("DELETE", "/api/v1/strategy-definitions", None)
        self.assertEqual(code, 405)

    def test_definition_id_with_trailing_slash_not_matched_as_list(self):
        # /api/v1/strategy-definitions/ (trailing slash stripped) → list route
        self.repo.list_definitions.return_value = []
        result = self.api.handle("GET", "/api/v1/strategy-definitions/", None)
        # after rstrip it matches list path, which is OK
        self.assertIsNotNone(result)


class CreateDefinitionUnitTests(unittest.TestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.runner = MagicMock()
        self.api = StrategyMgmtApi(repository=self.repo, job_runner=self.runner)

    def _fake_row(self, **kw):
        base = {"id": str(uuid.uuid4()), "name": "x", "family_key": "fk",
                "description": None, "provenance_notes": None,
                "created_at": "2026-10-07T00:00:00+00:00",
                "updated_at": "2026-10-07T00:00:00+00:00", "created_by": "test"}
        base.update(kw)
        return base

    def test_create_definition_success(self):
        self.repo.create_definition.return_value = self._fake_row(name="My Strat", family_key="MY_STRAT")
        body = json.dumps({"name": "My Strat", "familyKey": "MY_STRAT"}).encode()
        code, resp = self.api.handle("POST", "/api/v1/strategy-definitions", body)
        self.assertEqual(code, 201)
        self.assertEqual(resp["data"]["name"], "My Strat")
        self.repo.create_definition.assert_called_once()

    def test_create_definition_missing_name_is_400(self):
        body = json.dumps({"familyKey": "FK"}).encode()
        code, _ = self.api.handle("POST", "/api/v1/strategy-definitions", body)
        self.assertEqual(code, 400)

    def test_create_definition_missing_family_key_is_400(self):
        body = json.dumps({"name": "X"}).encode()
        code, _ = self.api.handle("POST", "/api/v1/strategy-definitions", body)
        self.assertEqual(code, 400)

    def test_get_definition_not_found(self):
        self.repo.get_definition.return_value = None
        code, _ = self.api.handle("GET", "/api/v1/strategy-definitions/does-not-exist", None)
        self.assertEqual(code, 404)

    def test_list_definitions(self):
        self.repo.list_definitions.return_value = [self._fake_row()]
        code, resp = self.api.handle("GET", "/api/v1/strategy-definitions", None)
        self.assertEqual(code, 200)
        self.assertEqual(len(resp["data"]), 1)


class CreateVersionUnitTests(unittest.TestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.api = StrategyMgmtApi(repository=self.repo, job_runner=MagicMock())

    def _fake_version(self, lifecycle="DRAFT", **kw):
        base = {"id": str(uuid.uuid4()), "definition_id": str(uuid.uuid4()),
                "version_label": "V1", "evaluator_key": "kojo_wedge",
                "lifecycle": lifecycle, "schema_id": None, "release_notes": None,
                "frozen_at": None, "frozen_by": None,
                "created_at": "2026-10-07T00:00:00+00:00",
                "updated_at": "2026-10-07T00:00:00+00:00", "created_by": "test"}
        base.update(kw)
        return base

    def test_create_version_success(self):
        row = self._fake_version()
        self.repo.create_version.return_value = row
        body = json.dumps({"definitionId": row["definition_id"], "versionLabel": "V1",
                           "evaluatorKey": "kojo_wedge"}).encode()
        code, resp = self.api.handle("POST", "/api/v1/strategy-versions", body)
        self.assertEqual(code, 201)
        self.assertEqual(resp["data"]["lifecycle"], "DRAFT")

    def test_create_version_missing_required_fields(self):
        body = json.dumps({"versionLabel": "V1"}).encode()
        code, _ = self.api.handle("POST", "/api/v1/strategy-versions", body)
        self.assertEqual(code, 400)

    def test_freeze_version_success(self):
        ver_id = str(uuid.uuid4())
        frozen_row = self._fake_version(lifecycle="FROZEN", frozen_by="operator")
        self.repo.freeze_version.return_value = frozen_row
        body = json.dumps({"frozenBy": "operator"}).encode()
        code, resp = self.api.handle("POST", f"/api/v1/strategy-versions/{ver_id}/freeze", body)
        self.assertEqual(code, 200)
        self.assertEqual(resp["data"]["lifecycle"], "FROZEN")

    def test_frozen_version_not_found_is_404(self):
        self.repo.freeze_version.side_effect = KeyError("not found")
        body = json.dumps({}).encode()
        code, _ = self.api.handle("POST", f"/api/v1/strategy-versions/{uuid.uuid4()}/freeze", body)
        self.assertEqual(code, 404)

    def test_freeze_already_frozen_is_400(self):
        self.repo.freeze_version.side_effect = ValueError("already frozen")
        body = json.dumps({}).encode()
        code, _ = self.api.handle("POST", f"/api/v1/strategy-versions/{uuid.uuid4()}/freeze", body)
        self.assertEqual(code, 400)


class ParameterSetUnitTests(unittest.TestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.api = StrategyMgmtApi(repository=self.repo, job_runner=MagicMock())

    def _fake_ps(self, frozen=False, **kw):
        base = {"id": str(uuid.uuid4()), "parameter_set_id": "ps-test-1",
                "strategy_version_id": str(uuid.uuid4()), "schema_id": "test-schema",
                "values": {"threshold": 10}, "fingerprint": "abc123", "frozen": frozen,
                "frozen_at": None, "frozen_by": None,
                "created_at": "2026-10-07T00:00:00+00:00",
                "updated_at": "2026-10-07T00:00:00+00:00", "created_by": "test"}
        base.update(kw)
        return base

    def test_create_parameter_set(self):
        row = self._fake_ps()
        self.repo.create_parameter_set.return_value = row
        sv_id = str(uuid.uuid4())
        body = json.dumps({"parameterSetId": "ps-test-1", "strategyVersionId": sv_id,
                           "schemaId": "test-schema", "values": {"threshold": 10}}).encode()
        code, resp = self.api.handle("POST", "/api/v1/parameter-sets", body)
        self.assertEqual(code, 201)
        self.assertFalse(resp["data"]["frozen"])

    def test_create_parameter_set_values_must_be_object(self):
        body = json.dumps({"parameterSetId": "p1", "strategyVersionId": str(uuid.uuid4()),
                           "schemaId": "s", "values": [1, 2]}).encode()
        code, _ = self.api.handle("POST", "/api/v1/parameter-sets", body)
        self.assertEqual(code, 400)

    def test_immutable_parameter_set_is_400(self):
        self.repo.create_parameter_set.side_effect = ValueError("already exists with different fingerprint")
        body = json.dumps({"parameterSetId": "ps-test-1", "strategyVersionId": str(uuid.uuid4()),
                           "schemaId": "s", "values": {"threshold": 99}}).encode()
        code, _ = self.api.handle("POST", "/api/v1/parameter-sets", body)
        self.assertEqual(code, 400)


class BacktestRunUnitTests(unittest.TestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.runner = MagicMock()
        self.api = StrategyMgmtApi(repository=self.repo, job_runner=self.runner)

    def _fake_run(self, purpose="DISCOVERY", status="QUEUED", **kw):
        base = {"id": str(uuid.uuid4()), "strategy_version_id": str(uuid.uuid4()),
                "parameter_set_id": str(uuid.uuid4()), "backtest_purpose": purpose,
                "status": status, "instruments": ["XAUUSD"], "timeframes": ["M15"],
                "dataset_fingerprint": None, "result_fingerprint": None,
                "metrics": None, "error_detail": None, "engine_version": None,
                "queued_at": "2026-10-07T00:00:00+00:00",
                "started_at": None, "completed_at": None, "created_by": "test"}
        base.update(kw)
        return base

    def test_create_discovery_backtest(self):
        ver_id = str(uuid.uuid4())
        row = self._fake_run(purpose="DISCOVERY")
        self.repo.create_backtest_run.return_value = row
        body = json.dumps({"parameterSetId": str(uuid.uuid4()),
                           "backtestPurpose": "DISCOVERY", "instruments": ["XAUUSD"]}).encode()
        code, resp = self.api.handle("POST", f"/api/v1/strategy-versions/{ver_id}/backtests", body)
        self.assertEqual(code, 201)
        self.assertEqual(resp["data"]["backtest_purpose"], "DISCOVERY")

    def test_create_validation_backtest_distinct_from_discovery(self):
        ver_id = str(uuid.uuid4())
        row = self._fake_run(purpose="VALIDATION")
        self.repo.create_backtest_run.return_value = row
        body = json.dumps({"parameterSetId": str(uuid.uuid4()),
                           "backtestPurpose": "VALIDATION", "instruments": ["XAUUSD"]}).encode()
        code, resp = self.api.handle("POST", f"/api/v1/strategy-versions/{ver_id}/backtests", body)
        self.assertEqual(code, 201)
        self.assertEqual(resp["data"]["backtest_purpose"], "VALIDATION")

    def test_invalid_purpose_is_400(self):
        self.repo.create_backtest_run.side_effect = ValueError("invalid purpose")
        ver_id = str(uuid.uuid4())
        body = json.dumps({"parameterSetId": str(uuid.uuid4()),
                           "backtestPurpose": "NOPE"}).encode()
        code, _ = self.api.handle("POST", f"/api/v1/strategy-versions/{ver_id}/backtests", body)
        self.assertEqual(code, 400)

    def test_status_polling(self):
        run_id = str(uuid.uuid4())
        self.repo.get_backtest_run.return_value = self._fake_run(status="RUNNING")
        code, resp = self.api.handle("GET", f"/api/v1/backtests/{run_id}", None)
        self.assertEqual(code, 200)
        self.assertEqual(resp["data"]["status"], "RUNNING")

    def test_status_not_found_is_404(self):
        self.repo.get_backtest_run.return_value = None
        code, _ = self.api.handle("GET", f"/api/v1/backtests/{uuid.uuid4()}", None)
        self.assertEqual(code, 404)

    def test_async_job_submitted_when_compute_fn_provided(self):
        ver_id = str(uuid.uuid4())
        row = self._fake_run(id=str(uuid.uuid4()))
        self.repo.create_backtest_run.return_value = row
        body = json.dumps({
            "parameterSetId": str(uuid.uuid4()),
            "backtestPurpose": "DISCOVERY",
            "_evaluator_key": "threshold",
            "_parameter_values": {"threshold": 10},
            "_feed_events": [],
        }).encode()
        code, _ = self.api.handle("POST", f"/api/v1/strategy-versions/{ver_id}/backtests", body)
        self.assertEqual(code, 201)
        self.runner.submit.assert_called_once()


class BacktestJobRunnerUnitTests(unittest.TestCase):
    def test_queued_to_completed_transition(self):
        repo = MagicMock()
        repo.update_backtest_status.return_value = {"status": "COMPLETED"}
        runner = BacktestJobRunner(repo)
        results = []
        done = threading.Event()

        def compute():
            results.append("computed")
            return {"result_fingerprint": "fp123", "metrics": {"signal_count": 5},
                    "engine_version": "v1", "evaluator_fingerprint": "ef", "dataset_fingerprint": "df"}

        def _patched_run(run_id, fn):
            runner._run(run_id, fn)
            done.set()

        run_id = str(uuid.uuid4())
        runner._run(run_id, compute)

        calls = [str(c) for c in repo.update_backtest_status.call_args_list]
        self.assertTrue(any("RUNNING" in c for c in calls))
        self.assertTrue(any("COMPLETED" in c for c in calls))
        self.assertEqual(results, ["computed"])
        runner.shutdown(wait=False)

    def test_failed_transition_on_exception(self):
        repo = MagicMock()
        repo.update_backtest_status.return_value = {"status": "FAILED"}
        runner = BacktestJobRunner(repo)

        def compute():
            raise RuntimeError("backtest exploded")

        runner._run(str(uuid.uuid4()), compute)

        calls = [str(c) for c in repo.update_backtest_status.call_args_list]
        self.assertTrue(any("FAILED" in c for c in calls))
        self.assertTrue(any("backtest exploded" in c for c in calls))
        runner.shutdown(wait=False)

    def test_deduplicate_same_run_id(self):
        repo = MagicMock()
        repo.update_backtest_status.return_value = {"status": "COMPLETED"}
        runner = BacktestJobRunner(repo, max_workers=2)
        call_count = [0]
        lock = threading.Lock()

        def compute():
            with lock:
                call_count[0] += 1
            return {}

        run_id = str(uuid.uuid4())
        runner.submit(run_id, compute)
        runner.submit(run_id, compute)  # duplicate, should not be submitted
        runner._pool.shutdown(wait=True)
        self.assertEqual(call_count[0], 1)


class BacktestResultFingerprintTests(unittest.TestCase):
    """Deterministic result fingerprint: same inputs → same fingerprint."""

    def test_deterministic_fingerprint_from_engine(self):
        from strategy_backtest import (BacktestEngine, CostModel, EntrySignal,
                                        HistoricalMarketFeed, MarketEvent,
                                        ParameterSchema, ParameterSet,
                                        StrategyEvaluatorRegistry, StrategyVersion)

        class ThresholdEvaluator:
            def initialize(self, sv, ps):
                self.threshold = float(ps.values["threshold"])
                self.emitted = False

            def consume_market_event(self, ev):
                if self.emitted or ev.close <= self.threshold:
                    return ()
                self.emitted = True
                return (EntrySignal("SIG-1", sv.strategy_version_id, ev.canonical_instrument,
                                     "LONG", ev.close, ev.close - 1, ev.close + 2,
                                     ev.close_timestamp,
                                     provenance={"available_through": ev.close_timestamp}),)

            def snapshot_state(self):
                return {}

            def restore_state(self, s):
                pass

        def _ev(i, close):
            ts = 1_700_000_000 + i * 300
            return MarketEvent("XAUUSD", "M5", ts, ts + 300, close - 0.2, close + 0.5, close - 0.5, close)

        schema = ParameterSchema("s", {"threshold": {"required": True}})
        sv = StrategyVersion("TEST", "V1", "thr", schema, lifecycle="IMPLEMENTED")
        ps = ParameterSet("ps1", "TEST@V1", "s", {"threshold": 10})
        registry = StrategyEvaluatorRegistry()
        registry.register("thr", ThresholdEvaluator)
        events = tuple(_ev(i, 9 if i < 1 else 11) for i in range(4))
        feed = HistoricalMarketFeed(events, "fp-test")
        engine = BacktestEngine(registry)
        r1 = engine.run(sv, ps, feed, CostModel("zero"), run_id="run-a")
        r2 = engine.run(sv, ps, feed, CostModel("zero"), run_id="run-b")
        self.assertEqual(r1.result_fingerprint, r2.result_fingerprint)
        self.assertIsNotNone(r1.result_fingerprint)


class StrategyInstanceUnitTests(unittest.TestCase):
    def setUp(self):
        self.repo = MagicMock()
        self.api = StrategyMgmtApi(repository=self.repo, job_runner=MagicMock())

    def _fake_instance(self, online=False, execution_eligible=False, **kw):
        base = {"id": str(uuid.uuid4()), "strategy_version_id": str(uuid.uuid4()),
                "parameter_set_id": str(uuid.uuid4()), "display_name": "Test Instance",
                "online": online, "execution_eligible": execution_eligible,
                "instruments": [], "attributes": {},
                "created_at": "2026-10-07T00:00:00+00:00",
                "updated_at": "2026-10-07T00:00:00+00:00", "created_by": "test"}
        base.update(kw)
        return base

    def test_new_instance_defaults_offline_and_execution_ineligible(self):
        row = self._fake_instance(online=False, execution_eligible=False)
        self.repo.create_instance.return_value = row
        body = json.dumps({"strategyVersionId": str(uuid.uuid4()),
                           "parameterSetId": str(uuid.uuid4()),
                           "displayName": "My Instance"}).encode()
        code, resp = self.api.handle("POST", "/api/v1/strategy-instances", body)
        self.assertEqual(code, 201)
        self.assertFalse(resp["data"]["online"], "new instance must start OFFLINE")
        self.assertFalse(resp["data"]["execution_eligible"], "new instance must start execution_ineligible")

    def test_create_instance_missing_required_fields(self):
        body = json.dumps({"displayName": "X"}).encode()
        code, _ = self.api.handle("POST", "/api/v1/strategy-instances", body)
        self.assertEqual(code, 400)

    def test_online_toggle_does_not_change_execution_eligibility(self):
        inst_id = str(uuid.uuid4())
        before = self._fake_instance(online=False, execution_eligible=False)
        after = self._fake_instance(id=before["id"], online=True, execution_eligible=False)
        self.repo.patch_instance_online.return_value = after
        body = json.dumps({"online": True}).encode()
        code, resp = self.api.handle("PATCH", f"/api/v1/strategy-instances/{inst_id}", body)
        self.assertEqual(code, 200)
        self.assertTrue(resp["data"]["online"])
        self.assertFalse(resp["data"]["execution_eligible"],
                         "execution_eligible must not change when toggling ONLINE")

    def test_patch_refuses_execution_eligible_change(self):
        inst_id = str(uuid.uuid4())
        body = json.dumps({"executionEligible": True}).encode()
        code, resp = self.api.handle("PATCH", f"/api/v1/strategy-instances/{inst_id}", body)
        self.assertEqual(code, 400)
        self.assertIn("execution_eligible", resp["message"])

    def test_patch_also_refuses_snake_case_execution_eligible(self):
        inst_id = str(uuid.uuid4())
        body = json.dumps({"execution_eligible": True}).encode()
        code, resp = self.api.handle("PATCH", f"/api/v1/strategy-instances/{inst_id}", body)
        self.assertEqual(code, 400)

    def test_get_instance_not_found(self):
        self.repo.get_instance.return_value = None
        code, _ = self.api.handle("GET", f"/api/v1/strategy-instances/{uuid.uuid4()}", None)
        self.assertEqual(code, 404)

    def test_instrument_configuration_is_instance_scoped(self):
        inst_id = str(uuid.uuid4())
        row = self._fake_instance(instruments=[{"canonical_instrument": "XAUUSD", "state": "ACTIVE"}])
        self.repo.patch_instance_instruments.return_value = row
        body = json.dumps({"instruments": [{"canonical_instrument": "XAUUSD", "state": "ACTIVE"}]}).encode()
        code, resp = self.api.handle("PATCH", f"/api/v1/strategy-instances/{inst_id}", body)
        self.assertEqual(code, 200)
        self.assertEqual(len(resp["data"]["instruments"]), 1)
        self.repo.patch_instance_instruments.assert_called_once_with(
            inst_id,
            [{"canonical_instrument": "XAUUSD", "state": "ACTIVE"}],
            "api",
        )

    def test_execution_eligibility_does_not_silently_change_online(self):
        """execution_eligible column changes must never flip online."""
        inst_id = str(uuid.uuid4())
        # Simulate: someone tries to set execution_eligible directly via PATCH
        body = json.dumps({"execution_eligible": True}).encode()
        code, _ = self.api.handle("PATCH", f"/api/v1/strategy-instances/{inst_id}", body)
        self.assertEqual(code, 400)
        # online is never touched
        self.repo.patch_instance_online.assert_not_called()


class NoBrokerWritesTests(unittest.TestCase):
    """Assert that no broker write methods are present or called anywhere in strategy_mgmt."""

    def test_no_broker_write_references_in_module(self):
        import platform_api.strategy_mgmt as m
        source = Path(m.__file__).read_text(encoding="utf-8")
        forbidden = [
            "mt5_order_send", "mt5_position_modify", "broker_write",
            "execution_authority", "canary", "ENABLED",
        ]
        for token in forbidden:
            self.assertNotIn(token, source,
                             f"strategy_mgmt.py must not reference broker/execution token: {token!r}")

    def test_create_instance_never_changes_execution_eligible(self):
        repo = MagicMock()
        runner = MagicMock()
        api = StrategyMgmtApi(repository=repo, job_runner=runner)
        row = {"id": "i1", "online": False, "execution_eligible": False,
               "strategy_version_id": "v1", "parameter_set_id": "p1",
               "display_name": "X", "instruments": [], "attributes": {},
               "created_at": "2026-10-07T00:00:00+00:00",
               "updated_at": "2026-10-07T00:00:00+00:00", "created_by": "api"}
        repo.create_instance.return_value = row
        body = json.dumps({"strategyVersionId": "v1", "parameterSetId": "p1",
                           "displayName": "X"}).encode()
        api.handle("POST", "/api/v1/strategy-instances", body)
        # Ensure patch_instance_online was never called (no side-effect enabling execution)
        repo.patch_instance_online.assert_not_called()


class ControlApiWiringTests(unittest.TestCase):
    """Verify strategy_mgmt routes are reachable through PlatformControlApi.execute()."""

    def setUp(self):
        from platform_api.control import PlatformControlApi
        from platform_api.strategy_mgmt import StrategyMgmtApi
        self.mock_repo = MagicMock()
        self.mock_repo.list_definitions.return_value = []
        self.mgmt_api = StrategyMgmtApi(repository=self.mock_repo, job_runner=MagicMock())

        # Build a minimal PlatformControlApi with stubs for everything else
        mock_ctrl_repo = MagicMock()
        mock_ctrl_repo.runtime_setting_reader = None
        mock_ctrl_repo.platform_status.return_value = {"outbox_count": 0, "inbox_count": 0,
                                                         "orchestrator_running": 0}
        self.ctrl = PlatformControlApi(
            repository=mock_ctrl_repo,
            environ={"ORCHESTRATOR_MODE": "PRIMARY", "SIGNAL_AUTHORITY_MODE": "DB_PRIMARY",
                     "EXECUTION_AUTHORITY_MODE": "DISABLED"},
            strategy_mgmt_api=self.mgmt_api,
        )

    def test_strategy_definitions_route_reachable(self):
        code, _ = self.ctrl.execute("GET", "/api/v1/strategy-definitions")
        self.assertEqual(code, 200)

    def test_existing_strategies_route_not_broken(self):
        from platform_api.signals import CanonicalSourceUnavailable
        self.ctrl.strategy_catalog = MagicMock()
        self.ctrl.strategy_catalog.list_strategies.return_value = []
        code, _ = self.ctrl.execute("GET", "/api/v1/strategies")
        self.assertEqual(code, 200)


# ─── integration tests (postgres required) ────────────────────────────────

def _server_available() -> bool:
    try:
        from postgres.config import PostgresConfig
        from postgres.db import connect
        with connect(PostgresConfig.from_env()):
            return True
    except Exception:
        return False


@unittest.skipUnless(_server_available(), "PostgreSQL is not available; set TRADING_POSTGRES_DSN")
class StrategyMgmtIntegrationTests(unittest.TestCase):
    """Full pipeline test against a fresh database with all migrations applied."""

    @classmethod
    def setUpClass(cls):
        from test_integration_tm_membership_runner import FreshDatabase
        from postgres.db import apply_migrations
        cls.db = FreshDatabase()
        with cls.db.connect() as conn:
            apply_migrations(conn)
            conn.commit()
        cls.repo = StrategyMgmtRepository(connect_fn=cls.db.connect)
        cls.api = StrategyMgmtApi(repository=cls.repo)

    @classmethod
    def tearDownClass(cls):
        cls.db.drop()

    def test_full_pipeline_happy_path(self):
        """End-to-end: Definition → Version → ParameterSet → Backtest → Instance → ONLINE toggle."""

        # 1. Create strategy definition
        defn = self.repo.create_definition(
            name="Kojo Wedge Integration Test",
            family_key=f"KOJO_WEDGE_INTEG_{uuid.uuid4().hex[:8]}",
            description="Integration test strategy",
            provenance_notes="test run",
            created_by="test",
        )
        self.assertIn("id", defn)
        def_id = defn["id"]

        # 2. Create strategy version
        ver = self.repo.create_version(
            definition_id=def_id,
            version_label="V1",
            evaluator_key="kojo_wedge",
            schema_id="kojo-wedge-v1",
            release_notes="Initial version",
            created_by="test",
        )
        self.assertEqual(ver["lifecycle"], "DRAFT")
        ver_id = ver["id"]

        # 3. Create parameter set
        ps = self.repo.create_parameter_set(
            parameter_set_id=f"kojo-integ-ps-{uuid.uuid4().hex[:8]}",
            strategy_version_id=ver_id,
            schema_id="kojo-wedge-v1",
            values={"context_depth": 0.2, "atr_multiplier": 1.5},
            provenance={"source": "integration_test"},
            created_by="test",
        )
        self.assertFalse(ps["frozen"])
        ps_id = ps["id"]
        expected_fp = _fingerprint({
            "parameter_set_id": ps["parameter_set_id"],
            "strategy_version_id": ver_id,
            "schema_id": "kojo-wedge-v1",
            "values": {"context_depth": 0.2, "atr_multiplier": 1.5},
            "provenance": {"source": "integration_test"},
        })
        self.assertEqual(ps["fingerprint"], expected_fp, "fingerprint must be deterministic")

        # 4. Create a discovery backtest
        run = self.repo.create_backtest_run(
            strategy_version_id=ver_id,
            parameter_set_id=ps_id,
            backtest_purpose="DISCOVERY",
            instruments=["XAUUSD"],
            timeframes=["M15"],
            date_range_start=None,
            date_range_end=None,
            cost_model_fingerprint="zero-cost",
            created_by="test",
        )
        self.assertEqual(run["status"], "QUEUED")
        self.assertEqual(run["backtest_purpose"], "DISCOVERY")
        run_id = run["id"]

        # 5. Status transitions: QUEUED → RUNNING → COMPLETED
        self.repo.update_backtest_status(run_id, "RUNNING")
        mid = self.repo.get_backtest_run(run_id)
        self.assertEqual(mid["status"], "RUNNING")

        self.repo.update_backtest_status(
            run_id, "COMPLETED",
            result_fingerprint="rf123",
            metrics={"signal_count": 3, "win_rate": 0.67},
        )
        done = self.repo.get_backtest_run(run_id)
        self.assertEqual(done["status"], "COMPLETED")
        self.assertEqual(done["result_fingerprint"], "rf123")

        # 6. Deterministic fingerprint: same inputs → same result
        fp1 = _fingerprint({"signals": [], "outcomes": [], "metrics": {"signal_count": 3}})
        fp2 = _fingerprint({"signals": [], "outcomes": [], "metrics": {"signal_count": 3}})
        self.assertEqual(fp1, fp2)

        # 7. Create a validation backtest (distinct purpose)
        val_run = self.repo.create_backtest_run(
            strategy_version_id=ver_id, parameter_set_id=ps_id,
            backtest_purpose="VALIDATION", instruments=["XAUUSD"],
            timeframes=["M15"], date_range_start=None, date_range_end=None,
            cost_model_fingerprint=None, created_by="test",
        )
        self.assertEqual(val_run["backtest_purpose"], "VALIDATION")
        self.assertNotEqual(run_id, val_run["id"], "discovery and validation are distinct rows")

        # 8. Advance version to IMPLEMENTED so it can be frozen
        self.repo.set_version_lifecycle(ver_id, "IMPLEMENTED", "test")
        ver_impl = self.repo.get_version(ver_id)
        self.assertEqual(ver_impl["lifecycle"], "IMPLEMENTED")

        # 9. Freeze version
        frozen_ver = self.repo.freeze_version(ver_id, "test")
        self.assertEqual(frozen_ver["lifecycle"], "FROZEN")
        self.assertIsNotNone(frozen_ver["frozen_at"])

        # 10. Frozen version cannot mutate (DB trigger raises)
        import psycopg
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(Exception):
                    cur.execute(
                        "UPDATE strategy_mgmt.strategy_version SET release_notes = 'mutated' WHERE id = %s",
                        (ver_id,),
                    )
                    conn.commit()
            conn.rollback()

        # 11. Freeze parameter set
        frozen_ps = self.repo.freeze_parameter_set(ps_id, "test")
        self.assertTrue(frozen_ps["frozen"])

        # 12. Frozen parameter set cannot change values (DB trigger raises)
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(Exception):
                    cur.execute(
                        "UPDATE strategy_mgmt.parameter_set SET values = %s::jsonb WHERE id = %s",
                        (json.dumps({"context_depth": 0.99}), ps_id),
                    )
                    conn.commit()
            conn.rollback()

        # 13. Create strategy instance — must default OFFLINE + execution_ineligible
        inst = self.repo.create_instance(
            strategy_version_id=ver_id,
            parameter_set_id=ps_id,
            display_name="Kojo Integration Instance",
            created_by="test",
        )
        self.assertFalse(inst["online"], "new instance must start OFFLINE")
        self.assertFalse(inst["execution_eligible"], "new instance must start execution_ineligible")
        inst_id = inst["id"]

        # 14. Toggle ONLINE — execution_eligible must NOT change
        updated = self.repo.patch_instance_online(inst_id, True, "test")
        self.assertTrue(updated["online"])
        self.assertFalse(updated["execution_eligible"],
                         "execution_eligible must not change when going ONLINE")

        # 15. Toggle OFFLINE — execution_eligible must NOT change
        updated2 = self.repo.patch_instance_online(inst_id, False, "test")
        self.assertFalse(updated2["online"])
        self.assertFalse(updated2["execution_eligible"])

        # 16. DB trigger blocks execution_eligible change via direct UPDATE
        with self.db.connect() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(Exception):
                    cur.execute(
                        "UPDATE strategy_mgmt.strategy_instance_v2 SET execution_eligible = true WHERE id = %s",
                        (inst_id,),
                    )
                    conn.commit()
            conn.rollback()

        # 17. Instrument configuration is instance-scoped
        instruments = [{"canonical_instrument": "XAUUSD", "state": "ACTIVE"}]
        upd_inst = self.repo.patch_instance_instruments(inst_id, instruments, "test")
        self.assertEqual(len(upd_inst["instruments"]), 1)

        # 18. New version required for parameter changes (uniqueness constraint)
        with self.assertRaises(Exception):
            self.repo.create_version(
                definition_id=def_id,
                version_label="V1",  # duplicate label for same definition
                evaluator_key="kojo_wedge",
                schema_id=None,
                release_notes=None,
                created_by="test",
            )


if __name__ == "__main__":
    unittest.main()
