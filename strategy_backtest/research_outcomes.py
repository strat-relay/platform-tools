"""Pure, research-only theoretical outcome resolution.

This module has no broker, database, signal-publication, or execution imports.
It is deliberately separate from ``strategy_backtest.execution`` so its more
conservative ambiguity and data-gap semantics cannot change production callers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


POLICY_VERSION = "price-action-research-outcome-policy-v1"
RESOLVER_VERSION = "shared-research-outcome-resolver-v1"
ADAPTER_VERSION = "price-action-research-entry-adapter-v1"


@dataclass(frozen=True)
class ResearchOutcomeEntry:
    episode_id: str
    strategy_version: str
    instrument: str
    direction: str
    entry_timestamp: int
    entry_price: float
    stop_price: float
    target_price: float
    parameter_hash: str | None = None
    dependency_manifest_hash: str | None = None
    dataset_manifest_hash: str | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResearchOutcome:
    episode_id: str
    strategy_version: str
    instrument: str
    direction: str
    status: str
    entry_timestamp: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_timestamp: int | None
    exit_price: float | None
    realized_r: float | None
    holding_seconds: int | None
    valuation_status: str
    resolution_reason: str
    gap_classification: str | None
    resolver_version: str = RESOLVER_VERSION
    policy_version: str = POLICY_VERSION
    adapter_version: str = ADAPTER_VERSION
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bar_value(bar: Any, name: str) -> float:
    if isinstance(bar, Mapping):
        return float(bar[name])
    return float(getattr(bar, name))


def _bar_time(bar: Any) -> int:
    if isinstance(bar, Mapping):
        return int(bar["time"])
    return int(getattr(bar, "open_timestamp"))


def _bar_close_time(bar: Any, timeframe_seconds: int) -> int:
    if isinstance(bar, Mapping):
        return _bar_time(bar) + timeframe_seconds
    return int(getattr(bar, "close_timestamp"))


def _through_open(direction: str, opening: float, stop: float, target: float) -> tuple[str, float] | None:
    if direction == "LONG":
        if opening <= stop:
            return "SL", opening
        if opening >= target:
            return "TP", opening
    else:
        if opening >= stop:
            return "SL", opening
        if opening <= target:
            return "TP", opening
    return None


def _bar_touch(direction: str, high: float, low: float, stop: float, target: float) -> tuple[str, float] | None:
    stop_hit = low <= stop if direction == "LONG" else high >= stop
    target_hit = high >= target if direction == "LONG" else low <= target
    if stop_hit and target_hit:
        return "AMBIGUOUS_INTRABAR", None  # type: ignore[return-value]
    if stop_hit:
        return "SL", stop
    if target_hit:
        return "TP", target
    return None


def _gap_classification(previous: int, current: int, step: int,
                        expected_gaps: Iterable[Mapping[str, Any]]) -> str | None:
    if current - previous <= step:
        return None
    missing = (current - previous) // step - 1
    for gap in expected_gaps:
        if (int(gap.get("from")) == previous and int(gap.get("to")) == current
                and int(gap.get("missing_bars")) == missing):
            return "EXPECTED_MARKET_CLOSURE_REOPENING"
    return "MISSING_OBSERVATION_GAP"


def resolve_research_entry(entry: ResearchOutcomeEntry, bars: Iterable[Any], *,
                           timeframe_seconds: int = 900,
                           expected_gaps: Iterable[Mapping[str, Any]] = ()) -> ResearchOutcome:
    """Resolve one accepted research entry without inventing a time exit.

    Bars must be complete, ordered, and include the entry candle.  An observed
    open that is already beyond a barrier is valued at that observed open and
    marked uncertain-gap valuation; intrabar touches use the barrier price.
    """
    if entry.direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    risk = abs(entry.entry_price - entry.stop_price)
    if risk <= 0:
        raise ValueError("initial risk must be positive")
    ordered = sorted(list(bars), key=_bar_time)
    path = [bar for bar in ordered if _bar_time(bar) >= entry.entry_timestamp]
    prior = [bar for bar in ordered if _bar_time(bar) < entry.entry_timestamp]
    if not path or _bar_time(path[0]) != entry.entry_timestamp:
        return _outcome(entry, "DATA_GAP_UNRESOLVED", None, None, None, None,
                        "ENTRY_CANDLE_UNAVAILABLE", "MISSING_OBSERVATION_GAP")
    previous_time = _bar_time(prior[-1]) if prior else None
    expected = list(expected_gaps)
    for bar in path:
        opening = _bar_value(bar, "open")
        high = _bar_value(bar, "high")
        low = _bar_value(bar, "low")
        current_time = _bar_time(bar)
        gap = _gap_classification(previous_time, current_time, timeframe_seconds, expected) if previous_time is not None else None
        if gap is not None:
            open_hit = _through_open(entry.direction, opening, entry.stop_price, entry.target_price)
            if open_hit is None:
                return _outcome(entry, "DATA_GAP_UNRESOLVED", None, None, None, None,
                                "PRICE_PATH_UNOBSERVED_BEFORE_REOPEN", gap)
            status, price = open_hit
            return _outcome(entry, status, current_time, price, (price - entry.entry_price) / risk if entry.direction == "LONG" else (entry.entry_price - price) / risk,
                            "UNCERTAIN_GAP_OPEN_REFERENCE", "BARRIER_CROSSED_AT_REOPEN_OPEN", gap)
        open_hit = _through_open(entry.direction, opening, entry.stop_price, entry.target_price)
        if open_hit is not None:
            status, price = open_hit
            return _outcome(entry, status, current_time, price, (price - entry.entry_price) / risk if entry.direction == "LONG" else (entry.entry_price - price) / risk,
                            "UNCERTAIN_GAP_OPEN_REFERENCE", "BARRIER_CROSSED_AT_CANDLE_OPEN", None)
        hit = _bar_touch(entry.direction, high, low, entry.stop_price, entry.target_price)
        if hit is not None:
            status, price = hit
            if status == "AMBIGUOUS_INTRABAR":
                return _outcome(entry, status, _bar_close_time(bar, timeframe_seconds), None, None, "AMBIGUOUS_INTRABAR", "BOTH_BARRIERS_TOUCHED", None)
            realized = (price - entry.entry_price) / risk if entry.direction == "LONG" else (entry.entry_price - price) / risk
            return _outcome(entry, status, _bar_close_time(bar, timeframe_seconds), price, realized,
                            "EXACT_BARRIER_REFERENCE", "SINGLE_BARRIER_TOUCHED", None)
        previous_time = current_time
    return _outcome(entry, "OPEN_AT_DATASET_END", None, None, None, None,
                    "NO_MAXIMUM_HOLD_AND_DATASET_ENDED", None)


def _outcome(entry: ResearchOutcomeEntry, status: str, exit_timestamp: int | None,
             exit_price: float | None, realized_r: float | None,
             valuation_status: str | None, reason: str,
             gap_classification: str | None) -> ResearchOutcome:
    return ResearchOutcome(entry.episode_id, entry.strategy_version, entry.instrument,
                           entry.direction, status, entry.entry_timestamp, entry.entry_price,
                           entry.stop_price, entry.target_price, exit_timestamp, exit_price,
                           realized_r,
                           exit_timestamp - entry.entry_timestamp if exit_timestamp is not None else None,
                           valuation_status or "NOT_VALUED", reason, gap_classification,
                           provenance={"parameter_hash": entry.parameter_hash,
                                       "dependency_manifest_hash": entry.dependency_manifest_hash,
                                       "dataset_manifest_hash": entry.dataset_manifest_hash,
                                       **dict(entry.provenance)})
