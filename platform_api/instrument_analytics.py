"""Authoritative, server-side strategy-instance instrument analytics.

The read model deliberately keeps theoretical, retrospective, forward-paper and broker
populations separate.  Missing evidence is represented as null/insufficient data; no price
or pip conversion is guessed here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from postgres.db import connect
from .signals import CanonicalSourceUnavailable, _row_dict

AUDIT_SCHEMA_VERSION = "061"
PIP_SIZE_SCHEMA_VERSION = "062"


class InstrumentAnalyticsRepository:
    def __init__(self, connect_fn: Callable[..., Any] = connect):
        self._connect = connect_fn

    @staticmethod
    def _period(query: dict[str, list[str]]) -> tuple[datetime, datetime, str]:
        now = datetime.now(timezone.utc)
        key = (query.get("period") or ["30D"])[0].upper()
        if key in {"7D", "30D", "90D"}:
            return now - timedelta(days=int(key[:-1])), now, key
        if key == "CUSTOM":
            try:
                start = datetime.fromisoformat((query.get("from") or [""])[0].replace("Z", "+00:00"))
                end = datetime.fromisoformat((query.get("to") or [""])[0].replace("Z", "+00:00"))
                if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
                if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
                if end <= start: raise ValueError
                return start, end, key
            except (TypeError, ValueError):
                raise ValueError("CUSTOM period requires valid from and to timestamps")
        raise ValueError("period must be 7D, 30D, 90D, or CUSTOM")

    @staticmethod
    def _filters(query: dict[str, list[str]], start: datetime, end: datetime) -> tuple[str, list[Any]]:
        clauses = ["s.strategy_id = %s", "s.strategy_instance_id = %s",
                   "COALESCE(o.exit_timestamp, s.signal_emitted_at, s.decision_time) >= %s",
                   "COALESCE(o.exit_timestamp, s.signal_emitted_at, s.decision_time) < %s"]
        params: list[Any] = [None, None, start, end]
        instrument = (query.get("instrument") or [""])[0].strip().upper()
        direction = (query.get("direction") or ["ALL"])[0].upper()
        version = (query.get("strategy_version") or [""])[0]
        parameter = (query.get("parameter_set_ref") or [""])[0]
        if instrument: clauses.append("s.instrument = %s"); params.append(instrument)
        if direction in {"LONG", "SHORT"}: clauses.append("s.direction = %s"); params.append(direction)
        if version: clauses.append("s.strategy_version = %s"); params.append(version)
        if parameter: clauses.append("COALESCE(s.parameter_set_ref, '') = %s"); params.append(parameter)
        return " AND ".join(clauses), params

    @staticmethod
    def _population(query: dict[str, list[str]]) -> str:
        value = (query.get("provenance") or ["THEORETICAL"])[0].upper()
        allowed = {"THEORETICAL", "RETROSPECTIVE", "FORWARD_PAPER", "BROKER_EXECUTED", "UNKNOWN", "ALL"}
        if value not in allowed: raise ValueError("provenance is invalid")
        return value

    @staticmethod
    def _population_sql(population: str) -> str:
        # Classification uses persisted evidence only.  A retrospective signal emitted after
        # its theoretical exit is not timely forward performance.
        # source_provenance_text: combines legacy 'classification' key and KOJO V3's
        # 'signal_emission_class' key so both strategies classify correctly.
        prov_text = (
            "lower(COALESCE(s.evidence_class, '') || ' '"
            "|| COALESCE(s.source_provenance->>'classification', '') || ' '"
            "|| COALESCE(s.source_provenance->'provenance'->>'signal_emission_class', '') || ' '"
            "|| COALESCE(s.source_provenance->>'signal_emission_class', ''))"
        )
        classification = (
            f"CASE WHEN o.outcome_kind = 'BROKER_REALIZED' OR o.source_kind = 'BROKER_EXECUTION' THEN 'BROKER_EXECUTED' "
            # psycopg3 treats percent sequences in a parameterized query as
            # placeholders. Escape the SQL LIKE wildcards so patterns such as
            # ``%research%`` are sent to PostgreSQL as literal percent signs
            # instead of being parsed as the unsupported ``%r`` format.
            f"WHEN {prov_text} LIKE ANY (ARRAY['%%research%%', '%%retro%%', '%%backfill%%']) THEN 'RETROSPECTIVE' "
            f"WHEN {prov_text} LIKE ANY (ARRAY['%%canonical_runtime%%', '%%forward%%', '%%prospective%%', '%%paper%%']) THEN 'FORWARD_PAPER' "
            f"ELSE 'UNKNOWN' END"
        )
        if population == "THEORETICAL":
            return "(o.outcome_kind IS DISTINCT FROM 'BROKER_REALIZED' AND o.source_kind IS DISTINCT FROM 'BROKER_EXECUTION')"
        if population == "BROKER_EXECUTED": return "(o.outcome_kind = 'BROKER_REALIZED' OR o.source_kind = 'BROKER_EXECUTION')"
        if population == "RETROSPECTIVE": return f"({classification}) = 'RETROSPECTIVE'"
        if population == "UNKNOWN": return f"({classification}) = 'UNKNOWN'"
        if population == "FORWARD_PAPER":
            # Exclude signals where the emission timestamp post-dates the exit timestamp
            # (emitted-after-outcome, e.g. pod-restart replay signals).
            return (
                f"({classification}) = 'FORWARD_PAPER' AND NOT ("
                f"{prov_text} LIKE ANY (ARRAY['%%retro%%']) "
                f"AND o.exit_timestamp IS NOT NULL AND s.signal_emitted_at > o.exit_timestamp)"
            )
        return "TRUE"

    def read(self, strategy_id: str, instance_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        start, end, period = self._period(query)
        population = self._population(query)
        where, params = self._filters(query, start, end)
        params[0:2] = [strategy_id, instance_id]
        pop = self._population_sql(population)
        try:
            limit = min(max(int((query.get("limit") or ["50"])[0]), 1), 200)
            offset = max(int((query.get("offset") or ["0"])[0]), 0)
        except ValueError:
            raise ValueError("limit and offset must be integers")
        sort_key = (query.get("sort") or ["instrument"])[0]
        sort_sql = {"instrument": "instrument", "netR": "net_r DESC NULLS LAST", "closed": "closed DESC", "wins": "wins DESC"}.get(sort_key)
        if sort_sql is None: raise ValueError("unsupported instrument sort")
        try:
            with self._connect(readonly=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (PIP_SIZE_SCHEMA_VERSION,))
                    if cur.fetchone() is None: raise CanonicalSourceUnavailable("schema 062 is required for planned pip metrics")
                    cur.execute(f"""WITH scoped AS (
                        SELECT s.signal_id, s.instrument, s.direction, s.strategy_version, s.parameter_set_ref,
                               s.signal_emitted_at, s.entry_price, s.stop_price, s.target_price, p.pip_size,
                               o.status, o.realized_r, o.exit_timestamp, o.outcome_kind,
                               o.source_kind, o.resolution_state, o.exit_price, o.activation_price,
                               o.resolution_evidence, {self._population_sql('ALL')} AS all_population
                        FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
                        LEFT JOIN platform.instrument_provider_mapping p
                          ON p.provider = 'MT5' AND p.state = 'ACTIVE' AND p.canonical_instrument = s.instrument
                        WHERE {where} AND {pop}
                    ), closed AS (SELECT * FROM scoped WHERE status <> 'OPEN' AND realized_r IS NOT NULL)
                    SELECT instrument, count(*) FILTER (WHERE status IS NULL) AS untracked,
                           count(*) FILTER (WHERE status IS NOT NULL) AS tracked,
                           count(*) FILTER (WHERE status = 'OPEN') AS open,
                           count(*) FILTER (WHERE status <> 'OPEN' AND realized_r IS NOT NULL) AS closed,
                           count(*) FILTER (WHERE realized_r > 0) AS wins,
                           count(*) FILTER (WHERE realized_r < 0) AS losses,
                           COALESCE(sum(realized_r) FILTER (WHERE status <> 'OPEN'), 0) AS net_r,
                           COALESCE(sum(realized_r) FILTER (WHERE realized_r > 0), 0) AS gross_win,
                           COALESCE(sum(realized_r) FILTER (WHERE realized_r < 0), 0) AS gross_loss,
                           avg(abs(target_price - entry_price) / NULLIF(pip_size, 0))
                               FILTER (WHERE target_price IS NOT NULL AND entry_price IS NOT NULL AND pip_size IS NOT NULL)
                               AS planned_target_pips,
                           avg(abs(stop_price - entry_price) / NULLIF(pip_size, 0))
                               FILTER (WHERE stop_price IS NOT NULL AND entry_price IS NOT NULL AND pip_size IS NOT NULL)
                               AS planned_stop_pips,
                           max(pip_size) AS pip_size,
                           min(signal_emitted_at) AS first_signal_at, max(exit_timestamp) AS last_exit_at
                    FROM scoped GROUP BY instrument ORDER BY {sort_sql} LIMIT %s OFFSET %s""", params + [limit, offset])
                    rows = [_row_dict(cur, row) for row in cur.fetchall()]
                    cur.execute(f"""WITH scoped AS (
                        SELECT s.instrument, COALESCE(o.exit_timestamp, s.signal_emitted_at, s.decision_time) AS at,
                               o.realized_r, o.status
                        FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
                        WHERE {where} AND {pop}
                    ) SELECT date_trunc('week', at)::date AS bucket, COALESCE(sum(realized_r) FILTER (WHERE status <> 'OPEN'), 0) AS net_r,
                             count(*) FILTER (WHERE status <> 'OPEN' AND realized_r IS NOT NULL) AS closed
                      FROM scoped GROUP BY 1 ORDER BY 1""", params)
                    trend = [_row_dict(cur, row) for row in cur.fetchall()]
                    cur.execute(f"""WITH scoped AS (
                        SELECT COALESCE(o.exit_timestamp, s.signal_emitted_at, s.decision_time) AS at,
                               o.realized_r, o.status
                        FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
                        WHERE {where} AND {pop})
                        SELECT date_trunc('month', at)::date AS bucket,
                               COALESCE(sum(realized_r) FILTER (WHERE status <> 'OPEN'), 0) AS net_r,
                               count(*) FILTER (WHERE status <> 'OPEN' AND realized_r IS NOT NULL) AS closed
                        FROM scoped GROUP BY 1 ORDER BY 1""", params)
                    monthly_trend = [_row_dict(cur, row) for row in cur.fetchall()]
                    cur.execute(f"""SELECT s.instrument, COALESCE(o.exit_timestamp, s.signal_emitted_at, s.decision_time) AS at,
                        o.realized_r FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
                        WHERE {where} AND {pop} AND o.status <> 'OPEN' AND o.realized_r IS NOT NULL
                        ORDER BY at, s.signal_id""", params)
                    trade_rows = [_row_dict(cur, row) for row in cur.fetchall()]
                    cumulative = 0.0; peak = 0.0; max_dd = 0.0
                    for trade in trade_rows:
                        cumulative += float(trade["realized_r"] or 0); peak = max(peak, cumulative)
                        max_dd = max(max_dd, peak - cumulative)
                    rolling = (sum(float(t["realized_r"] or 0) for t in trade_rows[-10:]) / min(10, len(trade_rows))) if trade_rows else None
                    prev_duration = end - start
                    prev_start, prev_end = start - prev_duration, start
                    prev_where, prev_params = self._filters(query, prev_start, prev_end)
                    prev_params[0:2] = [strategy_id, instance_id]
                    cur.execute(f"""SELECT COALESCE(sum(o.realized_r), 0) AS net_r,
                        count(*) AS closed FROM strategy.entry_signals s
                        LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
                        WHERE {prev_where} AND {pop} AND o.status <> 'OPEN' AND o.realized_r IS NOT NULL""", prev_params)
                    previous = _row_dict(cur, cur.fetchone())
                    cur.execute(f"""WITH scoped AS (SELECT DISTINCT s.instrument FROM strategy.entry_signals s
                        LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id) WHERE {where} AND {pop})
                        SELECT count(*) AS total FROM scoped""", params)
                    total = int(cur.fetchone()[0] or 0)
                    classification_expr = self._population_sql("UNKNOWN")
                    cur.execute(f"""SELECT count(*) FROM strategy.entry_signals s
                        LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
                        WHERE {where} AND {classification_expr}""", params)
                    uncertain_count = int(cur.fetchone()[0] or 0)
                    cur.execute(f"""SELECT count(*) FILTER (WHERE o.status = 'OPEN') AS theoretical_open,
                        count(*) FILTER (WHERE i.status IN ('PENDING','CLAIMED')) AS pending_execution_intents
                        FROM strategy.entry_signals s LEFT JOIN strategy.entry_signal_outcomes o USING(signal_id)
                        LEFT JOIN execution_v2.execution_intent i ON i.entry_signal_id = s.signal_id
                        WHERE s.strategy_id = %s AND s.strategy_instance_id = %s AND s.instrument = COALESCE(NULLIF(%s,''), s.instrument)""",
                                (strategy_id, instance_id, (query.get("instrument") or [""])[0].upper()))
                    exposure = _row_dict(cur, cur.fetchone())
                    cur.execute("""SELECT canonical_instrument, provider_symbol, asset_class, pip_size
                        FROM platform.instrument_provider_mapping WHERE provider = 'MT5' AND state = 'ACTIVE'
                        AND canonical_instrument = ANY(%s)""", ([r["instrument"] for r in rows],))
                    mapping = {_row_dict(cur, r)["canonical_instrument"]: _row_dict(cur, r) for r in cur.fetchall()}
            metrics = []
            for r in rows:
                closed = int(r["closed"] or 0); wins = int(r["wins"] or 0); loss = int(r["losses"] or 0)
                gross_loss = float(r["gross_loss"] or 0); net = float(r["net_r"] or 0)
                instrument_trades = [t for t in trade_rows if t["instrument"] == r["instrument"]]
                instrument_rs = [float(t["realized_r"] or 0) for t in instrument_trades]
                instrument_cum = 0.0; instrument_peak = 0.0; instrument_dd = 0.0
                for value in instrument_rs:
                    instrument_cum += value; instrument_peak = max(instrument_peak, instrument_cum)
                    instrument_dd = max(instrument_dd, instrument_peak - instrument_cum)
                wins_r = [value for value in instrument_rs if value > 0]
                losses_r = [value for value in instrument_rs if value < 0]
                metrics.append({**r, "untracked": int(r["untracked"] or 0), "tracked": int(r["tracked"] or 0), "closed": closed, "wins": wins, "losses": loss,
                    "net_r": net, "expectancy_r": net / closed if closed else None,
                    "profit_factor": abs(float(r["gross_win"] or 0) / gross_loss) if gross_loss else None,
                    "win_rate": wins / (wins + loss) if wins + loss else None,
                    "average_win_r": sum(wins_r) / len(wins_r) if wins_r else None,
                    "average_loss_r": sum(losses_r) / len(losses_r) if losses_r else None,
                    "max_drawdown_r": instrument_dd,
                    "assessment": "INSUFFICIENT_DATA" if closed < 10 else ("UNDERPERFORMING" if net <= 0 else "ADVISORY_POSITIVE"),
                    "instrument_metadata": mapping.get(r["instrument"], {"status": "UNAVAILABLE"})})
            trend_with_cumulative = []
            trend_cumulative = 0.0
            for point in trend:
                trend_cumulative += float(point["net_r"] or 0)
                trend_with_cumulative.append({**point, "cumulative_r": trend_cumulative})
            return {"strategy_id": strategy_id, "strategy_instance_id": instance_id,
                    "period": {"key": period, "from": start.isoformat(), "to": end.isoformat()},
                    "provenance": population, "metrics": metrics,
                    "trend": trend_with_cumulative, "monthly_trend": monthly_trend,
                    "summary": {"cumulative_r": cumulative, "max_drawdown_r": max_dd,
                                "rolling_expectancy_r_10": rolling,
                                "previous_period": {"net_r": float(previous["net_r"] or 0), "closed": int(previous["closed"] or 0),
                                                     "comparison_available": bool(previous["closed"]) }},
                    "pagination": {"limit": limit, "offset": offset, "total": total},
                    "exposure": {"theoretical_open": int(exposure["theoretical_open"] or 0),
                                  "pending_execution_intents": int(exposure["pending_execution_intents"] or 0),
                                  "broker_positions": {"status": "READ_ONLY_BRIDGE_REQUIRED"}},
                    "data_quality": {"authoritative": True, "partial_exit_policy": "OUTCOME_ROWS_ONLY_UNLESS_EXECUTION_FACTS_EXIST",
                                     "pip_point_policy": "CANONICAL_MAPPING_REQUIRED",
                                     "pip_point_status": "PLANNED_ONLY" if rows else "NO_ROWS",
                                     "classification_uncertain": uncertain_count,
                                     "sample_sufficient": any(int(x["closed"] or 0) >= 10 for x in rows)}}
        except (CanonicalSourceUnavailable, ValueError): raise
        except Exception as exc: raise CanonicalSourceUnavailable(f"instrument analytics unavailable: {exc}") from exc

    def audit_history(self, strategy_id: str, instance_id: str, instrument: str) -> list[dict[str, Any]]:
        with self._connect(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (AUDIT_SCHEMA_VERSION,))
                if cur.fetchone() is None: raise CanonicalSourceUnavailable("schema 061 is required")
                cur.execute("""SELECT audit_id, previous_state, new_state, actor, reason, selected_period,
                    provenance_filter, strategy_version, parameter_set_ref, performance_snapshot, created_at
                    FROM strategy.instrument_membership_audit WHERE strategy_id=%s AND strategy_instance_id=%s
                    AND canonical_instrument=%s ORDER BY created_at DESC, audit_id DESC""", (strategy_id, instance_id, instrument))
                return [_row_dict(cur, row) for row in cur.fetchall()]
