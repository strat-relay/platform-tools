"""Dynamic Strategy Creation Pipeline — API layer.

Provides the intake/creation path:
  StrategyDefinition → StrategyVersion → ParameterSchema → ParameterSet
  → BacktestRun → Validate/Freeze → StrategyInstance → Instruments → ONLINE

ARCHITECTURAL INVARIANTS (enforced here and in the DB):
  * StrategyVersion is immutable once frozen.
  * ParameterSet is immutable once frozen.
  * New StrategyInstance always starts with online=False, execution_eligible=False,
    execution_mode='OFF'.
  * PATCH /strategy-instances/{id} can toggle `online` only.
  * PATCH /strategy-instances/{id}/execution-mode changes execution_mode (OFF/SHADOW/LIVE).
  * Execution eligibility is NEVER changed here — it belongs to the authority system.
  * SHADOW→LIVE transition requires system-level execution authority preflight.
  * execution_mode_revision increments on every execution_mode change.
  * BROKER_WRITES = 0.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from postgres.db import connect as _default_connect


# ─── helpers ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _row_to_dict(cursor, row) -> dict[str, Any]:
    cols = [desc[0] for desc in cursor.description]
    return dict(zip(cols, row))


def _serialize(v: Any) -> Any:
    """Make a value JSON-safe for API responses."""
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, list):
        return [_serialize(x) for x in v]
    if isinstance(v, dict):
        return {k: _serialize(vv) for k, vv in v.items()}
    return v


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _serialize(v) for k, v in row.items()}


class ExecutionModeRevisionConflict(Exception):
    """execution_mode_revision did not match the expected value."""


VALID_EXECUTION_MODES = frozenset({"OFF", "SHADOW", "LIVE"})


# ─── repository ─────────────────────────────────────────────────────────────

class StrategyMgmtRepository:
    """Read/write access to the strategy_mgmt schema.  One repository per API instance."""

    SCHEMA_MIGRATION = "042"

    def __init__(self, connect_fn: Callable[..., Any] = _default_connect):
        self._connect = connect_fn

    def _conn(self, readonly: bool = False):
        return self._connect(readonly=readonly)

    def _check_schema(self, cur) -> None:
        cur.execute(
            "SELECT version FROM platform.schema_migrations WHERE version = %s",
            (self.SCHEMA_MIGRATION,),
        )
        if cur.fetchone() is None:
            raise RuntimeError(f"strategy_mgmt schema migration {self.SCHEMA_MIGRATION} not applied")

    # ── strategy definitions ─────────────────────────────────────────────────

    def create_definition(self, *, name: str, family_key: str, description: str | None,
                          provenance_notes: str | None, created_by: str) -> dict[str, Any]:
        row_id = str(uuid.uuid4())
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO strategy_mgmt.strategy_definition
                        (id, name, family_key, description, provenance_notes, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       RETURNING *""",
                    (row_id, name, family_key, description, provenance_notes, created_by),
                )
                row = _row_to_dict(cur, cur.fetchone())
            conn.commit()
        return _serialize_row(row)

    def list_definitions(self) -> list[dict[str, Any]]:
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM strategy_mgmt.strategy_definition ORDER BY created_at DESC")
                return [_serialize_row(_row_to_dict(cur, r)) for r in cur.fetchall()]

    def list_registry(self) -> list[dict[str, Any]]:
        """Return the unified registry with versions, parameter sets, and instances.

        Legacy identifiers are retained in instance attributes during migration 044 so
        callers can correlate the unified rows with existing signal and statistics APIs.
        """
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                self._check_schema(cur)
                cur.execute("""
                    SELECT d.id AS definition_id, d.name, d.family_key, d.description,
                           v.id AS version_id, v.version_label, v.evaluator_key, v.lifecycle,
                           i.id AS instance_id, i.display_name AS instance_display_name,
                           i.online, i.execution_eligible, i.instruments, i.attributes,
                           p.parameter_set_id, p.fingerprint AS parameter_fingerprint,
                           p.values AS parameter_values
                    FROM strategy_mgmt.strategy_definition d
                    LEFT JOIN strategy_mgmt.strategy_version v
                      ON v.definition_id = d.id
                    LEFT JOIN strategy_mgmt.strategy_instance_v2 i
                      ON i.strategy_version_id = v.id
                    LEFT JOIN strategy_mgmt.parameter_set p
                      ON p.id = i.parameter_set_id
                    ORDER BY d.created_at, v.created_at, i.created_at
                """)
                rows = [_serialize_row(_row_to_dict(cur, r)) for r in cur.fetchall()]
        return rows

    def get_definition(self, definition_id: str) -> dict[str, Any] | None:
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM strategy_mgmt.strategy_definition WHERE id = %s",
                    (definition_id,),
                )
                row = cur.fetchone()
                return _serialize_row(_row_to_dict(cur, row)) if row else None

    # ── strategy versions ────────────────────────────────────────────────────

    def create_version(self, *, definition_id: str, version_label: str, evaluator_key: str,
                       schema_id: str | None, release_notes: str | None,
                       created_by: str) -> dict[str, Any]:
        row_id = str(uuid.uuid4())
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO strategy_mgmt.strategy_version
                        (id, definition_id, version_label, evaluator_key, schema_id, release_notes, created_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       RETURNING *""",
                    (row_id, definition_id, version_label, evaluator_key,
                     schema_id, release_notes, created_by),
                )
                row = _row_to_dict(cur, cur.fetchone())
            conn.commit()
        return _serialize_row(row)

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM strategy_mgmt.strategy_version WHERE id = %s",
                    (version_id,),
                )
                row = cur.fetchone()
                return _serialize_row(_row_to_dict(cur, row)) if row else None

    def freeze_version(self, version_id: str, frozen_by: str) -> dict[str, Any]:
        """Transition lifecycle to FROZEN.  The DB trigger prevents any further mutations."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lifecycle FROM strategy_mgmt.strategy_version WHERE id = %s FOR UPDATE",
                    (version_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"StrategyVersion {version_id} not found")
                lifecycle = row[0]
                if lifecycle == "FROZEN":
                    raise ValueError(f"StrategyVersion {version_id} is already frozen")
                if lifecycle == "DRAFT":
                    raise ValueError(
                        f"StrategyVersion {version_id} is DRAFT; must be IMPLEMENTED before freezing"
                    )
                cur.execute(
                    """UPDATE strategy_mgmt.strategy_version
                       SET lifecycle = 'FROZEN', frozen_at = now(), frozen_by = %s, updated_at = now()
                       WHERE id = %s
                       RETURNING *""",
                    (frozen_by, version_id),
                )
                updated = _row_to_dict(cur, cur.fetchone())
            conn.commit()
        return _serialize_row(updated)

    def set_version_lifecycle(self, version_id: str, lifecycle: str, updated_by: str) -> dict[str, Any]:
        """Set lifecycle (DRAFT→IMPLEMENTED only; use freeze_version for FROZEN)."""
        if lifecycle not in ("DRAFT", "IMPLEMENTED"):
            raise ValueError(f"use freeze_version() for FROZEN; lifecycle must be DRAFT or IMPLEMENTED")
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE strategy_mgmt.strategy_version
                       SET lifecycle = %s, updated_at = now()
                       WHERE id = %s
                       RETURNING *""",
                    (lifecycle, version_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"StrategyVersion {version_id} not found")
                updated = _row_to_dict(cur, row)
            conn.commit()
        return _serialize_row(updated)

    # ── parameter sets ───────────────────────────────────────────────────────

    def create_parameter_set(self, *, parameter_set_id: str, strategy_version_id: str,
                              schema_id: str, values: dict[str, Any],
                              provenance: dict[str, Any], created_by: str) -> dict[str, Any]:
        fp = _fingerprint({
            "parameter_set_id": parameter_set_id,
            "strategy_version_id": strategy_version_id,
            "schema_id": schema_id,
            "values": values,
            "provenance": provenance,
        })
        row_id = str(uuid.uuid4())
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO strategy_mgmt.parameter_set
                        (id, parameter_set_id, strategy_version_id, schema_id,
                         values, fingerprint, provenance, created_by)
                       VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s)
                       RETURNING *""",
                    (row_id, parameter_set_id, strategy_version_id, schema_id,
                     json.dumps(values), fp, json.dumps(provenance), created_by),
                )
                row = _row_to_dict(cur, cur.fetchone())
            conn.commit()
        return _serialize_row(row)

    def get_parameter_set(self, parameter_set_id: str) -> dict[str, Any] | None:
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM strategy_mgmt.parameter_set WHERE id = %s",
                    (parameter_set_id,),
                )
                row = cur.fetchone()
                return _serialize_row(_row_to_dict(cur, row)) if row else None

    def get_parameter_schema(self, schema_id: str) -> dict[str, Any] | None:
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM strategy_mgmt.parameter_schema WHERE schema_id = %s",
                    (schema_id,),
                )
                row = cur.fetchone()
                return _serialize_row(_row_to_dict(cur, row)) if row else None

    def freeze_parameter_set(self, ps_id: str, frozen_by: str) -> dict[str, Any]:
        """Mark a parameter set frozen; the DB trigger then blocks value changes."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE strategy_mgmt.parameter_set
                       SET frozen = true, frozen_at = now(), frozen_by = %s, updated_at = now()
                       WHERE id = %s AND frozen = false
                       RETURNING *""",
                    (frozen_by, ps_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"ParameterSet {ps_id} not found or already frozen")
                updated = _row_to_dict(cur, row)
            conn.commit()
        return _serialize_row(updated)

    # ── backtest runs ────────────────────────────────────────────────────────

    def create_backtest_run(self, *, strategy_version_id: str, parameter_set_id: str,
                             backtest_purpose: str, instruments: list[str],
                             timeframes: list[str], date_range_start: datetime | None,
                             date_range_end: datetime | None, cost_model_fingerprint: str | None,
                             created_by: str) -> dict[str, Any]:
        if backtest_purpose not in ("DISCOVERY", "VALIDATION"):
            raise ValueError(f"backtest_purpose must be DISCOVERY or VALIDATION, got {backtest_purpose!r}")
        row_id = str(uuid.uuid4())
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO strategy_mgmt.backtest_run
                        (id, strategy_version_id, parameter_set_id, backtest_purpose,
                         status, instruments, timeframes, date_range_start, date_range_end,
                         cost_model_fingerprint, created_by)
                       VALUES (%s,%s,%s,%s,'QUEUED',%s,%s,%s,%s,%s,%s)
                       RETURNING *""",
                    (row_id, strategy_version_id, parameter_set_id, backtest_purpose,
                     instruments, timeframes, date_range_start, date_range_end,
                     cost_model_fingerprint, created_by),
                )
                row = _row_to_dict(cur, cur.fetchone())
            conn.commit()
        return _serialize_row(row)

    def get_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM strategy_mgmt.backtest_run WHERE id = %s",
                    (run_id,),
                )
                row = cur.fetchone()
                return _serialize_row(_row_to_dict(cur, row)) if row else None

    def update_backtest_status(self, run_id: str, status: str, *,
                                result_fingerprint: str | None = None,
                                metrics: dict[str, Any] | None = None,
                                error_detail: str | None = None,
                                engine_version: str | None = None,
                                evaluator_fingerprint: str | None = None,
                                dataset_fingerprint: str | None = None) -> dict[str, Any]:
        if status not in ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"):
            raise ValueError(f"invalid backtest status: {status!r}")
        started_at_clause = ", started_at = now()" if status == "RUNNING" else ""
        completed_at_clause = ", completed_at = now()" if status in ("COMPLETED", "FAILED", "CANCELLED") else ""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE strategy_mgmt.backtest_run
                        SET status = %s,
                            result_fingerprint = COALESCE(%s, result_fingerprint),
                            metrics = COALESCE(%s::jsonb, metrics),
                            error_detail = COALESCE(%s, error_detail),
                            engine_version = COALESCE(%s, engine_version),
                            evaluator_fingerprint = COALESCE(%s, evaluator_fingerprint),
                            dataset_fingerprint = COALESCE(%s, dataset_fingerprint)
                            {started_at_clause}
                            {completed_at_clause}
                        WHERE id = %s
                        RETURNING *""",
                    (status,
                     result_fingerprint,
                     json.dumps(metrics) if metrics is not None else None,
                     error_detail, engine_version, evaluator_fingerprint, dataset_fingerprint,
                     run_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"BacktestRun {run_id} not found")
                updated = _row_to_dict(cur, row)
            conn.commit()
        return _serialize_row(updated)

    # ── strategy instances ───────────────────────────────────────────────────

    def create_instance(self, *, strategy_version_id: str, parameter_set_id: str,
                         display_name: str, created_by: str) -> dict[str, Any]:
        """Create a new instance.

        Always starts online=false, execution_eligible=false, execution_mode='OFF'.
        Migration 044 adds execution_mode/execution_mode_revision columns; the
        INSERT explicitly sets them so the repo works after either migration order.
        """
        row_id = str(uuid.uuid4())
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO strategy_mgmt.strategy_instance_v2
                        (id, strategy_version_id, parameter_set_id, display_name,
                         online, execution_eligible, execution_mode,
                         execution_mode_revision, created_by)
                       VALUES (%s,%s,%s,%s, false, false, 'OFF', 0, %s)
                       RETURNING *""",
                    (row_id, strategy_version_id, parameter_set_id, display_name, created_by),
                )
                row = _row_to_dict(cur, cur.fetchone())
            conn.commit()
        return _serialize_row(row)

    def set_instance_execution_mode(
        self,
        instance_id: str,
        mode: str,
        updated_by: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Change execution_mode; increments execution_mode_revision.

        LIVE transitions must be gated by a preflight check in the caller — this
        method persists whatever mode is passed after the caller has verified it
        is safe.  It does NOT call the preflight itself.

        Raises:
          ValueError — invalid mode
          KeyError — instance not found
          ExecutionModeRevisionConflict — expected_revision mismatch
        """
        if mode not in VALID_EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of {sorted(VALID_EXECUTION_MODES)}, got {mode!r}")
        with self._conn() as conn:
            with conn.cursor() as cur:
                if expected_revision is not None:
                    cur.execute(
                        "SELECT execution_mode_revision FROM strategy_mgmt.strategy_instance_v2"
                        " WHERE id = %s FOR UPDATE",
                        (instance_id,),
                    )
                    rev_row = cur.fetchone()
                    if rev_row is None:
                        raise KeyError(f"StrategyInstance {instance_id} not found")
                    if rev_row[0] != expected_revision:
                        raise ExecutionModeRevisionConflict(
                            f"expected execution_mode_revision={expected_revision}, "
                            f"got {rev_row[0]}"
                        )
                cur.execute(
                    """UPDATE strategy_mgmt.strategy_instance_v2
                       SET execution_mode = %s,
                           execution_mode_revision = execution_mode_revision + 1,
                           updated_at = now()
                       WHERE id = %s
                       RETURNING *""",
                    (mode, instance_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"StrategyInstance {instance_id} not found")
                updated = _row_to_dict(cur, row)
            conn.commit()
        return _serialize_row(updated)

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM strategy_mgmt.strategy_instance_v2 WHERE id = %s OR attributes->>'instance_id' = %s",
                    (instance_id, instance_id),
                )
                row = cur.fetchone()
                if row is None:
                    return None
                result = _row_to_dict(cur, row)
                # Enrich the base row for the UI without making the instance
                # identity depend on a denormalized legacy table.
                cur.execute(
                    """SELECT v.*, d.family_key, d.name AS strategy_name
                         FROM strategy_mgmt.strategy_version v
                         JOIN strategy_mgmt.strategy_definition d ON d.id = v.definition_id
                        WHERE v.id = %s""",
                    (result["strategy_version_id"],),
                )
                version = cur.fetchone()
                if version:
                    version_row = _row_to_dict(cur, version)
                    result.update({"version_label": version_row.get("version_label"),
                                   "evaluator_key": version_row.get("evaluator_key"),
                                   "schema_id": version_row.get("schema_id"),
                                   "family_key": version_row.get("family_key"),
                                   "strategy_name": version_row.get("strategy_name")})
                cur.execute("SELECT * FROM strategy_mgmt.parameter_set WHERE id = %s",
                            (result["parameter_set_id"],))
                parameter_set = cur.fetchone()
                if parameter_set:
                    parameter_row = _row_to_dict(cur, parameter_set)
                    result.update({"parameter_set_id": parameter_row.get("parameter_set_id"),
                                   "parameter_fingerprint": parameter_row.get("fingerprint"),
                                   "parameter_values": parameter_row.get("values"),
                                   "schema_id": parameter_row.get("schema_id", result.get("schema_id"))})
                    cur.execute("SELECT fields FROM strategy_mgmt.parameter_schema WHERE schema_id = %s",
                                (parameter_row.get("schema_id"),))
                    schema = cur.fetchone()
                    if schema:
                        result["parameter_schema"] = schema[0]
                return _serialize_row(result)

    def list_instances(self) -> list[dict[str, Any]]:
        """List registered instances for UI identity resolution.

        The UUID is the write identity.  Legacy ``attributes.instance_id`` values
        are returned as metadata only so clients can migrate away from slugs.
        """
        with self._conn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM strategy_mgmt.strategy_instance_v2 ORDER BY created_at DESC")
                return [_serialize_row(_row_to_dict(cur, row)) for row in cur.fetchall()]

    def patch_instance_online(self, instance_id: str, online: bool, updated_by: str) -> dict[str, Any]:
        """Toggle online flag only.  execution_eligible is never changed here."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                # Read current execution_eligible to assert it won't change.
                cur.execute(
                    """SELECT id, execution_eligible
                         FROM strategy_mgmt.strategy_instance_v2
                        WHERE id::text = %s OR attributes->>'instance_id' = %s
                        FOR UPDATE""",
                    (instance_id, instance_id),
                )
                before = cur.fetchone()
                if before is None:
                    raise KeyError(f"StrategyInstance {instance_id} not found")
                resolved_id, execution_eligible = before
                # Update only online; the DB trigger will reject any execution_eligible change.
                cur.execute(
                    """UPDATE strategy_mgmt.strategy_instance_v2
                       SET online = %s, updated_at = now()
                       WHERE id = %s
                       RETURNING *""",
                    (online, resolved_id),
                )
                row = _row_to_dict(cur, cur.fetchone())
                # Assert invariant: execution_eligible must be unchanged.
                assert row["execution_eligible"] == execution_eligible, \
                    "INVARIANT VIOLATION: execution_eligible changed during online toggle"
            conn.commit()
        return _serialize_row(row)

    def patch_instance_instruments(self, instance_id: str,
                                    instruments: list[dict[str, Any]],
                                    updated_by: str) -> dict[str, Any]:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE strategy_mgmt.strategy_instance_v2
                       SET instruments = %s::jsonb, updated_at = now()
                       WHERE id = %s
                       RETURNING *""",
                    (json.dumps(instruments), instance_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"StrategyInstance {instance_id} not found")
                updated = _row_to_dict(cur, row)
            conn.commit()
        return _serialize_row(updated)

    def patch_instance_parameter_set(self, instance_id: str, parameter_set_id: str,
                                     updated_by: str) -> dict[str, Any]:
        """Bind a new immutable ParameterSet only while the instance is offline."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT online, execution_eligible FROM strategy_mgmt.strategy_instance_v2 "
                    "WHERE id::text = %s OR attributes->>'instance_id' = %s FOR UPDATE",
                    (instance_id, instance_id),
                )
                current = cur.fetchone()
                if current is None:
                    raise KeyError(f"StrategyInstance {instance_id} not found")
                if current[0] or current[1]:
                    raise ValueError("parameter_set can only change while instance is offline and execution-ineligible")
                cur.execute(
                    """UPDATE strategy_mgmt.strategy_instance_v2
                       SET parameter_set_id = %s::uuid, updated_at = now()
                       WHERE id::text = %s OR attributes->>'instance_id' = %s
                       RETURNING *""",
                    (parameter_set_id, instance_id, instance_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise KeyError(f"StrategyInstance {instance_id} not found")
                updated = _row_to_dict(cur, row)
            conn.commit()
        return _serialize_row(updated)


# ─── async backtest job runner ───────────────────────────────────────────────

class BacktestJobRunner:
    """Minimal thread-pool-based background backtest runner.

    For each QUEUED BacktestRun this submits compute to a thread pool.
    The runner uses the existing BacktestEngine (strategy_backtest.engine) and the
    StrategyEvaluatorRegistry — the same evaluator contract used for live feeds.

    BROKER_WRITES = 0.  This runner never touches execution state.
    """

    def __init__(self, repository: StrategyMgmtRepository,
                 registry: Any | None = None,
                 max_workers: int = 4):
        self._repo = repository
        self._registry = registry
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="backtest-worker"
        )
        self._submitted: set[str] = set()
        self._lock = threading.Lock()

    def submit(self, run_id: str, compute_fn: Callable[[], dict[str, Any]]) -> None:
        """Submit a backtest computation for run_id.  compute_fn returns metrics dict."""
        with self._lock:
            if run_id in self._submitted:
                return
            self._submitted.add(run_id)
        self._pool.submit(self._run, run_id, compute_fn)

    def _run(self, run_id: str, compute_fn: Callable[[], dict[str, Any]]) -> None:
        try:
            self._repo.update_backtest_status(run_id, "RUNNING")
            result = compute_fn()
            self._repo.update_backtest_status(
                run_id, "COMPLETED",
                result_fingerprint=result.get("result_fingerprint"),
                metrics=result.get("metrics"),
                engine_version=result.get("engine_version"),
                evaluator_fingerprint=result.get("evaluator_fingerprint"),
                dataset_fingerprint=result.get("dataset_fingerprint"),
            )
        except Exception as exc:  # noqa: BLE001
            try:
                self._repo.update_backtest_status(run_id, "FAILED", error_detail=str(exc))
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait)


# ─── API handler ─────────────────────────────────────────────────────────────

class StrategyMgmtApi:
    """HTTP-style handler that maps (method, path, body) → (status_code, response_dict).

    Wire this into PlatformControlApi.execute() before the catch-all 404.

    live_preflight_fn:
        Callable[[], dict] that returns {"passed": bool, "checks": [...], "reasons": [...]}.
        Called before any SHADOW→LIVE transition.  If None, LIVE transitions are refused
        (fail-closed).  Pass ExecutionAuthorityApi._preflight from PlatformControlApi so
        the existing execution authority preflight is reused without duplication.
    """

    def __init__(self, repository: StrategyMgmtRepository | None = None,
                 job_runner: BacktestJobRunner | None = None,
                 live_preflight_fn: Any | None = None):
        self._repo = repository or StrategyMgmtRepository()
        self._runner = job_runner or BacktestJobRunner(self._repo)
        self._live_preflight_fn = live_preflight_fn

    def registry_rows(self) -> list[dict[str, Any]]:
        """Read the unified strategy registry for the compatibility catalog endpoint."""
        return self._repo.list_registry()

    # ── routing ──────────────────────────────────────────────────────────────

    def handle(self, method: str, path: str, body: bytes | None) -> tuple[int, dict[str, Any]] | None:
        """Return (status, body) or None if path not owned by this handler."""
        p = path.rstrip("/")

        # Strategy definitions
        if p == "/api/v1/strategy-definitions":
            if method == "GET":
                return self._list_definitions()
            if method == "POST":
                return self._create_definition(body)
            return 405, self._err("METHOD_NOT_ALLOWED")

        if p.startswith("/api/v1/strategy-definitions/"):
            def_id = p[len("/api/v1/strategy-definitions/"):]
            if "/" not in def_id and def_id:
                if method == "GET":
                    return self._get_definition(def_id)
                return 405, self._err("METHOD_NOT_ALLOWED")

        # Strategy versions
        if p == "/api/v1/strategy-versions":
            if method == "POST":
                return self._create_version(body)
            return 405, self._err("METHOD_NOT_ALLOWED")

        if p.startswith("/api/v1/strategy-versions/"):
            rest = p[len("/api/v1/strategy-versions/"):]
            parts = rest.split("/")
            ver_id = parts[0]

            if len(parts) == 1 and ver_id:
                if method == "GET":
                    return self._get_version(ver_id)
                return 405, self._err("METHOD_NOT_ALLOWED")

            if len(parts) == 2 and parts[1] == "freeze" and ver_id:
                if method == "POST":
                    return self._freeze_version(ver_id, body)
                return 405, self._err("METHOD_NOT_ALLOWED")

            if len(parts) == 2 and parts[1] == "backtests" and ver_id:
                if method == "POST":
                    return self._create_backtest(ver_id, body)
                return 405, self._err("METHOD_NOT_ALLOWED")

        # Parameter sets
        if p == "/api/v1/parameter-sets":
            if method == "POST":
                return self._create_parameter_set(body)
            return 405, self._err("METHOD_NOT_ALLOWED")

        if p.startswith("/api/v1/parameter-sets/"):
            ps_id = p[len("/api/v1/parameter-sets/"):]
            if "/" not in ps_id and ps_id:
                if method == "GET":
                    return self._get_parameter_set(ps_id)
                return 405, self._err("METHOD_NOT_ALLOWED")

        if p.startswith("/api/v1/parameter-schemas/"):
            schema_id = p[len("/api/v1/parameter-schemas/"):]
            if "/" not in schema_id and schema_id:
                if method == "GET":
                    return self._get_parameter_schema(schema_id)
                return 405, self._err("METHOD_NOT_ALLOWED")

        # Backtests (status polling)
        if p.startswith("/api/v1/backtests/"):
            run_id = p[len("/api/v1/backtests/"):]
            if "/" not in run_id and run_id:
                if method == "GET":
                    return self._get_backtest(run_id)
                return 405, self._err("METHOD_NOT_ALLOWED")

        # Strategy instances (new pipeline)
        if p == "/api/v1/strategy-instances":
            if method == "GET":
                return self._list_instances()
            if method == "POST":
                return self._create_instance(body)
            return 405, self._err("METHOD_NOT_ALLOWED")

        if p.startswith("/api/v1/strategy-instances/"):
            rest = p[len("/api/v1/strategy-instances/"):]
            parts = rest.split("/")
            inst_id = parts[0]

            if len(parts) == 1 and inst_id:
                if method == "GET":
                    return self._get_instance(inst_id)
                if method == "PATCH":
                    return self._patch_instance(inst_id, body)
                return 405, self._err("METHOD_NOT_ALLOWED")

            if len(parts) == 2 and parts[1] == "execution-mode" and inst_id:
                if method == "GET":
                    return self._get_instance_execution_mode(inst_id)
                if method == "POST":
                    return self._set_instance_execution_mode(inst_id, body)
                return 405, self._err("METHOD_NOT_ALLOWED")

        # Not owned by this handler
        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _ok(data: Any) -> tuple[int, dict[str, Any]]:
        return 200, {"status": "ok", "data": data}

    @staticmethod
    def _created(data: Any) -> tuple[int, dict[str, Any]]:
        return 201, {"status": "created", "data": data}

    @staticmethod
    def _err(code: str, message: str = "") -> dict[str, Any]:
        return {"status": "error", "error": code, "message": message}

    @staticmethod
    def _parse(body: bytes | None) -> dict[str, Any]:
        try:
            payload = json.loads((body or b"{}").decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc

    def _wrap(self, fn: Callable[[], tuple[int, dict[str, Any]]]) -> tuple[int, dict[str, Any]]:
        try:
            return fn()
        except KeyError as exc:
            return 404, self._err("RESOURCE_NOT_FOUND", str(exc))
        except ValueError as exc:
            return 400, self._err("INVALID_REQUEST", str(exc))
        except Exception as exc:
            return 503, self._err("SOURCE_UNAVAILABLE", str(exc))

    # ── strategy definitions ──────────────────────────────────────────────────

    def _list_definitions(self) -> tuple[int, dict[str, Any]]:
        return self._wrap(lambda: self._ok(self._repo.list_definitions()))

    def _create_definition(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        def _do():
            p = self._parse(body)
            name = str(p.get("name") or "").strip()
            family_key = str(p.get("familyKey") or p.get("family_key") or "").strip()
            if not name:
                raise ValueError("name is required")
            if not family_key:
                raise ValueError("familyKey is required")
            row = self._repo.create_definition(
                name=name,
                family_key=family_key,
                description=p.get("description"),
                provenance_notes=p.get("provenanceNotes") or p.get("provenance_notes"),
                created_by=str(p.get("createdBy") or p.get("created_by") or "api"),
            )
            return 201, {"status": "created", "data": row}
        return self._wrap(_do)

    def _get_definition(self, def_id: str) -> tuple[int, dict[str, Any]]:
        def _do():
            row = self._repo.get_definition(def_id)
            if row is None:
                raise KeyError(f"StrategyDefinition {def_id} not found")
            return self._ok(row)
        return self._wrap(_do)

    # ── strategy versions ─────────────────────────────────────────────────────

    def _create_version(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        def _do():
            p = self._parse(body)
            def_id = str(p.get("definitionId") or p.get("definition_id") or "").strip()
            version_label = str(p.get("versionLabel") or p.get("version_label") or "").strip()
            evaluator_key = str(p.get("evaluatorKey") or p.get("evaluator_key") or "").strip()
            if not def_id:
                raise ValueError("definitionId is required")
            if not version_label:
                raise ValueError("versionLabel is required")
            if not evaluator_key:
                raise ValueError("evaluatorKey is required")
            row = self._repo.create_version(
                definition_id=def_id,
                version_label=version_label,
                evaluator_key=evaluator_key,
                schema_id=p.get("schemaId") or p.get("schema_id"),
                release_notes=p.get("releaseNotes") or p.get("release_notes"),
                created_by=str(p.get("createdBy") or "api"),
            )
            return 201, {"status": "created", "data": row}
        return self._wrap(_do)

    def _get_version(self, ver_id: str) -> tuple[int, dict[str, Any]]:
        def _do():
            row = self._repo.get_version(ver_id)
            if row is None:
                raise KeyError(f"StrategyVersion {ver_id} not found")
            return self._ok(row)
        return self._wrap(_do)

    def _freeze_version(self, ver_id: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        def _do():
            p = self._parse(body)
            frozen_by = str(p.get("frozenBy") or p.get("frozen_by") or "api")
            row = self._repo.freeze_version(ver_id, frozen_by)
            return self._ok(row)
        return self._wrap(_do)

    # ── parameter sets ────────────────────────────────────────────────────────

    def _create_parameter_set(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        def _do():
            p = self._parse(body)
            ps_id = str(p.get("parameterSetId") or p.get("parameter_set_id") or "").strip()
            sv_id = str(p.get("strategyVersionId") or p.get("strategy_version_id") or "").strip()
            schema_id = str(p.get("schemaId") or p.get("schema_id") or "").strip()
            values = p.get("values")
            if not ps_id:
                raise ValueError("parameterSetId is required")
            if not sv_id:
                raise ValueError("strategyVersionId is required")
            if not schema_id:
                raise ValueError("schemaId is required")
            if not isinstance(values, dict):
                raise ValueError("values must be an object")
            row = self._repo.create_parameter_set(
                parameter_set_id=ps_id,
                strategy_version_id=sv_id,
                schema_id=schema_id,
                values=values,
                provenance=p.get("provenance") or {},
                created_by=str(p.get("createdBy") or "api"),
            )
            return 201, {"status": "created", "data": row}
        return self._wrap(_do)

    def _get_parameter_set(self, ps_id: str) -> tuple[int, dict[str, Any]]:
        def _do():
            row = self._repo.get_parameter_set(ps_id)
            if row is None:
                raise KeyError(f"ParameterSet {ps_id} not found")
            return self._ok(row)
        return self._wrap(_do)

    def _get_parameter_schema(self, schema_id: str) -> tuple[int, dict[str, Any]]:
        def _do():
            row = self._repo.get_parameter_schema(schema_id)
            if row is None:
                raise KeyError(f"ParameterSchema {schema_id} not found")
            return self._ok(row)
        return self._wrap(_do)

    # ── backtests ─────────────────────────────────────────────────────────────

    def _create_backtest(self, ver_id: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        def _do():
            p = self._parse(body)
            ps_id = str(p.get("parameterSetId") or p.get("parameter_set_id") or "").strip()
            purpose = str(p.get("backtestPurpose") or p.get("backtest_purpose") or "DISCOVERY").upper()
            if not ps_id:
                raise ValueError("parameterSetId is required")
            instruments = p.get("instruments") or []
            timeframes = p.get("timeframes") or []
            cost_model_fp = p.get("costModelFingerprint") or p.get("cost_model_fingerprint")

            # Parse optional date range
            dr_start = dr_end = None
            if p.get("dateRangeStart"):
                dr_start = datetime.fromisoformat(str(p["dateRangeStart"]))
            if p.get("dateRangeEnd"):
                dr_end = datetime.fromisoformat(str(p["dateRangeEnd"]))

            row = self._repo.create_backtest_run(
                strategy_version_id=ver_id,
                parameter_set_id=ps_id,
                backtest_purpose=purpose,
                instruments=instruments,
                timeframes=timeframes,
                date_range_start=dr_start,
                date_range_end=dr_end,
                cost_model_fingerprint=cost_model_fp,
                created_by=str(p.get("createdBy") or "api"),
            )
            run_id = str(row["id"])

            # Dispatch async if a compute_fn factory is registered
            compute_fn = self._build_compute_fn(run_id, row, p)
            if compute_fn is not None:
                self._runner.submit(run_id, compute_fn)

            return 201, {"status": "created", "data": row}
        return self._wrap(_do)

    def _get_backtest(self, run_id: str) -> tuple[int, dict[str, Any]]:
        def _do():
            row = self._repo.get_backtest_run(run_id)
            if row is None:
                raise KeyError(f"BacktestRun {run_id} not found")
            return self._ok(row)
        return self._wrap(_do)

    def _build_compute_fn(self, run_id: str, run_row: dict[str, Any],
                           request_payload: dict[str, Any]) -> Callable[[], dict[str, Any]] | None:
        """Build a compute closure for a backtest if enough information is available.

        The compute function uses the existing BacktestEngine (strategy_backtest.engine) with
        the same evaluator contract used for live market feed processing.  It never touches
        execution state.

        Returns None if a feed/parameter-set is not resolvable from the request (caller must
        submit the job externally or the status will remain QUEUED until polled).
        """
        feed_events = request_payload.get("_feed_events")  # test injection point
        parameter_values = request_payload.get("_parameter_values")  # test injection point
        evaluator_key = request_payload.get("_evaluator_key")  # test injection point
        schema_fields = request_payload.get("_schema_fields")  # test injection point
        # _strategy_version_id: canonical "STRATEGY_ID@VERSION" (e.g. "KOJO_STRUCTURE_RECLAIM_V1@V1").
        # Required when the evaluator checks strategy_version.strategy_version_id identity.
        # Falls back to the DB UUID@V1 for evaluators that do not perform this check.
        canonical_svid_override = request_payload.get("_strategy_version_id")  # test injection point

        if feed_events is None or parameter_values is None or evaluator_key is None:
            return None  # no embedded compute data; caller polls for QUEUED → dispatched externally

        def compute() -> dict[str, Any]:
            from strategy_backtest import (BacktestEngine, CostModel, HistoricalMarketFeed,
                                           MarketEvent, ParameterSchema, ParameterSet,
                                           StrategyEvaluatorRegistry, StrategyVersion)
            from strategy_backtest.registry import register_builtin_evaluators

            registry = StrategyEvaluatorRegistry()
            register_builtin_evaluators(registry)

            schema = ParameterSchema(
                run_row.get("schema_id") or "inline",
                schema_fields or {},
            )
            ps_id = str(run_row["parameter_set_id"])

            # Build StrategyVersion with the canonical identity.
            # When _strategy_version_id is supplied (e.g. "KOJO_STRUCTURE_RECLAIM_V1@V1"),
            # parse it into strategy_id + version_label so evaluator identity checks pass.
            # Without it, fall back to the DB row UUID which works for evaluators without strict
            # identity checks.
            if canonical_svid_override:
                parts = str(canonical_svid_override).rsplit("@", 1)
                canonical_sid = parts[0]
                version_label = parts[1] if len(parts) == 2 else "V1"
            else:
                canonical_sid = str(run_row["strategy_version_id"])
                version_label = "V1"
            strategy_version = StrategyVersion(canonical_sid, version_label, evaluator_key, schema)
            # ParameterSet.strategy_version_id must equal strategy_version.strategy_version_id.
            parameter_set = ParameterSet(ps_id, strategy_version.strategy_version_id,
                                         schema.schema_id, parameter_values)

            events = tuple(
                MarketEvent(**e) if isinstance(e, dict) else e for e in feed_events
            )
            dataset_fp = _fingerprint({"events": [
                {"i": e.canonical_instrument, "t": e.timeframe,
                 "o": e.open_timestamp, "c": e.close_timestamp}
                for e in events
            ]})
            feed = HistoricalMarketFeed(events, dataset_fp)
            cost_model = CostModel(
                run_row.get("cost_model_fingerprint") or "zero-cost",
                spread_price=float(request_payload.get("spreadPrice") or 0.0),
                commission_r=float(request_payload.get("commissionR") or 0.0),
            )
            engine = BacktestEngine(registry)
            result = engine.run(strategy_version, parameter_set, feed, cost_model, run_id=run_id)
            return {
                "result_fingerprint": result.result_fingerprint,
                "metrics": result.metrics,
                "engine_version": result.run.engine_version,
                "evaluator_fingerprint": result.run.evaluator_fingerprint,
                "dataset_fingerprint": dataset_fp,
            }

        return compute

    # ── strategy instances ────────────────────────────────────────────────────

    def _create_instance(self, body: bytes | None) -> tuple[int, dict[str, Any]]:
        def _do():
            p = self._parse(body)
            sv_id = str(p.get("strategyVersionId") or p.get("strategy_version_id") or "").strip()
            ps_id = str(p.get("parameterSetId") or p.get("parameter_set_id") or "").strip()
            display_name = str(p.get("displayName") or p.get("display_name") or "").strip()
            if not sv_id:
                raise ValueError("strategyVersionId is required")
            if not ps_id:
                raise ValueError("parameterSetId is required")
            if not display_name:
                raise ValueError("displayName is required")
            row = self._repo.create_instance(
                strategy_version_id=sv_id,
                parameter_set_id=ps_id,
                display_name=display_name,
                created_by=str(p.get("createdBy") or "api"),
            )
            # Invariant assertions
            assert row["online"] is False, "INVARIANT: new instance must start OFFLINE"
            assert row["execution_eligible"] is False, "INVARIANT: new instance must start execution_ineligible"
            return 201, {"status": "created", "data": row}
        return self._wrap(_do)

    def _get_instance(self, inst_id: str) -> tuple[int, dict[str, Any]]:
        def _do():
            row = self._repo.get_instance(inst_id)
            if row is None:
                raise KeyError(f"StrategyInstance {inst_id} not found")
            return self._ok(row)
        return self._wrap(_do)

    def _list_instances(self) -> tuple[int, dict[str, Any]]:
        return self._wrap(lambda: self._ok(self._repo.list_instances()))

    def _patch_instance(self, inst_id: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        """PATCH can only change `online` or `instruments`.  execution_eligible changes are refused.
        Use POST /execution-mode for execution mode transitions."""
        def _do():
            p = self._parse(body)

            # Hard block: reject any attempt to change execution_eligible through this path.
            if "executionEligible" in p or "execution_eligible" in p:
                raise ValueError(
                    "execution_eligible cannot be changed through this endpoint; "
                    "it is managed by the execution authority system"
                )
            if "executionMode" in p or "execution_mode" in p:
                raise ValueError(
                    "execution_mode cannot be changed via PATCH; "
                    "use POST /api/v1/strategy-instances/{id}/execution-mode"
                )

            if "parameterSetId" in p or "parameter_set_id" in p:
                ps_id = str(p.get("parameterSetId") or p.get("parameter_set_id") or "").strip()
                if not ps_id:
                    raise ValueError("parameterSetId is required")
                row = self._repo.patch_instance_parameter_set(
                    inst_id, ps_id, str(p.get("updatedBy") or "api"))
                return self._ok(row)

            if "online" in p:
                online = bool(p["online"])
                updated_by = str(p.get("updatedBy") or "api")
                row = self._repo.patch_instance_online(inst_id, online, updated_by)
                return self._ok(row)

            if "instruments" in p:
                instruments = p["instruments"]
                if not isinstance(instruments, list):
                    raise ValueError("instruments must be an array")
                updated_by = str(p.get("updatedBy") or "api")
                row = self._repo.patch_instance_instruments(inst_id, instruments, updated_by)
                return self._ok(row)

            raise ValueError("PATCH body must include 'online' or 'instruments'")
        return self._wrap(_do)

    def _get_instance_execution_mode(self, inst_id: str) -> tuple[int, dict[str, Any]]:
        def _do():
            row = self._repo.get_instance(inst_id)
            if row is None:
                raise KeyError(f"StrategyInstance {inst_id} not found")
            return self._ok({
                "instance_id": inst_id,
                "execution_mode": row.get("execution_mode", "OFF"),
                "execution_mode_revision": row.get("execution_mode_revision", 0),
                "online": row.get("online", False),
                "execution_eligible": row.get("execution_eligible", False),
            })
        return self._wrap(_do)

    def _set_instance_execution_mode(self, inst_id: str, body: bytes | None) -> tuple[int, dict[str, Any]]:
        """POST /api/v1/strategy-instances/{id}/execution-mode

        Body: {"mode": "SHADOW", "expectedRevision": 0, "updatedBy": "operator"}

        Allowed transitions (all others refused):
          OFF    → SHADOW   (no preflight required)
          SHADOW → OFF      (no preflight required)
          SHADOW → LIVE     (requires system-level execution authority preflight)
          LIVE   → SHADOW   (no preflight required)
          LIVE   → OFF      (no preflight required)

        LIVE transition requires passing the injected live_preflight_fn.
        If no preflight_fn is wired, LIVE transitions are refused (fail-closed).

        OLD SHADOW signals are never retroactively executable.
        execution_mode_at_decision in signal provenance captures the mode at emit time.
        """
        def _do():
            p = self._parse(body)
            mode = str(p.get("mode") or p.get("executionMode") or "").strip().upper()
            if not mode:
                raise ValueError("'mode' is required (OFF | SHADOW | LIVE)")
            if mode not in VALID_EXECUTION_MODES:
                raise ValueError(f"mode must be one of {sorted(VALID_EXECUTION_MODES)}")

            expected_revision = p.get("expectedRevision")
            if expected_revision is not None and not isinstance(expected_revision, int):
                raise ValueError("expectedRevision must be an integer")
            updated_by = str(p.get("updatedBy") or "api")

            # Read current state for transition validation
            current = self._repo.get_instance(inst_id)
            if current is None:
                raise KeyError(f"StrategyInstance {inst_id} not found")
            current_mode = str(current.get("execution_mode") or "OFF")
            online = bool(current.get("online", False))

            # Validate transition
            _ALLOWED_TRANSITIONS = {
                ("OFF",    "SHADOW"),
                ("SHADOW", "OFF"),
                ("SHADOW", "LIVE"),
                ("LIVE",   "SHADOW"),
                ("LIVE",   "OFF"),
            }
            if current_mode == mode:
                return self._ok({
                    **{k: current[k] for k in ("execution_mode", "execution_mode_revision", "online") if k in current},
                    "message": f"already in {mode}",
                })
            if (current_mode, mode) not in _ALLOWED_TRANSITIONS:
                raise ValueError(
                    f"transition {current_mode}→{mode} is not supported; "
                    f"allowed: {sorted(_ALLOWED_TRANSITIONS)}"
                )

            # SHADOW→LIVE: system-level preflight required
            preflight_result: dict[str, Any] | None = None
            if mode == "LIVE":
                # Instance-level checks
                instance_checks = [
                    {"name": "instance_online", "passed": online,
                     "detail": "instance must be ONLINE to activate LIVE" if not online else None},
                ]
                # System-level preflight (reuses existing execution authority checks)
                if self._live_preflight_fn is None:
                    instance_checks.append({
                        "name": "system_preflight_available",
                        "passed": False,
                        "detail": "no system preflight function is wired; LIVE transition is blocked",
                    })
                    preflight_result = {
                        "passed": False,
                        "checks": instance_checks,
                        "reasons": [c["detail"] for c in instance_checks if not c["passed"]],
                    }
                else:
                    try:
                        sys_preflight = self._live_preflight_fn()
                        all_checks = instance_checks + sys_preflight.get("checks", [])
                        all_reasons = (
                            [c["detail"] for c in instance_checks if not c["passed"]]
                            + sys_preflight.get("reasons", [])
                        )
                        preflight_result = {
                            "passed": not all_reasons,
                            "checks": all_checks,
                            "reasons": all_reasons,
                        }
                    except Exception as exc:  # noqa: BLE001
                        preflight_result = {
                            "passed": False,
                            "checks": instance_checks,
                            "reasons": [f"system preflight failed: {exc}"],
                        }
                if not preflight_result["passed"]:
                    return 409, {
                        "status": "error",
                        "error": "PREFLIGHT_FAILED",
                        "message": "LIVE transition refused; execution mode remains unchanged",
                        "preflight": preflight_result,
                    }

            try:
                updated = self._repo.set_instance_execution_mode(
                    inst_id, mode, updated_by, expected_revision=expected_revision
                )
            except ExecutionModeRevisionConflict as exc:
                return 409, self._err("REVISION_CONFLICT", str(exc))

            result = self._ok(updated)
            if preflight_result is not None:
                result[1]["preflight"] = preflight_result
            return result
        return self._wrap(_do)
