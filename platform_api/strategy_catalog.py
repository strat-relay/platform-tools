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


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


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
        return self._run(lambda cur: [_summary(r) for r in self._rows(cur, _SUMMARY_SQL.format(where=""))])

    def strategy_ids(self) -> set[str]:
        return {s["strategy_id"] for s in self.list_strategies()}

    def strategy_page(self, strategy_id: str) -> dict[str, Any] | None:
        return self._run(lambda cur: self._page(cur, strategy_id))

    def _page(self, cur: Any, strategy_id: str) -> dict[str, Any] | None:
        rows = self._rows(cur, _SUMMARY_SQL.format(where="WHERE d.strategy_id = %s"), (strategy_id,))
        if not rows:
            return None
        page = _summary(rows[0])
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
