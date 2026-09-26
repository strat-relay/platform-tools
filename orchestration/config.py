from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "orchestration-platform-v1",
    "execution_mode": "SHADOW",
    "mcp_url": "http://127.0.0.1:22347/mcp",
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
}


CONFIG_SCHEMA_VERSION = "029"
STRATEGY_COLUMNS = ("strategy_id", "strategy_version", "enabled", "adapter", "routes")


class PlatformConfigUnavailable(RuntimeError):
    """The database is configured but the orchestration configuration cannot be loaded from it."""


def _database_configured() -> bool:
    from postgres.config import PostgresConfig
    cfg = PostgresConfig.from_env()
    return bool((cfg.dsn and cfg.dsn.strip()) or cfg.host)


def load_config_from_database(conn: Any) -> dict[str, Any]:
    """Assemble the orchestration configuration (the shape platform.json used to have) from
    platform.runtime_setting, orchestration_account/portfolio (029) and strategy_definition (028)."""
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
    return {**settings, "accounts": accounts, "portfolios": portfolios, "strategies": strategies}


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
