from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from orchestration.models import stable_id


@dataclass(frozen=True)
class ExecutionIntent:
    execution_intent_id: str
    schema_version: str
    signal_id: str
    strategy_id: str
    strategy_version: str
    strategy_instance_id: str
    portfolio_id: str
    account_id: str
    broker: str
    economic_position_id: str | None
    entry_opportunity_id: str | None
    canonical_symbol: str
    broker_symbol: str
    direction: str
    order_type: str
    strategy_entry_price: float
    strategy_stop_price: float
    strategy_target_price: float
    approved_volume: float
    desired_risk_amount: float
    estimated_actual_risk: float | None
    risk_fraction: float
    account_snapshot_id: str
    sizing_decision_id: str
    signal_timestamp: str
    signal_emitted_at: str
    sizing_timestamp: str
    execution_intent_created_at: str
    intent_created_at: str
    entry_condition_met_at: str
    quote_requested_at: str | None
    quote_received_at: str | None
    order_submitted_at: str | None
    expires_at: str | None
    correlation_id: str
    causation_id: str
    provenance: dict[str, Any] = field(default_factory=dict)
    risk_policy_version: int | None = None
    virtual_equity_usd: float | None = None
    risk_percent: float | None = None
    risk_budget_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_records(cls, signal: dict[str, Any], sizing: dict[str, Any], portfolio: dict[str, Any], account: dict[str, Any], intent_max_age_seconds: float = 5.0):
        identity = {
            "signal_id": signal["signal_id"],
            "sizing_decision_id": sizing["sizing_decision_id"],
            "portfolio_id": portfolio["portfolio_id"],
            "account_id": account["account_id"],
        }
        intent_id = stable_id("EXECINT", identity)
        now = datetime.now(timezone.utc).isoformat()
        entry_condition_met_at = signal.get("entry_condition_met_at") or signal.get("signal_timestamp")
        expires_at = datetime.fromtimestamp(
            datetime.fromisoformat(now.replace("Z", "+00:00")).timestamp() + float(intent_max_age_seconds),
            timezone.utc,
        ).isoformat()
        return cls(
            execution_intent_id=intent_id,
            schema_version="execution-intent-v1",
            signal_id=signal["signal_id"], strategy_id=signal["strategy_id"],
            strategy_version=signal["strategy_version"], strategy_instance_id=signal.get("strategy_instance_id", ""),
            portfolio_id=portfolio["portfolio_id"], account_id=account["account_id"], broker=account.get("broker", ""),
            economic_position_id=signal.get("economic_position_id"), entry_opportunity_id=signal.get("entry_opportunity_id"),
            canonical_symbol=signal.get("canonical_symbol", signal.get("symbol", "")),
            broker_symbol=signal.get("broker_symbol_hint", signal.get("symbol", "")), direction=signal["direction"],
            order_type="MARKET", strategy_entry_price=float(sizing["entry"]),
            strategy_stop_price=float(sizing["stop"]), strategy_target_price=float(sizing["target"]),
            approved_volume=float(sizing["rounded_volume"]), desired_risk_amount=float(sizing["desired_risk_amount"] or 0),
            estimated_actual_risk=sizing.get("estimated_loss_at_stop"), risk_fraction=float(sizing["desired_risk_fraction"]),
            account_snapshot_id=sizing["account_snapshot_id"], sizing_decision_id=sizing["sizing_decision_id"],
            signal_timestamp=signal["signal_timestamp"],
            # signal_timestamp is EVENT_TIME (the market/setup candle).  Live
            # freshness must use the explicit wall-clock emission time.
            signal_emitted_at=signal.get("signal_emitted_at") or "",
            sizing_timestamp=sizing["created_at"], execution_intent_created_at=now, intent_created_at=now,
            entry_condition_met_at=entry_condition_met_at,
            quote_requested_at=None, quote_received_at=None, order_submitted_at=None,
            expires_at=expires_at,
            correlation_id=intent_id, causation_id=sizing["sizing_decision_id"],
            provenance={"source": "orchestration.sizing_decisions", "classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL"},
            risk_policy_version=sizing.get("risk_policy_version"),
            virtual_equity_usd=sizing.get("virtual_equity_usd"),
            risk_percent=sizing.get("risk_percent"),
            risk_budget_usd=sizing.get("risk_budget_usd"),
        )


@dataclass(frozen=True)
class ExecutionMarketSnapshot:
    market_snapshot_id: str
    timestamp: str
    broker_symbol: str
    bid: float | None
    ask: float | None
    spread_price: float | None
    spread_ticks: float | None
    strategy_entry: float
    current_executable_price: float | None
    entry_drift_price: float | None
    entry_drift_ticks: float | None
    entry_drift_as_r: float | None
    metadata_reference: str | None

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class ExecutionDecision:
    execution_decision_id: str
    execution_intent_id: str
    signal_id: str
    strategy_id: str
    strategy_version: str
    portfolio_id: str
    account_id: str
    sizing_account_snapshot_id: str
    execution_account_snapshot_id: str | None
    market_snapshot_id: str | None
    approved_volume: float
    decision: str
    reason: str
    created_at: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self): return asdict(self)
