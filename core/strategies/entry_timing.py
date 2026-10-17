"""Shared causal entry observation; never invent a quote or a candle open.

Completed-bar research reconstruction and contemporaneous entry observation are
separate evidence classes. Providers must supply an actual opening-price event;
current quotes cannot be relabelled as an earlier opening price.
"""
from dataclasses import dataclass
import math

VERSION = "causal-entry-timing.v1"
RECONSTRUCTED = "COMPLETED_CANDLE_RECONSTRUCTION"
OBSERVED = "CONTEMPORANEOUS_CANDLE_OPEN"
MAX_ENTRY_DELAY_SECONDS = 60


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


@dataclass(frozen=True)
class CandleOpenObservation:
    instrument: str
    timeframe_seconds: int
    open_time: int
    price: float
    available_at: float
    observed_at: float
    source_event_id: str
    source: str

    def validate(self, *, expected_open: int, setup_available_at: float,
                 now: float, max_delay: float = MAX_ENTRY_DELAY_SECONDS):
        values = (self.open_time, self.price, self.available_at, self.observed_at,
                  setup_available_at, now, max_delay, self.timeframe_seconds)
        if (not all(finite(v) for v in values) or self.price <= 0 or self.open_time <= 0 or
                not 0 < max_delay <= MAX_ENTRY_DELAY_SECONDS):
            raise ValueError("finite positive price and bounded timing required")
        if (self.timeframe_seconds <= 0 or self.timeframe_seconds % 1 or
                self.open_time % self.timeframe_seconds or self.open_time != expected_open):
            raise ValueError("exact next candle open required")
        if not all(isinstance(v, str) and v.strip() for v in (self.instrument, self.source_event_id, self.source)):
            raise ValueError("provider identity required")
        if not (self.open_time <= self.available_at <= self.observed_at <= now):
            raise ValueError("invalid availability/observation clock order")
        if not (self.open_time <= setup_available_at <= self.observed_at):
            raise ValueError("setup must be confirmed before entry observation")
        if now - self.open_time > max_delay:
            raise ValueError("next-open observation window expired")
        return {"version": VERSION, "entry_classification": OBSERVED,
                "intended_entry_time": self.open_time,
                "entry_observed_at": self.observed_at,
                "entry_data_available_at": self.available_at,
                "setup_available_at": setup_available_at,
                "source_event_id": self.source_event_id, "source": self.source}
