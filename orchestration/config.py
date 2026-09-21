from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "orchestration" / "config" / "platform.json"


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "orchestration-platform-v1",
    "execution_mode": "SHADOW",
    "mcp_url": "http://127.0.0.1:22347/mcp",
    "sizing_scenarios": [0.0025, 0.005, 0.01, 0.02],
    "symbol_mappings": {
        "XAUUSD": "XAUUSDm", "BTCUSD": "BTCUSDm", "USDJPY": "USDJPYm", "EURUSD": "EURUSDm",
    },
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


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return json.loads(json.dumps(DEFAULT_CONFIG))
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
