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
from .registry import register_builtin_evaluators
from .kojo_wedge import (
    KojoWedgeEvaluator,
    kojo_wedge_baseline_parameter_set,
    kojo_wedge_diagnostic_artifact,
    kojo_wedge_parameter_schema,
    write_kojo_wedge_diagnostic_artifact,
)
from .kojo_structure_reclaim import (
    EVALUATOR_KEY as KOJO_STRUCTURE_RECLAIM_EVALUATOR_KEY,
    KojoStructureReclaimEvaluator,
    kojo_structure_reclaim_baseline_parameter_set,
    kojo_structure_reclaim_parameter_schema,
)
from .kojo_structure_reclaim_v2 import (
    EVALUATOR_KEY as KOJO_STRUCTURE_RECLAIM_V2_EVALUATOR_KEY,
    KojoStructureReclaimV2Evaluator,
    kojo_structure_reclaim_v2_baseline_parameter_set,
    kojo_structure_reclaim_v2_parameter_schema,
)
from .parity import assert_live_replay_parity

__all__ = [
    "BacktestArtifactStore", "BacktestEngine", "BacktestResult", "HistoricalMarketFeed", "BacktestRun", "CostModel",
    "EntrySignal", "EntrySignalOutcome", "MarketEvent", "ParameterSchema", "ParameterSet",
    "SetupLifecycleEvent", "StrategyVersion", "ParameterSetCatalog", "StrategyEvaluatorRegistry",
    "assert_live_replay_parity", "register_builtin_evaluators", "KojoWedgeEvaluator",
    "kojo_wedge_parameter_schema", "kojo_wedge_baseline_parameter_set",
    "kojo_wedge_diagnostic_artifact", "write_kojo_wedge_diagnostic_artifact",
    "KOJO_STRUCTURE_RECLAIM_EVALUATOR_KEY", "KojoStructureReclaimEvaluator",
    "kojo_structure_reclaim_parameter_schema", "kojo_structure_reclaim_baseline_parameter_set",
    "KOJO_STRUCTURE_RECLAIM_V2_EVALUATOR_KEY", "KojoStructureReclaimV2Evaluator",
    "kojo_structure_reclaim_v2_parameter_schema", "kojo_structure_reclaim_v2_baseline_parameter_set",
]
