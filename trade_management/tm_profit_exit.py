"""Pure profit-based exit policy evaluation.

The evaluator only decides.  Broker position/cost valuation is supplied by the existing
execution-v2 management boundary when a NET_PROFIT_USD candidate is acted on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

EVALUATOR_ID = "tm-exit-policy.v1"
REASON_TRADE_CLOSED = "TRADE_CLOSED"


@dataclass(frozen=True)
class ExitPolicy:
    time_exit_at: Any = None
    net_profit_target_usd: float | None = None
    profit_target_pips: float | None = None
    profit_target_r: float | None = None


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


def _positive(name: str, value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return result


def evaluate_exit_policy(*, trade_state: str, direction: str, entry_price: float,
                         bid: float | None, ask: float | None, risk_distance: float | None,
                         pip_size: float | None, as_of: Any,
                         policy: ExitPolicy, estimated_net_profit_usd: float | None = None,
                         quote_age_seconds: float | None = None,
                         max_quote_age_seconds: float = 60.0) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    """Evaluate all configured rules with deterministic precedence.

    Precedence is TIME_EXIT, NET_PROFIT_USD, PROFIT_R, PROFIT_PIPS.  All rules that are
    true are retained in ``triggered_rules``; the first rule is the canonical reason.
    Protective broker SL/TP remains outside this evaluator and is never removed.
    """
    if trade_state != "OPEN":
        return "HOLD", (REASON_TRADE_CLOSED,), {}
    direction = str(direction).upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")

    net_target = _positive("net_profit_target_usd", policy.net_profit_target_usd)
    pips_target = _positive("profit_target_pips", policy.profit_target_pips)
    r_target = _positive("profit_target_r", policy.profit_target_r)
    parameters: dict[str, Any] = {
        "exit_policy": {
            "time_exit_at": _utc(policy.time_exit_at).isoformat() if policy.time_exit_at is not None else None,
            "net_profit_target_usd": net_target,
            "profit_target_pips": pips_target,
            "profit_target_r": r_target,
            "precedence": ["TIME_EXIT", "NET_PROFIT_USD", "PROFIT_R", "PROFIT_PIPS"],
        },
        "observed_bid": bid, "observed_ask": ask,
        "observed_quote": {"bid": bid, "ask": ask},
        "quote_age_seconds": quote_age_seconds,
    }
    if quote_age_seconds is not None and quote_age_seconds > max_quote_age_seconds:
        return "HOLD", ("QUOTE_STALE",), parameters

    triggered: list[tuple[str, str]] = []
    if policy.time_exit_at is not None and _utc(as_of) >= _utc(policy.time_exit_at):
        triggered.append(("TIME_EXIT", "TIME_EXIT_DUE"))

    executable = None
    if bid is not None and ask is not None:
        executable = float(bid) if direction == "LONG" else float(ask)
    movement = None if executable is None else ((executable - float(entry_price)) if direction == "LONG"
                                                else (float(entry_price) - executable))
    if net_target is not None:
        parameters["net_profit_target_usd"] = net_target
        if estimated_net_profit_usd is not None:
            parameters["estimated_net_profit_usd"] = float(estimated_net_profit_usd)
            if float(estimated_net_profit_usd) >= net_target:
                triggered.append(("NET_PROFIT_USD", "NET_PROFIT_TARGET_REACHED"))
        else:
            # The execution boundary must value this rule with broker position/spec/cost facts.
            # An EXIT candidate is safe because that boundary fails closed until the target is met.
            parameters["net_profit_evaluation_required"] = True

    if pips_target is not None:
        if pip_size is None or float(pip_size) <= 0:
            parameters["profit_pips_unavailable"] = True
            parameters["profit_pips_evaluation_required"] = True
        elif movement is not None and movement / float(pip_size) + 1e-9 >= pips_target:
            parameters["observed_profit_pips"] = movement / float(pip_size)
            triggered.append(("PROFIT_PIPS", "PROFIT_PIPS_TARGET_REACHED"))

    if r_target is not None:
        if risk_distance is None or float(risk_distance) <= 0:
            parameters["profit_r_unavailable"] = True
        elif movement is not None and movement / float(risk_distance) + 1e-9 >= r_target:
            parameters["observed_profit_r"] = movement / float(risk_distance)
            triggered.append(("PROFIT_R", "PROFIT_R_TARGET_REACHED"))

    if not triggered and not parameters.get("net_profit_evaluation_required") and not parameters.get("profit_pips_evaluation_required"):
        return "HOLD", ("EXIT_POLICY_NOT_TRIGGERED",), parameters
    ordered = [name for name in ("TIME_EXIT", "NET_PROFIT_USD", "PROFIT_R", "PROFIT_PIPS")
               if any(trigger == name for trigger, _ in triggered)]
    if not ordered and parameters.get("net_profit_evaluation_required"):
        ordered = ["NET_PROFIT_USD"]
        parameters["exit_candidate"] = True
    if not ordered and parameters.get("profit_pips_evaluation_required"):
        ordered = ["PROFIT_PIPS"]
        parameters["exit_candidate"] = True
    parameters["triggered_rules"] = ordered
    parameters["exit_reason"] = ordered[0]
    parameters["exit_trigger_reason"] = next((reason for trigger, reason in triggered if trigger == ordered[0]),
                                              "NET_PROFIT_EVALUATION_REQUIRED" if ordered[0] == "NET_PROFIT_USD"
                                              else "PROFIT_PIPS_EVALUATION_REQUIRED")
    return "EXIT", tuple(reason for _, reason in triggered) or (("NET_PROFIT_EVALUATION_REQUIRED",)
                                                                  if ordered[0] == "NET_PROFIT_USD"
                                                                  else ("PROFIT_PIPS_EVALUATION_REQUIRED",)), parameters
