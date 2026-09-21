from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


def stable_id(prefix: str, value: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


@dataclass(frozen=True)
class StrategySignal:
    signal_id: str
    schema_version: str
    strategy_id: str
    strategy_version: str
    strategy_instance_id: str
    source_event_id: str
    market_event_id: str | None
    setup_id: str | None
    entry_opportunity_id: str | None
    economic_position_id: str | None
    created_at: str
    signal_timestamp: str
    symbol: str
    canonical_symbol: str
    broker_symbol_hint: str
    direction: str
    entry_type: str
    entry_price: float
    stop_price: float
    target_price: float
    risk_distance: float
    target_distance: float
    target_r: float | None
    timeframe: str
    lower_timeframe: str | None
    higher_timeframes: tuple[str, ...]
    entry_mechanism: tuple[str, ...]
    strategy_metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    # EVENT_TIME remains signal_timestamp.  These wall-clock fields describe
    # live decision/publication timing and are intentionally optional so old
    # persisted rows remain readable but cannot become live-eligible without a
    # trustworthy emission timestamp.
    decision_time: str | None = None
    signal_emitted_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AccountSnapshot:
    snapshot_id: str
    account_id: str
    timestamp: str
    balance: float | None
    equity: float | None
    margin: float | None
    free_margin: float | None
    margin_level: float | None
    currency: str | None
    leverage: float | None


@dataclass(frozen=True)
class SizingDecision:
    sizing_decision_id: str
    signal_id: str
    portfolio_id: str
    account_id: str
    account_snapshot_id: str
    sizing_policy_id: str
    strategy_id: str
    strategy_version: str
    desired_risk_fraction: float
    desired_risk_amount: float | None
    entry: float
    stop: float
    target: float
    raw_volume: float | None
    rounded_volume: float | None
    volume_min: float | None
    volume_max: float | None
    volume_step: float | None
    estimated_loss_at_stop: float | None
    estimated_margin_required: float | None
    decision: str
    reason: str
    created_at: str


def signal_identity_fields(signal: StrategySignal) -> dict[str, Any]:
    return {"strategy_id": signal.strategy_id, "strategy_version": signal.strategy_version,
            "strategy_instance_id": signal.strategy_instance_id,
            "economic_position_id": signal.economic_position_id,
            "entry_opportunity_id": signal.entry_opportunity_id,
            "source_event_id": signal.source_event_id}
