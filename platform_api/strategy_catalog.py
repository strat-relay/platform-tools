"""Strategy catalog and the complete strategy page model, from PostgreSQL only.

Strategy definitions come from platform.strategy_definition (migration 028), not platform.json.
Every statistic is computed server-side over the strategy's full canonical history
(strategy.entry_signals + strategy.entry_signal_outcomes), so a client never derives stats from a
paginated signals list.

Outcome coverage is explicit: signals with no canonical outcome row are counted as
`outcome_untracked`, never folded into open or closed.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable

from postgres.db import connect
from .signals import CanonicalSourceUnavailable, _row_dict

STRATEGY_SCHEMA_VERSION = "028"
INSTANCE_SCHEMA_VERSION = "030"
RECENT_LIMIT = 50

_SUMMARY_SQL = """
SELECT d.strategy_id, d.strategy_version, d.display_name, d.description, d.adapter, d.enabled, d.routes,
       d.trade_management, d.default_instance_id, d.revision, d.created_at, d.updated_at,
       coalesce(sig.signals, 0) AS signals_published, sig.last_signal_at AS last_event_at,
       sig.first_signal_at, coalesce(sig.signals_24h, 0) AS signals_24h,
       coalesce(o.open_n, 0) AS open_observations, coalesce(o.target_hit, 0) AS target_hits,
       coalesce(o.stopped, 0) AS stops, coalesce(o.tracked, 0) AS outcomes_tracked,
       o.realized_r_total, o.realized_r_avg, sig.avg_target_r,
       coalesce(mem.symbols, ARRAY[]::text[]) AS symbols,
       coalesce(sig.signal_instruments, ARRAY[]::text[]) AS signal_instruments
FROM platform.strategy_definition d
LEFT JOIN LATERAL (
    SELECT count(*) AS signals, max(s.signal_emitted_at) AS last_signal_at, min(s.decision_time) AS first_signal_at,
           count(*) FILTER (WHERE s.signal_emitted_at > now() - interval '24 hours') AS signals_24h,
           avg(s.target_r) AS avg_target_r,
           array_agg(DISTINCT s.instrument ORDER BY s.instrument) AS signal_instruments
    FROM strategy.entry_signals s WHERE s.strategy_id = d.strategy_id) sig ON TRUE
LEFT JOIN LATERAL (
    SELECT count(*) AS tracked,
           count(*) FILTER (WHERE o.status = 'OPEN') AS open_n,
           count(*) FILTER (WHERE o.status = 'TARGET_HIT') AS target_hit,
           count(*) FILTER (WHERE o.status = 'STOPPED') AS stopped,
           sum(o.realized_r) FILTER (WHERE o.status <> 'OPEN') AS realized_r_total,
           avg(o.realized_r) FILTER (WHERE o.status <> 'OPEN') AS realized_r_avg
    FROM strategy.entry_signal_outcomes o JOIN strategy.entry_signals s USING (signal_id)
    WHERE s.strategy_id = d.strategy_id) o ON TRUE
LEFT JOIN LATERAL (
    SELECT array_agg(m.canonical_instrument ORDER BY m.canonical_instrument) AS symbols
    FROM strategy.instrument_membership m
    WHERE m.strategy_id = d.strategy_id AND m.strategy_instance_id = d.default_instance_id
      AND m.state = 'ACTIVE') mem ON TRUE
{where}
ORDER BY d.strategy_id"""


# Strategy instances (migration 030) are the operational units. Every per-instance statistic is
# scoped by (strategy_id, strategy_instance_id), never aggregated across a family.
_INSTANCE_SQL = """
SELECT i.instance_id, i.strategy_id, i.display_name, i.enabled, i.attributes, i.revision,
       i.created_at, i.updated_at, i.updated_by, d.enabled AS parent_enabled, d.strategy_version,
       coalesce(sig.signals, 0) AS signals, coalesce(sig.signals_24h, 0) AS signals_24h,
       coalesce(o.tracked, 0) AS outcomes_tracked, coalesce(o.open_n, 0) AS open_n,
       coalesce(o.closed_n, 0) AS closed_n, o.realized_r_total,
       coalesce(mem.active, ARRAY[]::text[]) AS instruments, coalesce(mem.disabled_n, 0) AS instruments_disabled,
       ev.at AS last_event_at, ev.type AS last_event_type, ev.message AS last_event_message
FROM platform.strategy_instance i
JOIN platform.strategy_definition d ON d.strategy_id = i.strategy_id
LEFT JOIN LATERAL (
    SELECT count(*) AS signals,
           count(*) FILTER (WHERE s.signal_emitted_at > now() - interval '24 hours') AS signals_24h
    FROM strategy.entry_signals s
    WHERE s.strategy_id = i.strategy_id AND s.strategy_instance_id = i.instance_id) sig ON TRUE
LEFT JOIN LATERAL (
    SELECT count(*) AS tracked, count(*) FILTER (WHERE o.status = 'OPEN') AS open_n,
           count(*) FILTER (WHERE o.status <> 'OPEN') AS closed_n,
           sum(o.realized_r) FILTER (WHERE o.status <> 'OPEN') AS realized_r_total
    FROM strategy.entry_signal_outcomes o JOIN strategy.entry_signals s USING (signal_id)
    WHERE s.strategy_id = i.strategy_id AND s.strategy_instance_id = i.instance_id) o ON TRUE
LEFT JOIN LATERAL (
    SELECT array_agg(m.canonical_instrument ORDER BY m.canonical_instrument)
               FILTER (WHERE m.state = 'ACTIVE') AS active,
           count(*) FILTER (WHERE m.state <> 'ACTIVE') AS disabled_n
    FROM strategy.instrument_membership m
    WHERE m.strategy_id = i.strategy_id AND m.strategy_instance_id = i.instance_id) mem ON TRUE
LEFT JOIN LATERAL (
    SELECT * FROM (
        SELECT s.signal_emitted_at AS at, 'SIGNAL_CREATED' AS type,
               s.direction || ' ' || s.instrument AS message
        FROM strategy.entry_signals s
        WHERE s.strategy_id = i.strategy_id AND s.strategy_instance_id = i.instance_id
        UNION ALL
        SELECT o.exit_timestamp, o.status, s.instrument || ' ' || o.status
        FROM strategy.entry_signal_outcomes o JOIN strategy.entry_signals s USING (signal_id)
        WHERE s.strategy_id = i.strategy_id AND s.strategy_instance_id = i.instance_id
          AND o.status <> 'OPEN' AND o.exit_timestamp IS NOT NULL) e
    ORDER BY e.at DESC NULLS LAST LIMIT 1) ev ON TRUE
{where}
ORDER BY i.strategy_id, i.instance_id"""

# Runtimes that read platform.strategy_instance.enabled before producing new signals. ONLINE/OFFLINE
# is only offered where it is enforced; elsewhere the control is refused rather than faked.
LIFECYCLE_ENFORCEMENT: dict[str, str] = {
    "LIQUIDITY_DISPLACEMENT_SCALP_V1": "Signal orchestrator and Liquidity live runtime evaluate only ONLINE instances",
}
LIFECYCLE_NOT_ENFORCED_REASON = ("The runtime for this strategy does not read instance enablement yet; "
                                 "ONLINE/OFFLINE would not change what it evaluates")

# Friendly labels for known instance parameters (attributes); unknown keys are shown as-is.
_PARAMETER_LABELS = {"symbol": "Symbol", "target_r": "Target (R)", "entry_fraction": "Entry fraction",
                     "max_hold_minutes": "Max hold (min)"}
_HIDDEN_ATTRIBUTES = {"parent_strategy_id", "provider_symbol", "execution_account"}


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


def _parameters(attributes: dict[str, Any]) -> list[dict[str, Any]]:
    ordered = [k for k in _PARAMETER_LABELS if k in attributes]
    ordered += sorted(k for k in attributes if k not in _PARAMETER_LABELS and k not in _HIDDEN_ATTRIBUTES)
    return [{"key": k, "label": _PARAMETER_LABELS.get(k, k.replace("_", " ").capitalize()),
             "value": attributes[k]} for k in ordered
            if isinstance(attributes[k], (str, int, float, bool))]


def _instance(row: dict[str, Any], execution: dict[str, Any] | None) -> dict[str, Any]:
    attributes = row["attributes"] if isinstance(row["attributes"], dict) else {}
    enforcement = LIFECYCLE_ENFORCEMENT.get(row["strategy_id"])
    strategy_ref = f"{row['strategy_id']}@{row['strategy_version']}"
    instruments = list(row["instruments"] or [])
    last_event = ({"at": row["last_event_at"], "type": row["last_event_type"], "message": row["last_event_message"]}
                  if row["last_event_at"] is not None else None)
    return {
        "instance_id": row["instance_id"], "strategy_id": row["strategy_id"],
        "display_name": row["display_name"], "enabled": bool(row["enabled"]),
        "lifecycle_state": "ONLINE" if row["enabled"] else "OFFLINE",
        "revision": int(row["revision"]), "created_at": row["created_at"], "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
        "attributes": attributes, "parameters": _parameters(attributes),
        "instruments": {"active": instruments, "active_count": len(instruments),
                        "disabled_count": int(row["instruments_disabled"]), "source": "INSTRUMENT_MEMBERSHIP"},
        "stats": {"scope": "STRATEGY_INSTANCE", "signals": int(row["signals"]),
                  "signals_24h": int(row["signals_24h"]), "open": int(row["open_n"]),
                  "closed": int(row["closed_n"]), "outcomes_tracked": int(row["outcomes_tracked"]),
                  "outcomes_untracked": int(row["signals"]) - int(row["outcomes_tracked"]),
                  "realized_r_total": _num(row["realized_r_total"]),
                  "last_event_at": row["last_event_at"], "last_event": last_event},
        "lifecycle_control": {"enforced": enforcement is not None,
                              "detail": enforcement or LIFECYCLE_NOT_ENFORCED_REASON},
        # Runtime gating facts only; no runtime heartbeat is recorded per instance in PostgreSQL.
        "runtime": {"parent_strategy_enabled": bool(row["parent_enabled"]),
                    "effective": bool(row["enabled"]) and bool(row["parent_enabled"])},
        # Strategy-level V2 facts, read-only. ONLINE/OFFLINE never changes any of them.
        "execution": ({**execution, "strategy_ref": strategy_ref,
                       "allow_listed": strategy_ref in execution["allowed_strategies"],
                       "eligible": (execution["authority_state"] == "ENABLED" and execution["risk_policy_enabled"]
                                    and strategy_ref in execution["allowed_strategies"])}
                      if execution is not None else {"status": "UNAVAILABLE", "strategy_ref": strategy_ref,
                                                     "eligible": None}),
    }


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    closed = int(row["target_hits"]) + int(row["stops"])
    return {
        "strategy_id": row["strategy_id"], "strategy_version": row["strategy_version"],
        "display_name": row["display_name"], "description": row["description"], "adapter": row["adapter"],
        "enabled": bool(row["enabled"]), "routes": row["routes"] or {},
        "default_instance_id": row["default_instance_id"], "revision": int(row["revision"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        # Active instrument membership of the default instance; falls back to nothing, never to a
        # page of signals.
        "symbols": list(row["symbols"] or []),
        "signals_published": int(row["signals_published"]), "signals_24h": int(row["signals_24h"]),
        "last_event_at": row["last_event_at"], "first_signal_at": row["first_signal_at"],
        "open_observations": int(row["open_observations"]),
        "outcomes": {
            "tracked": int(row["outcomes_tracked"]),
            "untracked": int(row["signals_published"]) - int(row["outcomes_tracked"]),
            "open": int(row["open_observations"]), "target_hits": int(row["target_hits"]),
            "stops": int(row["stops"]), "closed": closed,
            "win_rate": (int(row["target_hits"]) / closed) if closed else None,
            "realized_r_total": _num(row["realized_r_total"]), "expectancy_r": _num(row["realized_r_avg"]),
            "average_target_r": _num(row["avg_target_r"]),
        },
        "stats_scope": "FULL_CANONICAL_HISTORY",
    }


class InstanceNotFound(LookupError):
    pass


class InstanceRevisionConflict(RuntimeError):
    pass


class LifecycleNotEnforced(RuntimeError):
    pass


class StrategyCatalogRepository:
    def __init__(self, connect_fn: Callable[..., Any] = connect):
        self._connect = connect_fn

    def _run(self, work: Callable[[Any], Any]) -> Any:
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s",
                                (STRATEGY_SCHEMA_VERSION,))
                    if cur.fetchone() is None:
                        raise CanonicalSourceUnavailable("canonical PostgreSQL schema 028 is required")
                    return work(cur)
        except CanonicalSourceUnavailable:
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable(f"canonical PostgreSQL strategy catalog unavailable: {exc}") from exc

    @staticmethod
    def _rows(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cur.execute(sql, params)
        return [_row_dict(cur, r) for r in cur.fetchall()]

    def list_strategies(self) -> list[dict[str, Any]]:
        def read(cur: Any) -> list[dict[str, Any]]:
            summaries = [_summary(r) for r in self._rows(cur, _SUMMARY_SQL.format(where=""))]
            instances = self._instances(cur)
            for s in summaries:
                self._attach_instances(s, [i for i in instances if i["strategy_id"] == s["strategy_id"]])
            return summaries
        return self._run(read)

    def strategy_ids(self) -> set[str]:
        return {s["strategy_id"] for s in self.list_strategies()}

    @staticmethod
    def _attach_instances(summary: dict[str, Any], instances: list[dict[str, Any]]) -> None:
        summary["instances"] = instances
        summary["instance_count"] = len(instances)
        # Signals no instance row accounts for stay visible instead of silently vanishing.
        summary["unattributed_signals"] = summary["signals_published"] - sum(i["stats"]["signals"] for i in instances)

    @staticmethod
    def _require_instances(cur: Any) -> None:
        cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (INSTANCE_SCHEMA_VERSION,))
        if cur.fetchone() is None:
            raise CanonicalSourceUnavailable("canonical PostgreSQL schema 030 is required")

    @staticmethod
    def _execution_facts(cur: Any) -> dict[str, Any] | None:
        cur.execute("""SELECT to_regclass('execution_v2.execution_authority') IS NOT NULL
                              AND to_regclass('execution_v2.risk_policy') IS NOT NULL""")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT state FROM execution_v2.execution_authority WHERE authority_id = 'current'")
        authority = cur.fetchone()
        cur.execute("SELECT enabled FROM execution_v2.risk_policy WHERE policy_id = 'current'")
        policy = cur.fetchone()
        cur.execute("SELECT strategy_ref FROM execution_v2.risk_policy_allowed_strategy WHERE policy_id = 'current'")
        allowed = sorted(r[0] for r in cur.fetchall())
        return {"status": "AVAILABLE", "authority_state": authority[0] if authority else "DISABLED",
                "risk_policy_enabled": bool(policy[0]) if policy else False, "allowed_strategies": allowed}

    def _instances(self, cur: Any, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        self._require_instances(cur)
        execution = self._execution_facts(cur)
        return [_instance(r, execution) for r in self._rows(cur, _INSTANCE_SQL.format(where=where), params)]

    def strategy_instances(self, strategy_id: str) -> list[dict[str, Any]]:
        return self._run(lambda cur: self._instances(cur, "WHERE i.strategy_id = %s", (strategy_id,)))

    def instance_page(self, strategy_id: str, instance_id: str) -> dict[str, Any] | None:
        """The complete instance page model: every list and statistic scoped to this instance."""
        def read(cur: Any) -> dict[str, Any] | None:
            rows = self._instances(cur, "WHERE i.strategy_id = %s AND i.instance_id = %s", (strategy_id, instance_id))
            if not rows:
                return None
            page = rows[0]
            scope = (strategy_id, instance_id)
            parent = self._rows(cur, "SELECT display_name, strategy_version FROM platform.strategy_definition "
                                     "WHERE strategy_id = %s", (strategy_id,))[0]
            membership = self._rows(cur, """SELECT m.canonical_instrument, m.state, m.revision, m.updated_at,
                    m.updated_by, p.provider_symbol, p.asset_class
                FROM strategy.instrument_membership m
                LEFT JOIN platform.instrument_provider_mapping p
                  ON p.canonical_instrument = m.canonical_instrument AND p.provider = 'MT5'
                WHERE m.strategy_id = %s AND m.strategy_instance_id = %s ORDER BY m.canonical_instrument""", scope)
            observations = self._rows(cur, """SELECT s.signal_id AS id, s.instrument AS symbol, s.direction,
                    s.terminal_state AS stage, s.decision_time AS opened_at, coalesce(o.status, 'UNTRACKED') AS status,
                    s.entry_price AS simulated_entry, s.stop_price AS simulated_stop,
                    s.target_price AS simulated_target, s.target_r AS simulated_rr, o.realized_r, o.exit_timestamp
                FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
                WHERE s.strategy_id = %s AND s.strategy_instance_id = %s
                ORDER BY (coalesce(o.status, '') = 'OPEN') DESC, s.decision_time DESC LIMIT %s""",
                                     (*scope, RECENT_LIMIT))
            events = self._rows(cur, """SELECT * FROM (
                    SELECT s.signal_id || ':created' AS id, s.signal_emitted_at AS timestamp, 'SIGNAL_CREATED' AS type,
                           s.direction || ' ' || s.instrument || ' @ ' || s.entry_price AS message, s.instrument AS symbol
                    FROM strategy.entry_signals s WHERE s.strategy_id = %s AND s.strategy_instance_id = %s
                    UNION ALL
                    SELECT o.signal_id || ':' || o.status, o.exit_timestamp, o.status,
                           s.instrument || ' ' || o.status || ' ' || round(o.realized_r::numeric, 2) || 'R', s.instrument
                    FROM strategy.entry_signal_outcomes o JOIN strategy.entry_signals s USING (signal_id)
                    WHERE s.strategy_id = %s AND s.strategy_instance_id = %s
                      AND o.status <> 'OPEN' AND o.exit_timestamp IS NOT NULL) e
                ORDER BY timestamp DESC NULLS LAST LIMIT %s""", (*scope, *scope, RECENT_LIMIT))
            page.update({
                "strategy_display_name": parent["display_name"], "strategy_version": parent["strategy_version"],
                "instrument_membership": membership,
                "observations": [{**o, "simulated_entry": _num(o["simulated_entry"]),
                                  "simulated_stop": _num(o["simulated_stop"]),
                                  "simulated_target": _num(o["simulated_target"]),
                                  "simulated_rr": _num(o["simulated_rr"]), "realized_r": _num(o["realized_r"])}
                                 for o in observations],
                "events": events,
            })
            return page
        return self._run(read)

    def set_instance_lifecycle(self, strategy_id: str, instance_id: str, state: str, *,
                               expected_revision: Any, updated_by: str) -> dict[str, Any]:
        """Revision-checked ONLINE/OFFLINE: writes only platform.strategy_instance.enabled.

        Never touches execution authority, the V2 risk policy or its allow-lists, or the parent
        strategy definition. Refused where the strategy's runtime does not enforce it."""
        state = str(state or "").upper()
        if state not in ("ONLINE", "OFFLINE"):
            raise ValueError("state must be ONLINE or OFFLINE")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValueError("expectedRevision (integer) is required")
        if not updated_by.strip():
            raise ValueError("updatedBy is required")
        try:
            with self._connect(readonly=False) as conn:
                with conn.cursor() as cur:
                    self._require_instances(cur)
                    cur.execute("""SELECT enabled, revision FROM platform.strategy_instance
                                   WHERE strategy_id = %s AND instance_id = %s FOR UPDATE""",
                                (strategy_id, instance_id))
                    row = cur.fetchone()
                    if row is None:
                        raise InstanceNotFound(f"{strategy_id}/{instance_id} does not exist")
                    if strategy_id not in LIFECYCLE_ENFORCEMENT:
                        raise LifecycleNotEnforced(LIFECYCLE_NOT_ENFORCED_REASON)
                    if int(row[1]) != expected_revision:
                        raise InstanceRevisionConflict(
                            f"revision conflict: expected {expected_revision}, current {row[1]}")
                    cur.execute("""UPDATE platform.strategy_instance
                                   SET enabled = %s, revision = revision + 1, updated_at = now(), updated_by = %s
                                   WHERE strategy_id = %s AND instance_id = %s""",
                                (state == "ONLINE", updated_by.strip(), strategy_id, instance_id))
                conn.commit()
        except (InstanceNotFound, LifecycleNotEnforced, InstanceRevisionConflict, CanonicalSourceUnavailable):
            raise
        except Exception as exc:
            raise CanonicalSourceUnavailable(f"canonical PostgreSQL strategy instance write unavailable: {exc}") from exc
        # The response is re-read from the database, never echoed from the request.
        return self._run(lambda cur: self._instances(
            cur, "WHERE i.strategy_id = %s AND i.instance_id = %s", (strategy_id, instance_id))[0])

    def strategy_page(self, strategy_id: str) -> dict[str, Any] | None:
        return self._run(lambda cur: self._page(cur, strategy_id))

    def _page(self, cur: Any, strategy_id: str) -> dict[str, Any] | None:
        rows = self._rows(cur, _SUMMARY_SQL.format(where="WHERE d.strategy_id = %s"), (strategy_id,))
        if not rows:
            return None
        page = _summary(rows[0])
        self._attach_instances(page, self._instances(cur, "WHERE i.strategy_id = %s", (strategy_id,)))
        out = page["outcomes"]

        series_rows = self._rows(cur, """SELECT (o.exit_timestamp AT TIME ZONE 'UTC')::date AS day,
                sum(o.realized_r) AS realized_r, count(*) AS closed
            FROM strategy.entry_signal_outcomes o JOIN strategy.entry_signals s USING (signal_id)
            WHERE s.strategy_id = %s AND o.status <> 'OPEN' AND o.exit_timestamp IS NOT NULL
            GROUP BY 1 ORDER BY 1""", (strategy_id,))
        cumulative, series = 0.0, []
        for r in series_rows:
            cumulative += float(r["realized_r"] or 0)
            day = r["day"].isoformat() if isinstance(r["day"], (date, datetime)) else str(r["day"])
            series.append({"date": day, "realized_r": float(r["realized_r"] or 0),
                           "cumulative_realized_r": cumulative, "closed": int(r["closed"])})

        by_instrument = self._rows(cur, """SELECT s.instrument, count(*) AS signals,
                count(*) FILTER (WHERE o.status = 'OPEN') AS open,
                count(*) FILTER (WHERE o.status = 'TARGET_HIT') AS target_hits,
                count(*) FILTER (WHERE o.status = 'STOPPED') AS stops,
                sum(o.realized_r) FILTER (WHERE o.status <> 'OPEN') AS realized_r
            FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
            WHERE s.strategy_id = %s GROUP BY 1 ORDER BY 1""", (strategy_id,))

        observations = self._rows(cur, """SELECT s.signal_id AS id, s.instrument AS symbol, s.direction,
                s.terminal_state AS stage, s.decision_time AS opened_at, coalesce(o.status, 'UNTRACKED') AS status,
                s.entry_price AS simulated_entry, s.stop_price AS simulated_stop, s.target_price AS simulated_target,
                s.target_r AS simulated_rr, o.realized_r, o.exit_timestamp
            FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
            WHERE s.strategy_id = %s
            ORDER BY (coalesce(o.status, '') = 'OPEN') DESC, s.decision_time DESC LIMIT %s""",
                                  (strategy_id, RECENT_LIMIT))

        events = self._rows(cur, """SELECT * FROM (
                SELECT s.signal_id || ':created' AS id, s.signal_emitted_at AS timestamp, 'SIGNAL_CREATED' AS type,
                       s.direction || ' ' || s.instrument || ' @ ' || s.entry_price AS message, s.instrument AS symbol
                FROM strategy.entry_signals s WHERE s.strategy_id = %s
                UNION ALL
                SELECT o.signal_id || ':' || o.status, o.exit_timestamp, o.status,
                       s.instrument || ' ' || o.status || ' ' || round(o.realized_r::numeric, 2) || 'R', s.instrument
                FROM strategy.entry_signal_outcomes o JOIN strategy.entry_signals s USING (signal_id)
                WHERE s.strategy_id = %s AND o.status <> 'OPEN' AND o.exit_timestamp IS NOT NULL) e
            ORDER BY timestamp DESC NULLS LAST LIMIT %s""", (strategy_id, strategy_id, RECENT_LIMIT))

        latest = self._rows(cur, """SELECT source_provenance, strategy_ref FROM strategy.entry_signals
            WHERE strategy_id = %s ORDER BY signal_emitted_at DESC NULLS LAST LIMIT 1""", (strategy_id,))
        provenance = (latest[0]["source_provenance"] or {}) if latest else {}

        instruments = self._rows(cur, """SELECT m.canonical_instrument, m.state, m.revision, m.updated_at, m.updated_by,
                p.provider_symbol, p.asset_class
            FROM strategy.instrument_membership m
            LEFT JOIN platform.instrument_provider_mapping p
              ON p.canonical_instrument = m.canonical_instrument AND p.provider = 'MT5'
            WHERE m.strategy_id = %s AND m.strategy_instance_id = %s ORDER BY m.canonical_instrument""",
                                 (strategy_id, page["default_instance_id"]))

        page.update({
            "lifecycle": [
                {"key": "SIGNALS", "label": "Entry signals", "count": page["signals_published"]},
                {"key": "OPEN", "label": "Open", "count": out["open"]},
                {"key": "TARGET_HIT", "label": "Target hit", "count": out["target_hits"]},
                {"key": "STOPPED", "label": "Stopped", "count": out["stops"]},
                {"key": "UNTRACKED", "label": "No canonical outcome", "count": out["untracked"],
                 "description": "Signals without a strategy outcome record (e.g. before outcome tracking began)"},
            ],
            "stats": [
                {"key": "signals_published", "label": "Signals", "value": page["signals_published"], "group": "Signals"},
                {"key": "signals_24h", "label": "Signals (24h)", "value": page["signals_24h"], "group": "Signals"},
                {"key": "open", "label": "Open", "value": out["open"], "group": "Outcomes"},
                {"key": "closed", "label": "Closed", "value": out["closed"], "group": "Outcomes"},
                {"key": "win_rate", "label": "Win rate", "value": out["win_rate"], "group": "Outcomes"},
                {"key": "realized_r_total", "label": "Realized R", "value": out["realized_r_total"], "group": "Outcomes"},
                {"key": "expectancy_r", "label": "Expectancy (R)", "value": out["expectancy_r"], "group": "Outcomes"},
                {"key": "untracked", "label": "Outcome untracked", "value": out["untracked"], "group": "Coverage"},
            ],
            "performance": {"win_rate": out["win_rate"], "target_hits": out["target_hits"], "stops": out["stops"],
                            "closed": out["closed"], "open": out["open"], "average_rr": out["average_target_r"],
                            "realized_r_total": out["realized_r_total"], "series": series,
                            "by_instrument": [{**r, "realized_r": _num(r["realized_r"])} for r in by_instrument]},
            "observations": [{**o, "simulated_entry": _num(o["simulated_entry"]), "simulated_stop": _num(o["simulated_stop"]),
                              "simulated_target": _num(o["simulated_target"]), "simulated_rr": _num(o["simulated_rr"]),
                              "realized_r": _num(o["realized_r"])} for o in observations],
            "events": events,
            "instruments": instruments,
            "configuration": [
                {"key": "strategy_version", "label": "Version", "value": page["strategy_version"], "category": "Identity"},
                {"key": "adapter", "label": "Adapter", "value": page["adapter"] or "", "category": "Identity"},
                {"key": "enabled", "label": "Enabled", "value": page["enabled"], "category": "Operation"},
                {"key": "default_instance_id", "label": "Instance", "value": page["default_instance_id"] or "",
                 "category": "Operation"},
                *[{"key": f"route_{k}", "label": k.replace("_", " ").title(), "value": bool(v), "category": "Routes"}
                  for k, v in sorted((page["routes"] or {}).items())],
            ],
            "technical_metadata": {
                "engine_version": provenance.get("source_strategy_fingerprint"),
                "config_hash": provenance.get("source_config_hash"),
                "data_source": provenance.get("source_process"),
                "strategy_ref": latest[0]["strategy_ref"] if latest else None,
                "definition_revision": page["revision"],
            },
            "trade_management": self._trade_management(cur, strategy_id, rows[0]["trade_management"]),
        })
        return page

    def _trade_management(self, cur: Any, strategy_id: str, policy: Any) -> dict[str, Any]:
        """Effective TM policy: explicit legacy_stream_binding rows if any, otherwise the default
        TM-NONE-1 resolution every unbound trade actually gets (never reported as 'unavailable')."""
        bindings = self._rows(cur, """SELECT b.binding_id, b.instrument, b.tm_version_id, b.valid_from,
                v.evaluator_id, v.label, v.status, v.manifest
            FROM trade_management.legacy_stream_binding b
            JOIN trade_management.trade_manager_version v ON v.tm_version_id = b.tm_version_id
            WHERE b.strategy_id = %s ORDER BY b.valid_from DESC, b.binding_id""", (strategy_id,))
        resolution = "LEGACY_STATIC" if bindings else "DEFAULT_TM_NONE"
        if not bindings:
            bindings = self._rows(cur, """SELECT NULL AS binding_id, NULL AS instrument, v.tm_version_id,
                    NULL AS valid_from, v.evaluator_id, v.label, v.status, v.manifest
                FROM trade_management.trade_manager_version v WHERE v.label = 'TM-NONE-1'""")
        trades = self._rows(cur, """SELECT count(*) FILTER (WHERE state = 'OPEN') AS open,
                count(*) FILTER (WHERE state = 'CLOSED') AS closed, count(*) AS total
            FROM trade_management.managed_trade WHERE strategy_id = %s""", (strategy_id,))
        return {"resolution": resolution, "bindings": bindings, "configured_policy": policy,
                "managed_trades": trades[0] if trades else {"open": 0, "closed": 0, "total": 0}}
