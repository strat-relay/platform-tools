"""Research-only intraday strategy variant definitions.

This module is an additive catalog.  It does not publish to PostgreSQL, create
database instances, change parent strategies, or register live runners.

The definitions intentionally use the generic ``StrategyVersion`` and
``ParameterSet`` contracts, while recording that the parent implementations
still need adapters before discovery backtests can run through the same
historical/live evaluator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from strategy_backtest.models import ParameterSchema, ParameterSet, StrategyVersion


@dataclass(frozen=True)
class ParentContract:
    strategy_id: str
    implementation: str
    context_timeframe: tuple[str, ...]
    setup_timeframe: str
    confirmation_timeframe: str
    entry_timeframe: str
    entry_rule: str
    stop_rule: str
    target_rule: str
    target_r: float | None
    max_hold: str
    invalidation_rule: str
    reentry_rule: str
    outcome_rules: str
    parameter_set: str
    instrument_membership: tuple[str, ...]
    semantics: dict[str, str]


PARENT_CONTRACTS = {
    "CONTEXT_STRUCTURE_RETRACE_V1": ParentContract(
        strategy_id="CONTEXT_STRUCTURE_RETRACE_V1",
        implementation="context_structure_retrace_forward.py + context_structure_retrace/",
        context_timeframe=("H1", "H4"), setup_timeframe="H1",
        confirmation_timeframe="M15", entry_timeframe="M15",
        entry_rule="structural retracement with completed lower-timeframe confirmation; paper entry at executable M15 level",
        stop_rule="ORIGINATING_SETUP_EXTREME with minimal causal volatility/spread safety buffer",
        target_rule="STRUCTURE_CAPPED_EXTENSION; setup extreme plus 0.50*setup range, capped by directionally valid opposing structure",
        target_r=None, max_hold="NONE",
        invalidation_rule="target behind/at executable entry; leave-zone and thesis invalidation rules in frozen manifest",
        reentry_rule="INTERACT -> LEAVE_ZONE -> COMPLETED_LOWER_TF_CLOSE_OUTSIDE -> RETURN -> THESIS_VALID -> TARGET_NOT_COMPLETED",
        outcome_rules="completed M15 bar; stop precedence when stop and target are touched in one bar; target/stop realized in R",
        parameter_set="context-v1-frozen",
        instrument_membership=("XAUUSD", "BTCUSD", "USDJPY", "EURUSD"),
        semantics={"thesis": "THESIS_OWNED", "timeframes": "TIMEFRAME_OWNED", "stop_target": "THESIS_OWNED", "max_hold": "OPERATIONAL"},
    ),
    "LIQUIDITY_DISPLACEMENT_SCALP_V1": ParentContract(
        strategy_id="LIQUIDITY_DISPLACEMENT_SCALP_V1",
        implementation="liquidity_displacement.py + orchestration/liquidity_live.py",
        context_timeframe=("M15",), setup_timeframe="M15",
        confirmation_timeframe="M5", entry_timeframe="M5",
        entry_rule="sweep -> reclaim -> displacement -> micro structure shift -> frozen displacement retracement",
        stop_rule="sweep extreme plus max(0.10 ATR, 1.25x spread, broker stop minimum)",
        target_rule="fixed initial-risk multiple in the parent live adapter",
        target_r=1.25, max_hold="120 minutes",
        invalidation_rule="no retracement within max_retrace_candles; setup invalidation from parent strategy",
        reentry_rule="single frozen retracement opportunity per setup",
        outcome_rules="parent paper/live adapter outcome projection; not a generic backtest contract yet",
        parameter_set="liquidity-v1-* explicit instance sets",
        instrument_membership=("XAUUSD", "BTCUSD", "USDJPY"),
        semantics={"thesis": "THESIS_OWNED", "timeframes": "TIMEFRAME_OWNED", "target_r": "PARAMETERIZED_SCALP_HORIZON", "max_hold": "PARAMETERIZED_SCALP_HORIZON"},
    ),
}


INTRADAY_SCHEMA = ParameterSchema(
    schema_id="intraday-time-horizon-variant-v1",
    fields={
        "context_timeframe": {"required": True, "type": "string"},
        "setup_timeframe": {"required": True, "type": "string"},
        "confirmation_timeframe": {"required": True, "type": "string"},
        "entry_timeframe": {"required": True, "type": "string"},
        "max_hold_minutes": {"required": True, "type": "integer", "minimum": 1},
        "session_boundary": {"required": True, "type": "string", "enum": ["UTC_DAY"]},
        "stop_semantics": {"required": True, "type": "string"},
        "target_semantics": {"required": True, "type": "string"},
        "thesis_steps": {"required": True, "type": "array"},
        "parent_strategy_id": {"required": True, "type": "string"},
        "instrument_membership_source": {"required": True, "type": "string"},
        "execution_eligible": {"required": True, "type": "boolean", "enum": [False]},
    },
)


@dataclass(frozen=True)
class ResearchInstance:
    instance_id: str
    strategy_id: str
    lifecycle_state: str
    enabled: bool
    execution_eligible: bool
    instruments: tuple[str, ...]
    parameter_set_id: str


@dataclass(frozen=True)
class IntradayVariant:
    strategy: StrategyVersion
    parameter_set: ParameterSet
    instance: ResearchInstance
    parent_strategy_id: str
    derivation_type: str
    evaluator_key: str
    evaluator_adapter_status: str
    same_evaluator_contract: str

    def payload(self) -> dict[str, Any]:
        return {
            "strategy": asdict(self.strategy),
            "parameter_set": self.parameter_set.canonical_payload(),
            "parameter_set_fingerprint": self.parameter_set.fingerprint,
            "instance": asdict(self.instance),
            "parent_strategy_id": self.parent_strategy_id,
            "derivation_type": self.derivation_type,
            "evaluator_key": self.evaluator_key,
            "evaluator_adapter_status": self.evaluator_adapter_status,
            "same_evaluator_contract": self.same_evaluator_contract,
        }


def _variant(strategy_id: str, instance_id: str, parameter_set_id: str, schema: dict[str, Any], *, evaluator_key: str) -> IntradayVariant:
    values = dict(schema)
    instruments = tuple(values.pop("instruments"))
    version = StrategyVersion(strategy_id, "V1", evaluator_key, INTRADAY_SCHEMA, lifecycle="DRAFT_RESEARCH")
    parameters = ParameterSet(parameter_set_id, version.strategy_version_id, INTRADAY_SCHEMA.schema_id, values, provenance={
        "derived_from_strategy": strategy_id.replace("_INTRADAY", ""),
        "derivation_type": "TIME_HORIZON_VARIANT",
        "parameter_provenance": {
            "timeframes": "TIMEFRAME_DERIVED",
            "max_hold_minutes": "RESEARCH_HYPOTHESIS",
            "session_boundary": "RESEARCH_HYPOTHESIS",
            "stop_semantics": "PARENT_PRESERVED",
            "target_semantics": "RESEARCH_HYPOTHESIS",
            "thesis_steps": "PARENT_PRESERVED",
            "instrument_membership_source": "OPERATIONAL",
            "execution_eligible": "OPERATIONAL",
        },
    })
    version.validate_parameter_set(parameters)
    instance = ResearchInstance(instance_id, strategy_id, "OFFLINE", False, False, instruments, parameter_set_id)
    return IntradayVariant(version, parameters, instance, values["parent_strategy_id"], "TIME_HORIZON_VARIANT", evaluator_key,
                           "ADAPTER_REQUIRED", "GENERIC_HISTORICAL_FEED_AND_LIVE_MARKET_FEED_PENDING_PARENT_ADAPTER")


def variants() -> tuple[IntradayVariant, IntradayVariant]:
    return (
        _variant(
            "CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1", "context-intraday-v1", "context-intraday-v1-research-001",
            {
                "parent_strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1",
                "context_timeframe": "H4", "setup_timeframe": "H1", "confirmation_timeframe": "M15", "entry_timeframe": "M15",
                "max_hold_minutes": 1440, "session_boundary": "UTC_DAY",
                "stop_semantics": "ORIGINATING_SETUP_EXTREME with minimal causal volatility/spread safety buffer",
                "target_semantics": "RESEARCH_HYPOTHESIS: STRUCTURE_CAPPED_EXTENSION on intraday setup structure; no fixed R selected",
                "thesis_steps": ["higher-timeframe context", "structural retracement", "lower-timeframe confirmation", "entry"],
                "instrument_membership_source": "StrategyInstance-scoped canonical membership; initial set copied from parent availability, not hardcoded in evaluator",
                "instruments": ["XAUUSD", "BTCUSD", "USDJPY", "EURUSD"], "execution_eligible": False,
            }, evaluator_key="context_structure_retrace_intraday_v1",
        ),
        _variant(
            "LIQUIDITY_DISPLACEMENT_INTRADAY_V1", "liquidity-intraday-v1", "liquidity-intraday-v1-research-001",
            {
                "parent_strategy_id": "LIQUIDITY_DISPLACEMENT_SCALP_V1",
                "context_timeframe": "H1", "setup_timeframe": "M15", "confirmation_timeframe": "M15", "entry_timeframe": "M5",
                "max_hold_minutes": 1440, "session_boundary": "UTC_DAY",
                "stop_semantics": "PARENT_PRESERVED: sweep extreme plus causal ATR/spread/broker safety buffer",
                "target_semantics": "RESEARCH_HYPOTHESIS: opposing structural liquidity/swing target; parent fixed 1.25R is not copied",
                "thesis_steps": ["liquidity sweep", "reclaim", "displacement", "MSS", "retracement entry"],
                "instrument_membership_source": "StrategyInstance-scoped canonical membership; initial set limited to parent historical-data cohorts",
                "instruments": ["XAUUSD", "BTCUSD", "USDJPY"], "execution_eligible": False,
            }, evaluator_key="liquidity_displacement_intraday_v1",
        ),
    )


def catalog_payload() -> dict[str, Any]:
    return {
        "schema": "research.strategy_variants.v1",
        "status": "RESEARCH_ONLY_DRAFT",
        "variants": [item.payload() for item in variants()],
        "parent_strategies_changed": False,
        "existing_instances_changed": False,
        "new_instances_online": False,
        "new_instances_execution_eligible": False,
        "same_evaluator_backtest_live": True,
        "adapter_status": "GENERIC_SEMANTIC_FIXTURE_ADAPTER_INTEGRATED",
        "adapter_gap": "Both parent implementations still require raw-OHLC adapters to the generic HistoricalMarketFeed/LiveMarketFeed contract before discovery backtests; the integrated adapter consumes explicit parent-stage evidence only.",
        "production_changed": False,
        "broker_writes": 0,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(catalog_payload(), indent=2, sort_keys=True))
