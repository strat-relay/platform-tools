from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MarketEvent:
    canonical_instrument: str
    timeframe: str
    open_timestamp: int
    close_timestamp: int
    open: float
    high: float
    low: float
    close: float
    completed: bool = True
    source: str = "historical"
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.canonical_instrument or not self.timeframe:
            raise ValueError("instrument and timeframe are required")
        if self.close_timestamp <= self.open_timestamp:
            raise ValueError("close_timestamp must be after open_timestamp")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC is inconsistent")

    def identity_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SetupLifecycleEvent:
    setup_id: str
    strategy_version_id: str
    canonical_instrument: str
    status: str
    event_timestamp: int
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntrySignal:
    signal_id: str
    strategy_version_id: str
    canonical_instrument: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    decision_timestamp: int
    order_type: str = "MARKET"
    expiry_timestamp: int | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        if self.order_type not in {"MARKET", "LIMIT", "STOP"}:
            raise ValueError("order_type must be MARKET, LIMIT, or STOP")
        if self.direction == "LONG" and not (self.stop_price < self.entry_price < self.target_price):
            raise ValueError("LONG signal requires stop < entry < target")
        if self.direction == "SHORT" and not (self.target_price < self.entry_price < self.stop_price):
            raise ValueError("SHORT signal requires target < entry < stop")

    def identity_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EntrySignalOutcome:
    signal_id: str
    status: str
    exit_timestamp: int
    exit_price: float
    realized_r: float
    reason: str
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParameterSchema:
    schema_id: str
    fields: Mapping[str, Mapping[str, Any]]

    def validate(self, values: Mapping[str, Any]) -> None:
        unknown = set(values) - set(self.fields)
        if unknown:
            raise ValueError(f"unknown parameter(s): {sorted(unknown)}")
        for name, spec in self.fields.items():
            if spec.get("required", False) and name not in values and "default" not in spec:
                raise ValueError(f"missing required parameter: {name}")
            if name not in values:
                continue
            value = values[name]
            if "enum" in spec and value not in spec["enum"]:
                raise ValueError(f"invalid value for {name}")
            if "minimum" in spec and value < spec["minimum"]:
                raise ValueError(f"{name} below minimum")
            if "maximum" in spec and value > spec["maximum"]:
                raise ValueError(f"{name} above maximum")


@dataclass(frozen=True)
class ParameterSet:
    parameter_set_id: str
    strategy_version_id: str
    schema_id: str
    values: Mapping[str, Any]
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.parameter_set_id or not self.strategy_version_id:
            raise ValueError("parameter_set_id and strategy_version_id are required")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "parameter_set_id": self.parameter_set_id,
            "strategy_version_id": self.strategy_version_id,
            "schema_id": self.schema_id,
            "values": dict(self.values),
            "provenance": dict(self.provenance),
        }

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.canonical_payload())


@dataclass(frozen=True)
class StrategyVersion:
    strategy_id: str
    version: str
    evaluator_key: str
    parameter_schema: ParameterSchema
    lifecycle: str = "DRAFT"

    @property
    def strategy_version_id(self) -> str:
        return f"{self.strategy_id}@{self.version}"

    def validate_parameter_set(self, parameter_set: ParameterSet) -> None:
        if parameter_set.strategy_version_id != self.strategy_version_id:
            raise ValueError("parameter set belongs to a different StrategyVersion")
        if parameter_set.schema_id != self.parameter_schema.schema_id:
            raise ValueError("parameter set schema does not match StrategyVersion")
        self.parameter_schema.validate(parameter_set.values)


@dataclass(frozen=True)
class CostModel:
    model_id: str
    spread_price: float = 0.0
    commission_r: float = 0.0
    approximation: str = "explicit deterministic model"

    @property
    def fingerprint(self) -> str:
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class BacktestRun:
    run_id: str
    strategy_version_id: str
    evaluator_fingerprint: str
    parameter_set_fingerprint: str
    instruments: tuple[str, ...]
    timeframes: tuple[str, ...]
    dataset_fingerprint: str
    requested_start: int
    requested_end: int
    partition: str
    cost_model_fingerprint: str
    engine_version: str
    status: str = "QUEUED"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    result_fingerprint: str | None = None

    def transition(self, status: str, *, result_fingerprint: str | None = None) -> "BacktestRun":
        if status not in {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError(f"invalid BacktestRun status: {status}")
        return replace(self, status=status, result_fingerprint=result_fingerprint or self.result_fingerprint)
