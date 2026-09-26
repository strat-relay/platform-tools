"""Generic, research-only StrategyVersion backtest infrastructure."""

from .engine import BacktestArtifactStore, BacktestEngine, BacktestResult
from .feeds import HistoricalMarketFeed
from .models import (
    BacktestRun,
    CostModel,
    EntrySignal,
    EntrySignalOutcome,
    MarketEvent,
    ParameterSchema,
    ParameterSet,
    SetupLifecycleEvent,
    StrategyVersion,
)
from .registry import ParameterSetCatalog, StrategyEvaluatorRegistry
from .parity import assert_live_replay_parity

__all__ = [
    "BacktestArtifactStore", "BacktestEngine", "BacktestResult", "HistoricalMarketFeed", "BacktestRun", "CostModel",
    "EntrySignal", "EntrySignalOutcome", "MarketEvent", "ParameterSchema", "ParameterSet",
    "SetupLifecycleEvent", "StrategyVersion", "ParameterSetCatalog", "StrategyEvaluatorRegistry",
    "assert_live_replay_parity",
]
