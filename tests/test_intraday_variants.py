import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.intraday_variants import (  # noqa: E402
    PARENT_CONTRACTS,
    catalog_payload,
    variants,
)


def test_variants_have_distinct_strategy_versions_parameter_sets_and_instances():
    context, liquidity = variants()
    assert context.strategy.strategy_version_id != liquidity.strategy.strategy_version_id
    assert context.parameter_set.parameter_set_id != liquidity.parameter_set.parameter_set_id
    assert context.parameter_set.fingerprint != liquidity.parameter_set.fingerprint
    assert context.instance.instance_id != liquidity.instance.instance_id


def test_variants_preserve_parent_thesis_steps_without_parent_mutation():
    context, liquidity = variants()
    assert context.parameter_set.values["thesis_steps"] == [
        "higher-timeframe context", "structural retracement", "lower-timeframe confirmation", "entry"
    ]
    assert liquidity.parameter_set.values["thesis_steps"] == [
        "liquidity sweep", "reclaim", "displacement", "MSS", "retracement entry"
    ]
    assert PARENT_CONTRACTS["CONTEXT_STRUCTURE_RETRACE_V1"].strategy_id == "CONTEXT_STRUCTURE_RETRACE_V1"
    assert PARENT_CONTRACTS["LIQUIDITY_DISPLACEMENT_SCALP_V1"].target_r == 1.25


def test_instances_are_offline_and_execution_disabled():
    for item in variants():
        assert item.instance.lifecycle_state == "OFFLINE"
        assert item.instance.enabled is False
        assert item.instance.execution_eligible is False
        assert item.parameter_set.values["execution_eligible"] is False


def test_timeframe_hierarchies_and_explicit_intraday_hold():
    context, liquidity = variants()
    assert (context.parameter_set.values["context_timeframe"], context.parameter_set.values["setup_timeframe"], context.parameter_set.values["confirmation_timeframe"], context.parameter_set.values["entry_timeframe"]) == ("H4", "H1", "M15", "M15")
    assert (liquidity.parameter_set.values["context_timeframe"], liquidity.parameter_set.values["setup_timeframe"], liquidity.parameter_set.values["confirmation_timeframe"], liquidity.parameter_set.values["entry_timeframe"]) == ("H1", "M15", "M15", "M5")
    assert context.parameter_set.values["max_hold_minutes"] == liquidity.parameter_set.values["max_hold_minutes"] == 1440
    assert context.parameter_set.values["session_boundary"] == liquidity.parameter_set.values["session_boundary"] == "UTC_DAY"


def test_catalog_declares_adapter_gap_without_registering_live_runtime():
    payload = catalog_payload()
    assert payload["status"] == "RESEARCH_ONLY_DRAFT"
    assert payload["same_evaluator_backtest_live"] is True
    assert payload["adapter_status"] == "GENERIC_SEMANTIC_FIXTURE_ADAPTER_INTEGRATED"
    assert all(item["evaluator_adapter_status"] == "ADAPTER_REQUIRED" for item in payload["variants"])
    assert payload["parent_strategies_changed"] is False
    assert payload["existing_instances_changed"] is False
    assert payload["new_instances_online"] is False
    assert payload["new_instances_execution_eligible"] is False
    assert payload["broker_writes"] == 0
