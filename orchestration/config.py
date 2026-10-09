from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "orchestration-platform-v1",
    "execution_mode": "SHADOW",
    "mcp_url": "http://10.10.10.100:22347/mcp",
    "sizing_scenarios": [0.0025, 0.005, 0.01, 0.02],
    "accounts": [{"account_id": "exness-shadow-1", "broker": "Exness", "broker_environment": "DEMO",
                   "broker_account_reference": "REDACTED", "currency": "USD", "enabled": True,
                   "execution_mode": "SHADOW"}],
    "portfolios": [{"portfolio_id": "portfolio-shadow-context", "name": "Context Shadow Portfolio",
                     "enabled": True, "base_currency": "USD", "sizing_policy_id": "equity-fractional-v1",
                     "account_ids": ["exness-shadow-1"], "strategy_ids": ["CONTEXT_STRUCTURE_RETRACE_V1"]}],
    "strategies": [
        {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1",
         "enabled": True, "adapter": "ContextStructureRetraceAdapter",
         "routes": {"audit": True, "shadow_execution": True, "distribution_queue": True}},
        {"strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1", "strategy_version": "V1",
         "enabled": True, "status": "ACTIVE", "adapter": "LiquidityDisplacementAdapter",
         "routes": {"audit": True, "shadow_execution": False, "distribution_queue": True},
         "paper_only": True, "portfolio_routing": False},
        {"strategy_id": "BASELINE", "strategy_version": "REFERENCE", "enabled": False,
         "status": "ARCHIVED", "visibility": "INVENTORY", "adapter": None,
         "routes": {"audit": False, "shadow_execution": False, "distribution_queue": False},
         "portfolio_routing": False},
        {"strategy_id": "MICRO_SCALP", "strategy_version": "RESEARCH", "enabled": False,
         "status": "ARCHIVED_RESEARCH", "visibility": "INVENTORY", "adapter": None,
         "routes": {"audit": False, "shadow_execution": False, "distribution_queue": False},
         "portfolio_routing": False},
        {"strategy_id": "SIMPLE_SR_CANDLE_V1", "strategy_version": "V1", "enabled": False,
         "status": "HISTORICAL_RESEARCH", "visibility": "INVENTORY", "adapter": None,
         "routes": {"audit": False, "shadow_execution": False, "distribution_queue": False},
         "portfolio_routing": False},
        {"strategy_id": "LIQUIDITY_RECLAIM_CONTINUATION_RESEARCH", "strategy_version": "RESEARCH", "enabled": False,
         "status": "RESEARCH_ONLY", "visibility": "INVENTORY", "adapter": None,
         "routes": {"audit": False, "shadow_execution": False, "distribution_queue": False},
         "portfolio_routing": False},
        {"strategy_id": "MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH", "strategy_version": "RESEARCH", "enabled": False,
         "status": "RESEARCH_ONLY", "visibility": "INVENTORY", "adapter": None,
         "routes": {"audit": False, "shadow_execution": False, "distribution_queue": False},
         "portfolio_routing": False},
    ],
    "instances": [{"instance_id": "phase6", "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
                    "display_name": "Context Structure Retrace phase6", "enabled": True}],
}


CONFIG_SCHEMA_VERSION = "030"
STRATEGY_COLUMNS = ("strategy_id", "strategy_version", "enabled", "adapter", "routes")


class PlatformConfigUnavailable(RuntimeError):
    """The database is configured but the orchestration configuration cannot be loaded from it."""


def _database_configured() -> bool:
    from postgres.config import PostgresConfig
    cfg = PostgresConfig.from_env()
    return bool((cfg.dsn and cfg.dsn.strip()) or cfg.host)


def load_config_from_database(conn: Any) -> dict[str, Any]:
    """Assemble orchestration configuration from canonical DB strategy definitions and instances."""
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM platform.schema_migrations WHERE version = %s", (CONFIG_SCHEMA_VERSION,))
        if cur.fetchone() is None:
            raise PlatformConfigUnavailable("canonical PostgreSQL schema 029 is required")
        cur.execute("SELECT key, value FROM platform.runtime_setting ORDER BY key")
        settings = {key: _json(value) for key, value in cur.fetchall()}
        if not settings:
            raise PlatformConfigUnavailable(
                "orchestration configuration has not been imported (scripts/import_platform_config.py)")
        cur.execute("""SELECT account_id, broker, broker_environment, broker_account_reference, currency, enabled,
                              execution_mode, attributes FROM platform.orchestration_account ORDER BY account_id""")
        accounts = [{**_json(attrs), "account_id": a, "broker": b, "broker_environment": env,
                     "broker_account_reference": ref, "currency": cur_, "enabled": bool(en), "execution_mode": mode}
                    for a, b, env, ref, cur_, en, mode, attrs in cur.fetchall()]
        cur.execute("""SELECT portfolio_id, name, enabled, base_currency, sizing_policy_id, account_ids, strategy_ids,
                              attributes FROM platform.orchestration_portfolio ORDER BY portfolio_id""")
        portfolios = [{**_json(attrs), "portfolio_id": pid, "name": name, "enabled": bool(en), "base_currency": ccy,
                       "sizing_policy_id": policy, "account_ids": list(acc or []), "strategy_ids": list(strat or [])}
                      for pid, name, en, ccy, policy, acc, strat, attrs in cur.fetchall()]
        cur.execute("""SELECT strategy_id, strategy_version, enabled, adapter, routes, trade_management, attributes
                       FROM platform.strategy_definition ORDER BY strategy_id""")
        strategies = []
        for sid, version, enabled, adapter, routes, tm, attrs in cur.fetchall():
            record = {**_json(attrs), "strategy_id": sid, "strategy_version": version, "enabled": bool(enabled),
                      "adapter": adapter, "routes": _json(routes)}
            if tm is not None:
                record["trade_management"] = _json(tm)
            strategies.append(record)
    instances = load_instances_from_database(conn)
    known = {row["strategy_id"] for row in strategies}
    # Pipeline-created research instances are authoritative in strategy_mgmt and
    # do not require a duplicate legacy platform.strategy_definition row.
    for instance in instances:
        if instance["strategy_id"] not in known and instance.get("_source") == "strategy_instance_v2":
            strategies.append({"strategy_id": instance["strategy_id"], "strategy_version": "V2",
                                "enabled": bool(instance.get("enabled")),
                                "adapter": "ContextStructureRetraceAdapter",
                                "routes": {"audit": True, "shadow_execution": False,
                                           "distribution_queue": True},
                                "research_only": True, "paper_only": True})
            known.add(instance["strategy_id"])
    return {**settings, "accounts": accounts, "portfolios": portfolios,
            "strategies": strategies, "instances": instances}


def load_instances_from_database(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("""SELECT i.instance_id, i.strategy_id, i.display_name, i.enabled, i.attributes,
                              coalesce(array_agg(m.canonical_instrument) FILTER (WHERE m.state = 'ACTIVE'), '{}')
                       FROM platform.strategy_instance i
                       LEFT JOIN strategy.instrument_membership m
                         ON m.strategy_instance_id = i.instance_id AND m.strategy_id = i.strategy_id
                       GROUP BY i.instance_id, i.strategy_id, i.display_name, i.enabled, i.attributes
                       ORDER BY i.strategy_id, i.instance_id""")
        rows = cur.fetchall()
        # Keep local/fake database adapters compatible while the canonical schema rolls out.
        instances = [{**_json(row[4]), "instance_id": row[0], "strategy_id": row[1],
                      "display_name": row[2], "enabled": bool(row[3]),
                      "active_instruments": list(row[5] or []) if len(row) >= 6 else []}
                     for row in rows]
    # Also load pipeline-created instances from strategy_mgmt schema (online only).
    # These bridge strategy_instance_v2 records into the existing instance format so
    # load_adapters() can discover them without duplicating the identity model.
    v2 = _load_v2_instances_from_database(conn)
    return instances + v2


def _load_v2_instances_from_database(conn: Any) -> list[dict[str, Any]]:
    """Load ONLINE strategy_instance_v2 rows into the standard instance dict format.

    Only ONLINE instances (online=true) are returned.  execution_eligible is never
    relevant here — that column is owned by the execution authority system.

    Returns [] if the strategy_mgmt schema does not exist (pre-042 deployments).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT v.id, sv.evaluator_key, v.display_name, v.online,
                          v.instruments, v.attributes,
                          ps.fingerprint AS parameter_set_fingerprint,
                          COALESCE(ps.updated_at, ps.created_at) AS config_rev,
                          COALESCE(v.execution_mode, 'OFF') AS execution_mode,
                          COALESCE(v.execution_mode_revision, 0) AS execution_mode_revision
                   FROM strategy_mgmt.strategy_instance_v2 v
                   JOIN strategy_mgmt.strategy_version sv ON sv.id = v.strategy_version_id
                   JOIN strategy_mgmt.parameter_set ps ON ps.id = v.parameter_set_id
                   WHERE v.online = true
                   ORDER BY sv.evaluator_key, v.id"""
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        return []

    from strategy_backtest.kojo_structure_reclaim import STRATEGY_ID as KSR_ID, EVALUATOR_KEY as KSR_KEY
    from strategy_backtest.kojo_structure_reclaim_v3 import STRATEGY_ID as KSR_V3_ID, EVALUATOR_KEY as KSR_V3_KEY
    _KEY_TO_STRATEGY = {
        KSR_KEY:    KSR_ID,
        KSR_V3_KEY: KSR_V3_ID,
    }
    result = []
    for row in rows:
        (inst_id, evaluator_key, display_name, online, instruments_json,
         attrs_json, ps_fingerprint, config_rev, execution_mode, execution_mode_revision) = row
        strategy_id = _KEY_TO_STRATEGY.get(evaluator_key)
        if strategy_id is None:
            # Unknown evaluator key — skip; do not invent an adapter.
            continue
        instruments = []
        if instruments_json:
            raw = instruments_json if isinstance(instruments_json, list) else []
            instruments = [str(x.get("canonical_instrument") or x) for x in raw if x]
        result.append({
            "instance_id": str((attrs_json or {}).get("instance_id") or inst_id),
            "strategy_id": strategy_id,
            "display_name": display_name,
            "enabled": bool(online),
            "active_instruments": instruments,
            "parameter_set_fingerprint": ps_fingerprint,
            "configuration_revision": config_rev,
            "execution_mode": str(execution_mode or "OFF"),
            "execution_mode_revision": int(execution_mode_revision or 0),
            "_source": "strategy_instance_v2",  # provenance marker
        })
    return result


def refresh_lifecycle(config: dict[str, Any], *, connect_fn: Any = None) -> dict[str, Any]:
    """Re-read lifecycle state for the next orchestration cycle - instance ONLINE/OFFLINE and parent
    strategy ACTIVE/SUSPENDED (`enabled`) - so a change takes effect without a restart. Everything
    else in the config stays as loaded at start. Without a database the config is unchanged. A
    failed read keeps the previous state; the full configuration already fails closed at startup,
    and no lifecycle change can be written while the database is unreachable."""
    if connect_fn is None:
        if not _database_configured():
            return config
        from postgres.config import PostgresConfig
        from postgres.db import connect
        connect_fn = lambda: connect(PostgresConfig.from_env(), readonly=True)  # noqa: E731
    try:
        with connect_fn() as conn:
            instances = load_instances_from_database(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT strategy_id, enabled FROM platform.strategy_definition")
                enabled = {sid: bool(on) for sid, on in cur.fetchall()}
        strategies = [{**s, "enabled": enabled.get(s["strategy_id"], s.get("enabled"))}
                      for s in config.get("strategies", [])]
        return {**config, "strategies": strategies, "instances": instances}
    except Exception as exc:  # noqa: BLE001
        print(f"STRATEGY_INSTANCE_REFRESH_UNAVAILABLE {type(exc).__name__}: {exc}")
        return config


def _json(value: Any) -> Any:
    # psycopg already decodes jsonb, including jsonb strings (e.g. "SHADOW" -> str); never re-parse.
    return value


def load_config() -> dict[str, Any]:
    """The orchestration configuration comes from PostgreSQL; there is no platform.json.

    With a database configured (production), a missing schema or an un-imported configuration
    raises PlatformConfigUnavailable: the orchestrator refuses to start rather than run on
    defaults. Without any database configured (local runs, unit tests) DEFAULT_CONFIG applies."""
    if not _database_configured():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    from postgres.config import PostgresConfig
    from postgres.db import connect
    try:
        with connect(PostgresConfig.from_env(), readonly=True) as conn:
            return load_config_from_database(conn)
    except PlatformConfigUnavailable:
        raise
    except Exception as exc:
        raise PlatformConfigUnavailable(f"orchestration configuration unavailable: {exc}") from exc
