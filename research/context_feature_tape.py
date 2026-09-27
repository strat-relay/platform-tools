"""Causal, research-only feature tape for the Context intraday replay.

The tape is an execution artifact, not a strategy definition.  It stores only
feature snapshots generated from the completed prefix available at a decision
timestamp.  The evaluator still performs pattern detection and all setup,
entry, and outcome decisions while replaying chronologically.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import zlib
from typing import Any, Callable, Iterable

from context_structure_retrace.config import ResearchTimeframes
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace.replay import feature_snapshot
from strategy_backtest.models import MarketEvent


TAPE_VERSION = "context-causal-feature-tape-v1"


@dataclass(frozen=True)
class FeatureTapeRecord:
    feature_timestamp: int
    source_timeframe: str
    latest_source_candle_close: int
    generation_version: str
    snapshot_payload: bytes

    @property
    def snapshot(self) -> dict[str, Any]:
        return json.loads(zlib.decompress(self.snapshot_payload).decode("utf-8"))


class CausalFeatureTape:
    """Immutable timestamp-indexed feature snapshots for one replay input."""

    VERSION = "context-causal-feature-tape-v1"

    def __init__(self, records: Iterable[FeatureTapeRecord] = ()) -> None:
        self._records = {int(record.feature_timestamp): record for record in records}

    @property
    def records(self) -> tuple[FeatureTapeRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def __len__(self) -> int:
        return len(self._records)

    def get(self, feature_timestamp: int) -> dict[str, Any] | None:
        record = self._records.get(int(feature_timestamp))
        return record.snapshot if record else None

    def validate(self) -> None:
        for record in self.records:
            if record.latest_source_candle_close > record.feature_timestamp:
                raise AssertionError("feature tape contains future source data")
            if record.source_timeframe != "M15":
                raise AssertionError("Context feature tape must be generated at M15 decisions")

    def prefix(self, cutoff: int) -> "CausalFeatureTape":
        return CausalFeatureTape(record for record in self.records if record.feature_timestamp <= int(cutoff))

    def memory_bytes(self) -> int:
        # This is an intentionally conservative payload estimate for the JSON
        # representation, suitable for comparing tape variants without relying
        # on CPython object allocator details.
        return sum(len(record.snapshot_payload) for record in self.records)


def _encode_snapshot(snapshot: dict[str, Any]) -> bytes:
    return zlib.compress(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8"), 6)


def build_causal_feature_tape(
    events: Iterable[MarketEvent],
    symbol: str,
    replay_factory: Callable[[Any, int], Any],
    timeframes: ResearchTimeframes,
    generation_version: str = TAPE_VERSION,
) -> CausalFeatureTape:
    """Build snapshots from a growing causal prefix.

    Pattern detection is used only to avoid materializing snapshots at M15
    timestamps that cannot be consumed by the existing evaluator.  It does not
    change the evaluator's pattern or decision semantics.
    """
    # Import locally to keep this module usable by the lightweight test tools.
    from strategy_backtest.raw_ohlc_adapters import _bar

    class Prefix:
        def __init__(self) -> None:
            self.events: list[MarketEvent] = []

        def append(self, event: MarketEvent) -> None:
            self.events.append(event)

        def completed(self, timeframe: str, *, as_of: int | None = None):
            from strategy_backtest.intraday_adapters import aggregate_completed_events
            return aggregate_completed_events(self.events, timeframe, as_of=as_of)

    prefix = Prefix()
    records: list[FeatureTapeRecord] = []
    seen_patterns: set[str] = set()
    for event in sorted(events, key=lambda item: item.open_timestamp):
        prefix.append(event)
        if event.close_timestamp % 900:
            continue
        m15_events = prefix.completed("M15", as_of=event.close_timestamp)
        m15 = [_bar(item) for item in m15_events]
        patterns = detect_patterns(m15, "M15", event.close_timestamp, symbol)
        new_patterns = [pattern for pattern in patterns if pattern["event_id"] not in seen_patterns]
        if not new_patterns:
            continue
        for pattern in new_patterns:
            seen_patterns.add(pattern["event_id"])
        replay = replay_factory(prefix, event.close_timestamp)
        snapshot = feature_snapshot(replay, symbol, event.close_timestamp, timeframes=timeframes)
        latest = max((int(item.close_timestamp) for item in prefix.events), default=event.close_timestamp)
        record = FeatureTapeRecord(event.close_timestamp, "M15", latest, generation_version, _encode_snapshot(snapshot))
        if record.latest_source_candle_close > record.feature_timestamp:
            raise AssertionError("causal feature tape generation violated source cutoff")
        records.append(record)
    tape = CausalFeatureTape(records)
    tape.validate()
    return tape
