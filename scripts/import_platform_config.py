"""One-time import of a platform.json (e.g. the running ConfigMap) into the database, after which
nothing reads the file.

    kubectl -n trading get cm mt5-native-bridge-safe-platform-config -o jsonpath='{.data.platform\\.json}' > platform.json
    python -m scripts.import_platform_config platform.json            # dry run: shows what would change
    python -m scripts.import_platform_config platform.json --apply    # writes

Targets: platform.runtime_setting (every top-level scalar/object key), orchestration_account,
orchestration_portfolio (029), strategy_definition (028), strategy_instance (030; unknown keys
kept in `attributes`) and instrument_provider_mapping (027, from `symbol_mappings`). Idempotent:
rows only change (and bump their revision) when their content differs. Existing display_name /
description / default_instance_id of a strategy are preserved. Nothing is deleted.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from postgres.config import PostgresConfig  # noqa: E402
from postgres.db import connect  # noqa: E402

LIST_KEYS = ("accounts", "portfolios", "strategies", "instances", "symbol_mappings")
ACCOUNT_COLUMNS = ("account_id", "broker", "broker_environment", "broker_account_reference", "currency", "enabled", "execution_mode")
PORTFOLIO_COLUMNS = ("portfolio_id", "name", "enabled", "base_currency", "sizing_policy_id", "account_ids", "strategy_ids")
STRATEGY_COLUMNS = ("strategy_id", "strategy_version", "enabled", "adapter", "routes", "trade_management")
INSTANCE_COLUMNS = ("instance_id", "strategy_id", "display_name", "enabled")
METALS = {"XAU", "XAG", "XPT", "XPD"}
COINS = {"BTC", "ETH", "LTC", "XRP", "BCH", "ADA", "DOT", "SOL", "DOGE", "LINK", "XLM", "UNI", "AVAX", "MATIC", "TRX",
         "BNB", "ATOM", "XTZ", "EOS", "ETC", "FIL", "AAVE", "SHIB", "TON", "NEAR", "APT", "ARB", "OP", "SUI"}


def asset_class(canonical: str) -> str:
    base = canonical[:3]
    if base in METALS:
        return "METAL"
    if any(canonical.startswith(c) for c in COINS):
        return "CRYPTO"
    return "FX" if len(canonical) == 6 and canonical.isalpha() else "OTHER"


def _upsert(cur: Any, sql: str, params: tuple[Any, ...]) -> bool:
    cur.execute(sql, params)
    return cur.fetchone() is not None


def import_config(config: dict[str, Any], *, apply: bool, actor: str = "import_platform_config",
                  connect_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    settings = {k: v for k, v in config.items() if k not in LIST_KEYS}
    plan = {"settings": sorted(settings), "accounts": [a["account_id"] for a in config.get("accounts", [])],
            "portfolios": [p["portfolio_id"] for p in config.get("portfolios", [])],
            "strategies": [s["strategy_id"] for s in config.get("strategies", [])],
            "instances": [i["instance_id"] for i in config.get("instances", [])],
            "symbol_mappings": sorted((config.get("symbol_mappings") or {}).keys()), "dry_run": not apply}
    if not apply:
        return plan
    changed: dict[str, list[str]] = {k: [] for k in ("settings", "accounts", "portfolios", "strategies", "instances", "symbol_mappings")}
    connect_fn = connect_fn or (lambda: connect(PostgresConfig.from_env()))
    with connect_fn() as conn:
        with conn.cursor() as cur:
            for key, value in sorted(settings.items()):
                if _upsert(cur, """INSERT INTO platform.runtime_setting (key, value, updated_by) VALUES (%s, %s::jsonb, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by,
                            revision = platform.runtime_setting.revision + 1, updated_at = now()
                        WHERE platform.runtime_setting.value IS DISTINCT FROM EXCLUDED.value RETURNING key""",
                           (key, json.dumps(value), actor)):
                    changed["settings"].append(key)
            for a in config.get("accounts", []):
                attrs = {k: v for k, v in a.items() if k not in ACCOUNT_COLUMNS}
                if _upsert(cur, """INSERT INTO platform.orchestration_account (account_id, broker, broker_environment,
                            broker_account_reference, currency, enabled, execution_mode, attributes, updated_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                        ON CONFLICT (account_id) DO UPDATE SET broker = EXCLUDED.broker,
                            broker_environment = EXCLUDED.broker_environment,
                            broker_account_reference = EXCLUDED.broker_account_reference, currency = EXCLUDED.currency,
                            enabled = EXCLUDED.enabled, execution_mode = EXCLUDED.execution_mode,
                            attributes = EXCLUDED.attributes, updated_by = EXCLUDED.updated_by,
                            revision = platform.orchestration_account.revision + 1, updated_at = now()
                        WHERE (platform.orchestration_account.broker, platform.orchestration_account.broker_environment,
                               platform.orchestration_account.broker_account_reference, platform.orchestration_account.currency,
                               platform.orchestration_account.enabled, platform.orchestration_account.execution_mode,
                               platform.orchestration_account.attributes)
                              IS DISTINCT FROM (EXCLUDED.broker, EXCLUDED.broker_environment, EXCLUDED.broker_account_reference,
                                                EXCLUDED.currency, EXCLUDED.enabled, EXCLUDED.execution_mode, EXCLUDED.attributes)
                        RETURNING account_id""",
                           (a["account_id"], a.get("broker", ""), a.get("broker_environment", ""),
                            a.get("broker_account_reference"), a.get("currency", "USD"), bool(a.get("enabled")),
                            a.get("execution_mode", "SHADOW"), json.dumps(attrs), actor)):
                    changed["accounts"].append(a["account_id"])
            for p in config.get("portfolios", []):
                attrs = {k: v for k, v in p.items() if k not in PORTFOLIO_COLUMNS}
                if _upsert(cur, """INSERT INTO platform.orchestration_portfolio (portfolio_id, name, enabled, base_currency,
                            sizing_policy_id, account_ids, strategy_ids, attributes, updated_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                        ON CONFLICT (portfolio_id) DO UPDATE SET name = EXCLUDED.name, enabled = EXCLUDED.enabled,
                            base_currency = EXCLUDED.base_currency, sizing_policy_id = EXCLUDED.sizing_policy_id,
                            account_ids = EXCLUDED.account_ids, strategy_ids = EXCLUDED.strategy_ids,
                            attributes = EXCLUDED.attributes, updated_by = EXCLUDED.updated_by,
                            revision = platform.orchestration_portfolio.revision + 1, updated_at = now()
                        WHERE (platform.orchestration_portfolio.name, platform.orchestration_portfolio.enabled,
                               platform.orchestration_portfolio.base_currency, platform.orchestration_portfolio.sizing_policy_id,
                               platform.orchestration_portfolio.account_ids, platform.orchestration_portfolio.strategy_ids,
                               platform.orchestration_portfolio.attributes)
                              IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.enabled, EXCLUDED.base_currency, EXCLUDED.sizing_policy_id,
                                                EXCLUDED.account_ids, EXCLUDED.strategy_ids, EXCLUDED.attributes)
                        RETURNING portfolio_id""",
                           (p["portfolio_id"], p.get("name", p["portfolio_id"]), bool(p.get("enabled")),
                            p.get("base_currency", "USD"), p.get("sizing_policy_id"), list(p.get("account_ids", [])),
                            list(p.get("strategy_ids", [])), json.dumps(attrs), actor)):
                    changed["portfolios"].append(p["portfolio_id"])
            for s in config.get("strategies", []):
                attrs = {k: v for k, v in s.items() if k not in STRATEGY_COLUMNS}
                tm = s.get("trade_management")
                if _upsert(cur, """INSERT INTO platform.strategy_definition (strategy_id, strategy_version, display_name,
                            adapter, enabled, routes, trade_management, attributes, updated_by)
                        VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s)
                        ON CONFLICT (strategy_id) DO UPDATE SET strategy_version = EXCLUDED.strategy_version,
                            adapter = EXCLUDED.adapter, enabled = EXCLUDED.enabled, routes = EXCLUDED.routes,
                            trade_management = EXCLUDED.trade_management, attributes = EXCLUDED.attributes,
                            updated_by = EXCLUDED.updated_by,
                            revision = platform.strategy_definition.revision + 1, updated_at = now()
                        WHERE (platform.strategy_definition.strategy_version, platform.strategy_definition.adapter,
                               platform.strategy_definition.enabled, platform.strategy_definition.routes,
                               platform.strategy_definition.trade_management, platform.strategy_definition.attributes)
                              IS DISTINCT FROM (EXCLUDED.strategy_version, EXCLUDED.adapter, EXCLUDED.enabled,
                                                EXCLUDED.routes, EXCLUDED.trade_management, EXCLUDED.attributes)
                        RETURNING strategy_id""",
                           (s["strategy_id"], s.get("strategy_version", "V1"),
                            s["strategy_id"].replace("_", " ").title(), s.get("adapter"), bool(s.get("enabled")),
                            json.dumps(s.get("routes") or {}), None if tm is None else json.dumps(tm),
                            json.dumps(attrs), actor)):
                    changed["strategies"].append(s["strategy_id"])
            for instance in config.get("instances", []):
                attrs = {k: v for k, v in instance.items() if k not in INSTANCE_COLUMNS}
                if _upsert(cur, """INSERT INTO platform.strategy_instance
                            (instance_id, strategy_id, display_name, enabled, attributes, updated_by)
                        VALUES (%s,%s,%s,%s,%s::jsonb,%s)
                        ON CONFLICT (instance_id) DO UPDATE SET strategy_id = EXCLUDED.strategy_id,
                            display_name = EXCLUDED.display_name, enabled = EXCLUDED.enabled,
                            attributes = EXCLUDED.attributes, updated_by = EXCLUDED.updated_by,
                            revision = platform.strategy_instance.revision + 1, updated_at = now()
                        WHERE (platform.strategy_instance.strategy_id, platform.strategy_instance.display_name,
                               platform.strategy_instance.enabled, platform.strategy_instance.attributes)
                              IS DISTINCT FROM (EXCLUDED.strategy_id, EXCLUDED.display_name,
                                                EXCLUDED.enabled, EXCLUDED.attributes)
                        RETURNING instance_id""",
                           (instance["instance_id"], instance["strategy_id"],
                            instance.get("display_name", instance["instance_id"]), bool(instance.get("enabled")),
                            json.dumps(attrs), actor)):
                    changed["instances"].append(instance["instance_id"])
            for canonical, symbol in sorted((config.get("symbol_mappings") or {}).items()):
                canonical = canonical.upper()
                if _upsert(cur, """INSERT INTO platform.instrument_provider_mapping
                            (provider, canonical_instrument, provider_symbol, asset_class, display_name, updated_by)
                        VALUES ('MT5', %s, %s, %s, %s, %s)
                        ON CONFLICT (provider, canonical_instrument) DO UPDATE SET provider_symbol = EXCLUDED.provider_symbol,
                            revision = platform.instrument_provider_mapping.revision + 1, updated_at = now(),
                            updated_by = EXCLUDED.updated_by
                        WHERE platform.instrument_provider_mapping.provider_symbol IS DISTINCT FROM EXCLUDED.provider_symbol
                        RETURNING canonical_instrument""",
                           (canonical, symbol, asset_class(canonical), canonical, actor)):
                    changed["symbol_mappings"].append(canonical)
        conn.commit()
    return {"changed": changed, "dry_run": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path)
    parser.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = parser.parse_args(argv)
    config = json.loads(args.input.read_text(encoding="utf-8"))
    print(json.dumps(import_config(config, apply=args.apply), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
