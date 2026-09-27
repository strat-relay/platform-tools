from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .execution import SAME_BAR_POLICY, SimulatedExecution
from .feeds import HistoricalMarketFeed
from .metrics import calculate_metrics
from .models import BacktestRun, CostModel, EntrySignal, EntrySignalOutcome, SetupLifecycleEvent, StrategyVersion, fingerprint
from .registry import StrategyEvaluatorRegistry


ENGINE_VERSION = "strategy-backtest-core-v1"


@dataclass(frozen=True)
class BacktestResult:
    run: BacktestRun
    setups: tuple[SetupLifecycleEvent, ...]
    signals: tuple[EntrySignal, ...]
    outcomes: tuple[EntrySignalOutcome, ...]
    metrics: dict[str, Any]

    @property
    def result_fingerprint(self) -> str:
        return fingerprint({"signals": [asdict(x) for x in self.signals], "outcomes": [asdict(x) for x in self.outcomes], "metrics": self.metrics})


class BacktestEngine:
    def __init__(self, registry: StrategyEvaluatorRegistry, *, engine_version: str = ENGINE_VERSION):
        self.registry = registry
        self.engine_version = engine_version

    def run(self, strategy_version: StrategyVersion, parameter_set, feed: HistoricalMarketFeed, cost_model: CostModel, *, run_id: str = "run-1") -> BacktestResult:
        strategy_version.validate_parameter_set(parameter_set)
        evaluator = self.registry.resolve(strategy_version)
        evaluator.initialize(strategy_version, parameter_set)
        execution = SimulatedExecution(cost_model)
        setups: list[SetupLifecycleEvent] = []
        signals: list[EntrySignal] = []
        last_event = None
        for event in feed:
            for output in evaluator.consume_market_event(event):
                if isinstance(output, SetupLifecycleEvent):
                    setups.append(output)
                elif isinstance(output, EntrySignal):
                    signals.append(output)
                    execution.submit(output)
                else:
                    raise TypeError(f"unsupported evaluator output: {type(output).__name__}")
            # Submit decisions before consuming the current bar. The execution
            # model still enforces causality using decision_timestamp: a signal
            # decided on this bar's close cannot fill until the next open, while
            # an evaluator that waited for that next open can fill it here.
            execution.consume(event)
            last_event = event
        execution.finalize(last_event)
        metrics = calculate_metrics(signals, execution.outcomes)
        evaluator_identity = getattr(evaluator, "VERSION", f"{type(evaluator).__module__}.{type(evaluator).__qualname__}")
        run = BacktestRun(run_id, strategy_version.strategy_version_id, fingerprint({"evaluator": evaluator_identity}), parameter_set.fingerprint, tuple(sorted({event.canonical_instrument for event in feed.events})), tuple(sorted({event.timeframe for event in feed.events})), feed.dataset_fingerprint, feed.requested_start or (feed.events[0].open_timestamp if feed.events else 0), feed.requested_end or (feed.events[-1].close_timestamp if feed.events else 0), feed.partition, cost_model.fingerprint, self.engine_version, "COMPLETED")
        result = BacktestResult(run, tuple(setups), tuple(signals), tuple(execution.outcomes), metrics)
        return BacktestResult(run.transition("COMPLETED", result_fingerprint=result.result_fingerprint), result.setups, result.signals, result.outcomes, result.metrics)


class BacktestArtifactStore:
    """Filesystem artifact store; deliberately independent of production PostgreSQL."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def write(self, result: BacktestResult) -> Path:
        path = self.root / result.run.run_id
        path.mkdir(parents=True, exist_ok=True)
        payload = {"run": asdict(result.run), "setups": [asdict(x) for x in result.setups], "signals": [asdict(x) for x in result.signals], "outcomes": [asdict(x) for x in result.outcomes], "metrics": result.metrics, "same_bar_policy": SAME_BAR_POLICY}
        (path / "result.json").write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return path / "result.json"
