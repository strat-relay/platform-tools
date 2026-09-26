from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

from .models import MarketEvent, fingerprint


@dataclass(frozen=True)
class HistoricalMarketFeed:
    """Chronological, completed-only feed with explicit dataset provenance."""

    events: tuple[MarketEvent, ...]
    dataset_id: str
    requested_start: int | None = None
    requested_end: int | None = None
    partition: str = "DISCOVERY"
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        selected = tuple(event for event in self.events if event.completed)
        ordered = tuple(sorted(selected, key=lambda event: (event.open_timestamp, event.canonical_instrument, event.timeframe)))
        if selected != ordered:
            raise ValueError("historical events must already be in deterministic chronological order")
        if self.partition not in {"DISCOVERY", "VALIDATION"}:
            raise ValueError("partition must be DISCOVERY or VALIDATION")
        if self.requested_start is not None and self.requested_end is not None and self.requested_end < self.requested_start:
            raise ValueError("invalid feed bounds")
        object.__setattr__(self, "events", tuple(event for event in ordered if self._in_bounds(event)))
        if not self.snapshot_id:
            object.__setattr__(self, "snapshot_id", fingerprint([event.identity_payload() for event in self.events]))

    def _in_bounds(self, event: MarketEvent) -> bool:
        return (self.requested_start is None or event.open_timestamp >= self.requested_start) and (self.requested_end is None or event.close_timestamp <= self.requested_end)

    @property
    def dataset_fingerprint(self) -> str:
        return fingerprint({"dataset_id": self.dataset_id, "snapshot_id": self.snapshot_id, "partition": self.partition, "events": [event.identity_payload() for event in self.events]})

    def __iter__(self) -> Iterator[MarketEvent]:
        return iter(self.events)

    def with_source(self, source: str) -> "HistoricalMarketFeed":
        return HistoricalMarketFeed(tuple(MarketEvent(**{**event.identity_payload(), "source": source}) for event in self.events), self.dataset_id, self.requested_start, self.requested_end, self.partition, self.snapshot_id)


def feed_from_events(events: Iterable[MarketEvent], dataset_id: str, **kwargs: object) -> HistoricalMarketFeed:
    return HistoricalMarketFeed(tuple(events), dataset_id=dataset_id, **kwargs)
