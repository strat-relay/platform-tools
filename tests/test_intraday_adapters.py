import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.intraday_variants import variants  # noqa: E402
from strategy_backtest.feeds import feed_from_events  # noqa: E402
from strategy_backtest.intraday_adapters import (  # noqa: E402
    CONTEXT_STAGES,
    LIQUIDITY_STAGES,
    ContextIntradayEvaluator as StageContextIntradayEvaluator,
    LiquidityIntradayEvaluator as StageLiquidityIntradayEvaluator,
    CausalMultiTimeframeState,
    aggregate_completed_events,
)
from strategy_backtest.models import MarketEvent  # noqa: E402
from strategy_backtest.raw_ohlc_adapters import (  # noqa: E402
    ContextRawOhlcEvaluator,
    LiquidityRawOhlcEvaluator,
)
from strategy_backtest.registry import StrategyEvaluatorRegistry, register_builtin_evaluators  # noqa: E402
from strategy_backtest.parity import assert_live_replay_parity  # noqa: E402


def event(ts, stage=None, **values):
    provenance = {"research_stage": stage} if stage else {}
    provenance.update(values)
    return MarketEvent("XAUUSD", "M5", ts, ts + 300, 100.0, 101.0, 99.0, 100.5, True, "fixture", provenance)


def test_aggregation_uses_fixed_utc_boundaries_and_rejects_missing_source_bar():
    complete = [event(0), event(300), event(600)]
    assert len(aggregate_completed_events(complete, "M15", as_of=900)) == 1
    missing = [event(0), event(600)]
    assert aggregate_completed_events(missing, "M15", as_of=900) == ()


def test_completed_candle_boundary_does_not_expose_partial_target_bar():
    state = CausalMultiTimeframeState()
    for ts in (0, 300, 600, 900, 1200, 1500, 1800, 2100, 2400, 2700, 3000, 3300):
        state.append(event(ts))
    assert len(state.completed("H1", as_of=3599)) == 0
    assert len(state.completed("H1", as_of=3600)) == 1
    state.append(event(3600))
    assert len(state.completed("H1", as_of=3900)) == 1
    state.append(event(3600 + 300))
    assert len(state.completed("H1", as_of=3600 + 600)) == 1


def test_context_and_liquidity_stage_fixtures_preserve_parent_thesis():
    context, liquidity = variants()
    ctx = StageContextIntradayEvaluator()
    ctx.initialize(context.strategy, context.parameter_set)
    outputs = []
    for i, stage in enumerate(CONTEXT_STAGES):
        values = {"direction": "LONG", "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0} if stage == "ENTRY" else {}
        outputs.extend(ctx.consume_market_event(event(i * 300, stage, **values)))
    assert len([x for x in outputs if getattr(x, "signal_id", None)]) == 1
    liq = StageLiquidityIntradayEvaluator()
    liq.initialize(liquidity.strategy, liquidity.parameter_set)
    outputs = []
    for i, stage in enumerate(LIQUIDITY_STAGES):
        values = {"direction": "LONG", "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0} if stage == "M5_RETRACEMENT_ENTRY" else {}
        outputs.extend(liq.consume_market_event(event(i * 300, stage, **values)))
    assert len([x for x in outputs if getattr(x, "signal_id", None)]) == 1


def test_missing_required_stage_is_negative_fixture():
    context, _ = variants()
    evaluator = StageContextIntradayEvaluator()
    evaluator.initialize(context.strategy, context.parameter_set)
    outputs = []
    for i, stage in enumerate(("HTF_STRUCTURE", "RETRACEMENT", "ENTRY")):
        values = {"direction": "LONG", "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0} if stage == "ENTRY" else {}
        outputs.extend(evaluator.consume_market_event(event(i * 300, stage, **values)))
    assert not [x for x in outputs if getattr(x, "signal_id", None)]


def test_historical_live_parity_and_restart_state_identity():
    context, _ = variants()
    stages = []
    for i, stage in enumerate(CONTEXT_STAGES):
        values = {"direction": "LONG", "entry_price": 100.0, "stop_price": 99.0, "target_price": 102.0} if stage == "ENTRY" else {}
        stages.append(event(i * 300, stage, **values))
    historical = feed_from_events(stages, "intraday-semantic-fixture", partition="DISCOVERY")
    live = historical.with_source("live-completed-bar")
    registry = register_builtin_evaluators(StrategyEvaluatorRegistry())
    # Registry invocation uses raw OHLC adapters; stage labels are ignored and
    # therefore cannot become an external runtime dependency.
    assert_live_replay_parity(context.strategy, context.parameter_set, historical, live, registry)
    left = StageContextIntradayEvaluator(); left.initialize(context.strategy, context.parameter_set)
    for item in stages[:2]: left.consume_market_event(item)
    snapshot = left.snapshot_state()
    right = StageContextIntradayEvaluator(); right.initialize(context.strategy, context.parameter_set); right.restore_state(snapshot)
    assert left.snapshot_state() == right.snapshot_state()


def test_registry_resolves_raw_ohlc_adapters_without_external_labels():
    context, liquidity = variants()
    registry = register_builtin_evaluators(StrategyEvaluatorRegistry())
    context_eval = registry.resolve(context.strategy)
    liquidity_eval = registry.resolve(liquidity.strategy)
    assert isinstance(context_eval, ContextRawOhlcEvaluator)
    assert isinstance(liquidity_eval, LiquidityRawOhlcEvaluator)
