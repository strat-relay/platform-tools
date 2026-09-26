"""The read-only `MarketDataProvider` port (A6 02, 04 option C/F).

Trade Observation Service talks to exactly this Protocol - never to a broker position, order,
execution result, the execution consumer, or Phase 7. A future adapter (not built here) would
wrap a read-only market-data source (e.g. the research listener) behind this same Protocol;
nothing in `observation.py` needs to change when that adapter is added.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MarketQuote:
    """A MARKET observation (A6 02 section 2): instrument + provider + instant. No account,
    ticket, lot, or broker state of any kind."""
    instrument: str
    bid: float
    ask: float
    source_timestamp: str  # ISO-8601 UTC
    provider_id: str
    feed_id: str | None = None
    price_semantics_version: str = "ps.v1"
    data_status: str = "FORWARD"  # FORWARD | REPLAY | BACKTEST

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class BarWindow:
    """A BAR reference (A6 02 section 2, 08 section 2.1): carried by reference (`bars_ref`),
    never as an inline array - digest lets the evaluator verify it resolved the same window."""
    timeframe: str
    completed_through: str  # ISO-8601 UTC
    count: int
    digest: str

    def as_ref(self) -> dict[str, object]:
        return {"timeframe": self.timeframe, "completed_through": self.completed_through,
                "count": self.count, "digest": self.digest}


class MarketDataProvider(Protocol):
    """Read-only port. No method here can express a broker position, an order, an execution
    result, or an account - by construction, not by convention."""

    def quote(self, instrument: str) -> MarketQuote:
        """Return the current best-available quote for `instrument`. Raises if unavailable;
        never returns a synthetic/guessed price."""
        ...

    def bars(self, instrument: str, *, timeframe: str = "M5") -> BarWindow | None:
        """Return the completed-bar window reference for `instrument`, or None if the provider
        has no bar store for this timeframe (observation still records with `bars_ref=None`)."""
        ...


class FakeMarketDataProvider:
    """In-process test double. Not a broker/MT5 adapter of any kind - a plain dict-backed
    stand-in for `MarketDataProvider`, used only by tests and the (not-activated) service
    wiring examples."""

    def __init__(self) -> None:
        self._quotes: dict[str, MarketQuote] = {}
        self._bars: dict[str, BarWindow] = {}
        self.quote_calls = 0

    def set_quote(self, instrument: str, quote: MarketQuote) -> None:
        self._quotes[instrument] = quote

    def set_bars(self, instrument: str, bars: BarWindow | None) -> None:
        if bars is None:
            self._bars.pop(instrument, None)
        else:
            self._bars[instrument] = bars

    def quote(self, instrument: str) -> MarketQuote:
        self.quote_calls += 1
        if instrument not in self._quotes:
            raise LookupError(f"no fake quote configured for {instrument!r}")
        return self._quotes[instrument]

    def bars(self, instrument: str, *, timeframe: str = "M5") -> BarWindow | None:
        return self._bars.get(instrument)
