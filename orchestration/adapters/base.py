from __future__ import annotations

from typing import Protocol

from orchestration.models import StrategySignal


class StrategyAdapter(Protocol):
    strategy_id: str

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[StrategySignal]: ...
