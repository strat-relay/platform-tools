from __future__ import annotations

from typing import Any, Mapping, Protocol

from core.strategies.evaluation import Evaluation


class StrategyRuntime(Protocol):
    """Small runtime boundary shared by legacy and future evaluators."""

    strategy_id: str
    runtime_version: str

    def evaluate(self, inputs: Mapping[str, Any], *, decision_time: str,
                 as_of: str | None = None) -> Evaluation:
        """Evaluate domain-compatible inputs without I/O or broker dependencies."""
