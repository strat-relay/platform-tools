from __future__ import annotations

from typing import Callable, Protocol

from .models import EntrySignal, MarketEvent, ParameterSet, SetupLifecycleEvent, StrategyVersion


class StrategyEvaluator(Protocol):
    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None: ...
    def consume_market_event(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]: ...
    def snapshot_state(self) -> dict: ...
    def restore_state(self, state: dict) -> None: ...


EvaluatorFactory = Callable[[], StrategyEvaluator]


class StrategyEvaluatorRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, EvaluatorFactory] = {}

    def register(self, evaluator_key: str, factory: EvaluatorFactory) -> None:
        if evaluator_key in self._factories:
            raise ValueError(f"evaluator already registered: {evaluator_key}")
        self._factories[evaluator_key] = factory

    def resolve(self, strategy_version: StrategyVersion) -> StrategyEvaluator:
        try:
            return self._factories[strategy_version.evaluator_key]()
        except KeyError as exc:
            raise KeyError(f"no evaluator registered for {strategy_version.evaluator_key}") from exc


class ParameterSetCatalog:
    """Additive research catalog; authoritative use freezes an id to one fingerprint."""

    def __init__(self) -> None:
        self._sets: dict[str, ParameterSet] = {}
        self._authoritative: set[str] = set()

    def add(self, parameter_set: ParameterSet) -> ParameterSet:
        existing = self._sets.get(parameter_set.parameter_set_id)
        if existing and existing.fingerprint != parameter_set.fingerprint:
            if parameter_set.parameter_set_id in self._authoritative:
                raise ValueError("validated ParameterSet is immutable")
            raise ValueError("ParameterSet id already exists with a different fingerprint")
        self._sets[parameter_set.parameter_set_id] = parameter_set
        return parameter_set

    def get(self, parameter_set_id: str) -> ParameterSet:
        return self._sets[parameter_set_id]

    def mark_authoritative(self, parameter_set: ParameterSet) -> None:
        stored = self.get(parameter_set.parameter_set_id)
        if stored.fingerprint != parameter_set.fingerprint:
            raise ValueError("cannot freeze an unknown ParameterSet fingerprint")
        self._authoritative.add(parameter_set.parameter_set_id)

    def is_authoritative(self, parameter_set_id: str) -> bool:
        return parameter_set_id in self._authoritative
