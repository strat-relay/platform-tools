from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResearchTimeframes:
    """Instrument-independent timeframe wiring for a research run."""

    execution: str = "M15"
    lower: tuple[str, ...] = ("M5",)
    higher: tuple[str, ...] = ("H1", "H4")

    @property
    def all(self) -> tuple[str, ...]:
        return (self.execution, *self.lower, *self.higher)


DEFAULT_TIMEFRAMES = ResearchTimeframes()
