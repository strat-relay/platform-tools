"""Generic, causal adapters for the research-only intraday variants.

The parent runners currently do not expose pure ``HistoricalMarketFeed`` /
``LiveMarketFeed`` evaluators.  This module therefore provides the shared
completed-candle and state/identity contract, plus a stage-evidence adapter
for semantic fixtures.  Raw OHLC without explicit parent-stage evidence is
deliberately not interpreted here; doing so would invent parent rules.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import EntrySignal, MarketEvent, ParameterSet, SetupLifecycleEvent, StrategyVersion, fingerprint


TIMEFRAME_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
CONTEXT_STAGES = ("HTF_STRUCTURE", "RETRACEMENT", "M15_CONFIRMATION", "ENTRY")
LIQUIDITY_STAGES = ("H1_LIQUIDITY_CONTEXT", "M15_SWEEP", "M15_RECLAIM", "M15_DISPLACEMENT", "M15_MSS", "M5_RETRACEMENT_ENTRY")


def _bucket(timestamp: int, timeframe: str) -> int:
    try:
        size = TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc
    return int(timestamp) - int(timestamp) % size


def aggregate_completed_events(events: Iterable[MarketEvent], timeframe: str, *, as_of: int | None = None) -> tuple[MarketEvent, ...]:
    """Aggregate one completed base feed into deterministic UTC candles.

    Input events must have one timeframe, one canonical instrument, and unique
    open timestamps.  A target bucket is emitted only when every expected base
    candle is present.  Missing buckets are omitted and reported through the
    returned event provenance, never silently filled.
    """
    source = tuple(events)
    if not source:
        return ()
    source_tf = source[0].timeframe
    if any(event.timeframe != source_tf for event in source):
        raise ValueError("aggregation input must use one source timeframe")
    if source_tf not in TIMEFRAME_SECONDS or timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("unsupported aggregation timeframe")
    if TIMEFRAME_SECONDS[timeframe] < TIMEFRAME_SECONDS[source_tf] or TIMEFRAME_SECONDS[timeframe] % TIMEFRAME_SECONDS[source_tf]:
        raise ValueError("target timeframe must be an integer multiple of source timeframe")
    ordered = tuple(sorted(source, key=lambda event: (event.open_timestamp, event.canonical_instrument)))
    if len({event.open_timestamp for event in ordered}) != len(ordered):
        raise ValueError("duplicate source candle timestamp")
    width = TIMEFRAME_SECONDS[timeframe]
    base_width = TIMEFRAME_SECONDS[source_tf]
    output: list[MarketEvent] = []
    by_bucket: dict[int, list[MarketEvent]] = {}
    for event in ordered:
        if as_of is not None and event.close_timestamp > as_of:
            continue
        by_bucket.setdefault(_bucket(event.open_timestamp, timeframe), []).append(event)
    for start in sorted(by_bucket):
        bucket = by_bucket[start]
        expected = tuple(start + offset for offset in range(0, width, base_width))
        actual = tuple(event.open_timestamp for event in bucket)
        if actual != expected:
            continue
        first, last = bucket[0], bucket[-1]
        provenance = {
            "aggregation": "UTC_FIXED_BOUNDARY",
            "source_timeframe": source_tf,
            "source_open_timestamps": list(actual),
            "source_bar_count": len(bucket),
            "missing_source_timestamps": [],
            "duplicate_source_timestamps": [],
            "completed_at": last.close_timestamp,
            "future_bars_excluded": True,
        }
        output.append(MarketEvent(
            canonical_instrument=first.canonical_instrument,
            timeframe=timeframe,
            open_timestamp=start,
            close_timestamp=start + width,
            open=first.open,
            high=max(event.high for event in bucket),
            low=min(event.low for event in bucket),
            close=last.close,
            completed=last.close_timestamp <= (as_of if as_of is not None else last.close_timestamp),
            source=first.source,
            provenance=provenance,
        ))
    return tuple(output)


class CausalMultiTimeframeState:
    """Restorable event-prefix state shared by historical and live adapters."""

    def __init__(self, base_timeframe: str = "M5") -> None:
        if base_timeframe not in TIMEFRAME_SECONDS:
            raise ValueError("unsupported base timeframe")
        self.base_timeframe = base_timeframe
        self.events: list[MarketEvent] = []

    def append(self, event: MarketEvent) -> None:
        if event.timeframe != self.base_timeframe:
            raise ValueError(f"expected {self.base_timeframe} event")
        if not event.completed:
            return
        if self.events and event.open_timestamp <= self.events[-1].open_timestamp:
            if event.open_timestamp == self.events[-1].open_timestamp:
                raise ValueError("duplicate event timestamp")
            raise ValueError("events must be chronological")
        self.events.append(event)

    def completed(self, timeframe: str, *, as_of: int | None = None) -> tuple[MarketEvent, ...]:
        return aggregate_completed_events(self.events, timeframe, as_of=as_of)

    def snapshot_state(self) -> dict[str, Any]:
        return {"base_timeframe": self.base_timeframe, "events": [event.identity_payload() for event in self.events]}

    def restore_state(self, state: dict[str, Any]) -> None:
        if state.get("base_timeframe") != self.base_timeframe:
            raise ValueError("base timeframe mismatch")
        restored = [MarketEvent(**row) for row in state.get("events", [])]
        self.events = []
        for event in restored:
            self.append(event)


class IntradayStageEvidenceEvaluator:
    """Generic fixture adapter, not a replacement for parent strategy logic.

    A semantic fixture supplies ``provenance.research_stage`` and final-stage
    economics.  This proves the evaluator/state/identity contract without
    assigning unapproved OHLC rules to either parent strategy.
    """

    def __init__(self, strategy_id: str, stages: tuple[str, ...]) -> None:
        self.strategy_id = strategy_id
        self.stages = stages
        self.strategy_version: StrategyVersion | None = None
        self.parameter_set: ParameterSet | None = None
        self.state = CausalMultiTimeframeState("M5")
        self.stage_index = 0
        self.setup_id: str | None = None
        self.emitted = False

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        if strategy_version.strategy_id != self.strategy_id:
            raise ValueError("strategy identity mismatch")
        strategy_version.validate_parameter_set(parameter_set)
        self.strategy_version = strategy_version
        self.parameter_set = parameter_set

    def consume_market_event(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        if self.strategy_version is None or self.parameter_set is None:
            raise RuntimeError("evaluator is not initialized")
        self.state.append(event)
        stage = event.provenance.get("research_stage")
        if stage != self.stages[self.stage_index] or self.emitted:
            return ()
        available_through = event.close_timestamp
        stage_identity = {"strategy": self.strategy_version.strategy_version_id, "parameter_set": self.parameter_set.fingerprint, "instrument": event.canonical_instrument, "stage": stage, "decision_timestamp": available_through}
        self.setup_id = self.setup_id or "SETUP_" + fingerprint(stage_identity)[:24]
        self.stage_index += 1
        if self.stage_index < len(self.stages):
            return (SetupLifecycleEvent(self.setup_id, self.strategy_version.strategy_version_id, event.canonical_instrument, stage, available_through, {"research_stage": stage, "completed_candle_only": True}),)
        values = event.provenance
        direction = values.get("direction")
        entry = values.get("entry_price")
        stop = values.get("stop_price")
        target = values.get("target_price")
        if direction not in {"LONG", "SHORT"} or not all(isinstance(x, (float, int)) for x in (entry, stop, target)):
            raise ValueError("final semantic fixture stage requires direction and entry/stop/target economics")
        signal_identity = {**stage_identity, "setup_id": self.setup_id, "direction": direction, "entry": entry, "stop": stop, "target": target}
        signal_id = "SIG_" + fingerprint(signal_identity)[:24]
        self.emitted = True
        return (
            SetupLifecycleEvent(self.setup_id, self.strategy_version.strategy_version_id, event.canonical_instrument, "ENTRY", available_through, {"research_stage": stage, "completed_candle_only": True}),
            EntrySignal(signal_id, self.strategy_version.strategy_version_id, event.canonical_instrument, direction, float(entry), float(stop), float(target), available_through, "MARKET", available_through + int(self.parameter_set.values["max_hold_minutes"]) * 60, {"setup_id": self.setup_id, "research_stage": stage, "completed_candle_only": True, "entry_timestamp": available_through}),
        )

    def snapshot_state(self) -> dict[str, Any]:
        return {"stage_index": self.stage_index, "setup_id": self.setup_id, "emitted": self.emitted, "market": self.state.snapshot_state()}

    def restore_state(self, state: dict[str, Any]) -> None:
        self.stage_index = int(state.get("stage_index", 0))
        self.setup_id = state.get("setup_id")
        self.emitted = bool(state.get("emitted", False))
        self.state.restore_state(state.get("market", {}))


class ContextIntradayEvaluator(IntradayStageEvidenceEvaluator):
    VERSION = "CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1_ADAPTER"

    def __init__(self) -> None:
        super().__init__("CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1", CONTEXT_STAGES)


class LiquidityIntradayEvaluator(IntradayStageEvidenceEvaluator):
    VERSION = "LIQUIDITY_DISPLACEMENT_INTRADAY_V1_ADAPTER"

    def __init__(self) -> None:
        super().__init__("LIQUIDITY_DISPLACEMENT_INTRADAY_V1", LIQUIDITY_STAGES)


def register_intraday_evaluators(registry: Any) -> Any:
    registry.register("context_structure_retrace_intraday_v1", ContextIntradayEvaluator)
    registry.register("liquidity_displacement_intraday_v1", LiquidityIntradayEvaluator)
    return registry
