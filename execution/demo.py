"""Fail-closed demo-execution controls and virtual-bankroll accounting."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestration.models import stable_id


DEMO_CONTEXT = "5056045369@MetaQuotes-Demo"
DEMO_SERVER = "MetaQuotes-Demo"
REAL_SERVER_PREFIX = "Exness-"
VIRTUAL_STARTING_EQUITY = 200.0
RISK_FRACTION = 0.01
RISK_BUDGET = 2.0

MAPPINGS = {
    "XAUUSDm": {"broker_symbol": "XAUUSD", "status": "UNSAFE", "policy": "REJECT_CROSS_FEED"},
    "USDJPYm": {"broker_symbol": "USDJPY", "status": "UNSAFE", "policy": "REJECT_CROSS_FEED"},
    "EURUSDm": {"broker_symbol": "EURUSD", "status": "UNSAFE", "policy": "REJECT_CROSS_FEED"},
    "BTCUSDm": {"broker_symbol": None, "status": "UNAVAILABLE", "policy": "REJECT_CROSS_FEED"},
}

REAL_MAPPINGS = {
    "XAUUSDm": {"broker_symbol": "XAUUSDm", "status": "NATIVE", "policy": "DIRECT_NATIVE"},
    "BTCUSDm": {"broker_symbol": "BTCUSDm", "status": "NATIVE", "policy": "DIRECT_NATIVE"},
    "USDJPYm": {"broker_symbol": "USDJPYm", "status": "NATIVE", "policy": "DIRECT_NATIVE"},
    "EURUSDm": {"broker_symbol": "EURUSDm", "status": "NATIVE", "policy": "DIRECT_NATIVE"},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mode": "DRY_RUN", "armed": False, "armed_at": None,
                "account_context_id": None, "virtual_equity": VIRTUAL_STARTING_EQUITY,
                "realized_pnl": 0.0, "starting_equity": VIRTUAL_STARTING_EQUITY}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def account_is_authorized(snapshot: dict[str, Any]) -> tuple[bool, str]:
    context = snapshot.get("account_context_id")
    raw = snapshot.get("raw") or {}
    if context != DEMO_CONTEXT:
        return False, "DEMO_ACCOUNT_CONTEXT_MISMATCH"
    if raw.get("server") != DEMO_SERVER:
        return False, "DEMO_SERVER_MISMATCH"
    # MT5 ACCOUNT_TRADE_MODE: 0 is demo in the connected terminal used here.
    if raw.get("type") not in (0, "0"):
        return False, "ACCOUNT_NOT_POSITIVELY_DEMO"
    if snapshot.get("freshness_state") != "FRESH":
        return False, "ACCOUNT_SNAPSHOT_STALE"
    return True, "DEMO_ACCOUNT_VERIFIED"


def mapping_for(strategy_symbol: str) -> dict[str, Any]:
    return dict(MAPPINGS.get(strategy_symbol, {"broker_symbol": None, "status": "UNAVAILABLE", "policy": "REJECT_CROSS_FEED"}))


def real_mapping_for(strategy_symbol: str) -> dict[str, Any]:
    return dict(REAL_MAPPINGS.get(strategy_symbol, {"broker_symbol": None, "status": "UNAVAILABLE", "policy": "DIRECT_NATIVE"}))


def real_account_is_authorized(snapshot: dict[str, Any], expected_context: str | None = None) -> tuple[bool, str]:
    raw = snapshot.get("raw") or {}
    context = expected_context or snapshot.get("account_context_id")
    if not context or snapshot.get("account_context_id") != context:
        return False, "REAL_ACCOUNT_CONTEXT_MISMATCH"
    if not str(raw.get("server", "")).startswith(REAL_SERVER_PREFIX):
        return False, "REAL_BROKER_SERVER_MISMATCH"
    if raw.get("type") not in (2, "2"):
        return False, "ACCOUNT_NOT_POSITIVELY_REAL"
    if snapshot.get("freshness_state") != "FRESH":
        return False, "ACCOUNT_SNAPSHOT_STALE"
    return True, "REAL_ACCOUNT_VERIFIED"


def virtual_size(*, entry: float, stop: float, metadata: dict[str, Any], virtual_equity: float,
                 risk_fraction: float = RISK_FRACTION) -> dict[str, Any]:
    risk_distance = abs(float(entry) - float(stop))
    tick_size = metadata.get("tick_size")
    tick_value = metadata.get("tick_value")
    minimum = metadata.get("volume_min")
    maximum = metadata.get("volume_max")
    step = metadata.get("volume_step")
    desired = float(virtual_equity) * float(risk_fraction)
    if risk_distance <= 0 or not all(x is not None and float(x) > 0 for x in (tick_size, tick_value, minimum, maximum, step)):
        return {"decision": "SKIP", "reason": "MISSING_OR_INVALID_SYMBOL_ECONOMICS", "desired_risk_amount": desired}
    loss_per_lot = risk_distance / float(tick_size) * float(tick_value)
    raw = desired / loss_per_lot if loss_per_lot > 0 else 0.0
    rounded = math.floor(raw / float(step)) * float(step)
    actual = rounded * loss_per_lot
    if rounded < float(minimum):
        return {"decision": "SKIP", "reason": "BELOW_MINIMUM_VOLUME_FOR_RISK_BUDGET", "desired_risk_amount": desired,
                "raw_volume": raw, "rounded_volume": rounded, "actual_risk": actual, "risk_fraction": float(risk_fraction)}
    if rounded > float(maximum):
        return {"decision": "SKIP", "reason": "ABOVE_MAXIMUM_VOLUME", "desired_risk_amount": desired,
                "raw_volume": raw, "rounded_volume": rounded, "actual_risk": actual, "risk_fraction": float(risk_fraction)}
    return {"decision": "EXECUTABLE", "reason": "VIRTUAL_RISK_SIZE_OK", "desired_risk_amount": desired,
            "raw_volume": raw, "rounded_volume": rounded, "actual_risk": actual,
            "actual_risk_fraction": actual / float(virtual_equity) if virtual_equity else None,
            "risk_fraction": float(risk_fraction)}


def execution_key(signal_id: str, opportunity_id: str | None, account_context: str) -> str:
    return stable_id("DEMOEXEC", {"signal_id": signal_id, "entry_opportunity_id": opportunity_id,
                                  "account_context_id": account_context})


def strategy_symbol_for(signal: dict[str, Any]) -> str:
    """Return the frozen strategy symbol without globally stripping suffixes."""
    return str(signal.get("symbol") or signal.get("strategy_symbol") or
               signal.get("broker_symbol_hint") or signal.get("canonical_symbol") or "")
