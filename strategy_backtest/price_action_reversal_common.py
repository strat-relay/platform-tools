"""Shared causal lifecycle for the research-only Price Action reversals.

Pattern modules provide only their completed-candle detector and frozen contract.
This module owns the common H1 level selection, M15 prior-move gate, next-open
paper-entry handling, provenance, deduplication, and checkpoint semantics.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import math
from typing import Any, Mapping

from core.strategies.evaluation import (
    Decision, DecisionTrace, Evaluation, ReasonCode, StageResult, StageStatus,
    TraceFidelity, canonical_hash,
)

from .models import MarketEvent, ParameterSet, StrategyVersion
from .price_action_primitives import Candle, confirmed_swings, construct_levels, opposing_target, validate_candle

TIMEFRAME_SECONDS = {"H1": 3600, "M15": 900}
CHECKPOINT_SCHEMA = "price-action-reversal-checkpoint-v3"


def _reason(code: str) -> ReasonCode:
    from core.strategies.evaluation.models import default_reason_codes
    known = {item.code: item for item in default_reason_codes().all()}
    return known.get(code, known["NO_VALID_ENTRY_SIGNAL"])


class ReversalEvaluator:
    """Base lifecycle; subclasses implement ``detect_pattern`` only."""

    STRATEGY_ID = ""
    VERSION = "V1"
    EVALUATOR_KEY = ""
    EVALUATOR_VERSION = ""
    ENGINE_VERSION = "research-sequence-engine-v1"
    RESEARCH_PARAMETER_VALUES: Mapping[str, Any] = {}
    PRIMITIVE_MANIFEST: Mapping[str, str] = {}
    DISPLAY_NAME = ""

    def __init__(self) -> None:
        self.strategy_version: StrategyVersion | None = None
        self.parameters: ParameterSet | None = None
        self.events: list[MarketEvent] = []
        self._context_events: dict[str, list[MarketEvent]] = {}
        self._execution_events: dict[str, list[MarketEvent]] = {}
        self.seen_events: set[str] = set()
        self.emitted_evaluations: set[str] = set()
        self.consumed_candidates: set[str] = set()
        self.candle_content: dict[tuple[str, str, int, int], str] = {}
        self.last_series_open: dict[tuple[str, str], int] = {}
        self.context_watermarks: dict[str, int] = {}
        self.context_timeouts: dict[str, int] = {}
        self.buffered_execution: dict[str, dict[int, MarketEvent]] = {}
        self.rejection_counts: dict[str, int] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    @property
    def strategy_version_id(self) -> str:
        return f"{self.STRATEGY_ID}@{self.VERSION}"

    @property
    def parameter_hash(self) -> str:
        return canonical_hash(self.RESEARCH_PARAMETER_VALUES)

    @property
    def dependency_manifest_hash(self) -> str:
        return canonical_hash(self.PRIMITIVE_MANIFEST)

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        if strategy_version.strategy_version_id != self.strategy_version_id:
            raise ValueError(f"evaluator requires {self.strategy_version_id}")
        if dict(parameter_set.values) != dict(self.RESEARCH_PARAMETER_VALUES):
            raise ValueError(f"{self.DISPLAY_NAME} requires its frozen experimental parameter manifest")
        if parameter_set.values.get("require_exchange_gap") is True:
            raise ValueError("V1 star protocol does not support exchange-session gaps; require_exchange_gap must be false")
        strategy_version.validate_parameter_set(parameter_set)
        self.strategy_version, self.parameters = strategy_version, parameter_set

    @property
    def values(self) -> Mapping[str, Any]:
        if self.parameters is None:
            raise RuntimeError("evaluator is not initialized")
        return self.parameters.values

    @staticmethod
    def _event_id(event: MarketEvent) -> str:
        return canonical_hash(event.identity_payload())

    def _evaluation(self, event: MarketEvent, decision: Decision, stages: tuple[StageResult, ...],
                    reason: str, extra: Mapping[str, Any] | None = None) -> Evaluation:
        assert self.parameters is not None
        extra = dict(extra or {})
        candidate_id = extra.get("candidate_id") or canonical_hash({
            "strategy": self.strategy_version_id, "instrument": event.canonical_instrument,
            "decision_time": event.close_timestamp, "direction": extra.get("direction"),
            "level": extra.get("level"),
        })[:24]
        trace_reason = _reason(reason)
        fidelity = TraceFidelity.L3 if stages else TraceFidelity.L2
        trace = DecisionTrace(stages, decision, reason_codes=(trace_reason,), fidelity=fidelity)
        provenance = {
            "research_only": True, "execution_eligible": False,
            "strategy_definition": {"family": "Price Action", "name": self.DISPLAY_NAME,
                                     "version": self.VERSION, "status": "RESEARCH"},
            "parameter_values": dict(self.values), "parameter_hash": self.parameter_hash,
            "primitive_manifest": dict(self.PRIMITIVE_MANIFEST),
            "dependency_manifest_hash": self.dependency_manifest_hash,
            "evaluator_version": self.EVALUATOR_VERSION, "engine_version": self.ENGINE_VERSION,
            "instrument_timeframe_roles": {"context": self.values["context_timeframe"],
                                            "execution": self.values["execution_timeframe"]},
            "data_snapshot_id": event.provenance.get("snapshot_id", "synthetic-or-supplied"),
            "information_cutoff": event.close_timestamp, "decision_cutoff": event.close_timestamp,
            "rejection_reason": reason,
            "pattern_event_time": extra.pop("pattern_event_time", None),
            "confirmation_time": extra.pop("confirmation_time", None),
            "processing_time": event.provenance.get("processing_time"),
            **extra,
        }
        return Evaluation(self.STRATEGY_ID, event.canonical_instrument, str(event.close_timestamp), decision,
                           trace, direction=extra.get("direction"), strategy_version=self.strategy_version_id,
                           parameter_set_id=self.parameters.parameter_set_id, candidate_id=candidate_id,
                           reason_codes=(trace_reason,), trace_fidelity=fidelity, runtime_version=self.ENGINE_VERSION,
                           evaluator_version=self.EVALUATOR_VERSION, provenance=provenance)

    def detect_pattern(self, candles: tuple[Candle, ...], tick: float) -> dict[str, Any] | None:
        raise NotImplementedError

    def _context(self, instrument: str, cutoff: int) -> tuple[Candle, ...]:
        return tuple(Candle.from_event(i, event) for i, event in enumerate(
            x for x in self._context_events.get(instrument, []) if x.close_timestamp <= cutoff))

    def _resolve_pending(self, event: MarketEvent) -> Evaluation | None:
        if event.timeframe != self.values["execution_timeframe"]:
            return None
        pending = self.pending.pop(event.canonical_instrument, None)
        if pending is None:
            return None
        direction, entry = pending["direction"], float(event.open)
        tick, level, tolerance = pending["tick"], pending["level"], pending["tolerance"]
        stop, target = float(pending["stop"]), float(pending["target"])
        expected_open = pending["expected_next_open_timestamp"]
        delayed = event.open_timestamp != expected_open
        gap = tick * self.values["entry_gap_cancel_ticks"]
        adverse_gap = direction == "LONG" and entry < pending["pattern_close"] - gap or \
            direction == "SHORT" and entry > pending["pattern_close"] + gap
        structural_invalid = direction == "LONG" and entry < level - tolerance or \
            direction == "SHORT" and entry > level + tolerance
        valid_side = stop < entry < target if direction == "LONG" else target < entry < stop
        actual_r = abs(target - entry) / abs(entry - stop) if entry != stop else 0.0
        if delayed:
            reason = "MISSING_EXPECTED_NEXT_CANDLE"
        elif adverse_gap:
            reason = "ENTRY_GAP_CANCELLED"
        elif structural_invalid:
            reason = "STRUCTURAL_INVALIDATION"
        elif not valid_side:
            reason = "INVALID_ACTUAL_ENTRY_GEOMETRY"
        elif actual_r < self.values["minimum_planned_r"]:
            reason = "INSUFFICIENT_ACTUAL_ENTRY_R"
        else:
            reason = "ENTRY_OBSERVED_RESEARCH_ONLY"
        stages = tuple(StageResult(item["stage_id"], StageStatus(item["status"]), item.get("primitive_id"),
                                   item.get("observed"), item.get("expected"), item.get("margin"),
                                   evidence_times=tuple(item.get("evidence_times", ())),
                                   metadata=item.get("metadata") or {})
                       for item in pending["stages"])
        if delayed:
            stages = stages[:-1] + (StageResult("entry_eligibility", StageStatus.FAIL, "entry.next_open@1",
                         observed={"observed_open_timestamp": event.open_timestamp},
                         expected={"timestamp": expected_open}, evidence_times=(str(event.open_timestamp),)),)
        else:
            stages = stages[:-1] + (StageResult("entry_eligibility", StageStatus.PASS if reason == "ENTRY_OBSERVED_RESEARCH_ONLY" else StageStatus.FAIL,
                         "entry.next_open@1", observed={"open": entry, "timestamp": event.open_timestamp},
                         expected={"timestamp": expected_open}, evidence_times=(str(event.open_timestamp),)),)
        extra = {
            "direction": direction, "stop": stop, "target": target,
            "planned_r": pending["planned_r"], "actual_entry_r": actual_r,
            "entry_cancelled": reason != "ENTRY_OBSERVED_RESEARCH_ONLY",
            "intended_entry_time": pending["expected_next_open_timestamp"],
            "intended_entry_price": pending["pattern_close_price"],
            "reference_candle_open_timestamp": event.open_timestamp,
            "reference_candle_open_price": entry,
            "entry_candle_data_available_time": event.provenance.get("data_available_at"),
            "actual_entry_observation_time": event.provenance.get("processing_time"),
            "observation_timestamp_unknown": event.provenance.get("processing_time") is None,
            "reconstruction_classification": "COMPLETED_CANDLE_NEXT_OPEN_RECONSTRUCTION",
            "completed_candle_reconstruction": True,
            "next_candle_mismatch": delayed,
            "pattern_event_time": pending["pattern_event_time"], "confirmation_time": pending["confirmation_time"],
            "candidate_id": pending["candidate_id"], "level": pending["level_payload"],
            "entry_gap_distance": abs(entry - pending["pattern_close"]),
            "structural_distance": abs(entry - level),
        }
        if reason == "ENTRY_OBSERVED_RESEARCH_ONLY":
            extra.update({"entry_open": entry, "accepted_paper_entry_time": event.open_timestamp,
                          "accepted_paper_entry_reference_timestamp": event.open_timestamp})
        else:
            extra["entry_open"] = None
        return self._evaluation(event, Decision.REJECT, stages, reason, extra)

    def _evaluate_execution_close(self, event: MarketEvent) -> Evaluation | None:
        if event.timeframe != self.values["execution_timeframe"]:
            return None
        instrument = event.canonical_instrument
        event_close_cutoff = event.close_timestamp
        context = self._context(instrument, event.close_timestamp)
        minimum = self.values["swing_left_bars"] + self.values["swing_right_bars"] + 1
        if len(context) < minimum:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "INSUFFICIENT_CONTEXT")
        execution = tuple(Candle.from_event(i, x) for i, x in enumerate(
            event for event in self._execution_events.get(instrument, [])
            if event.completed and event.close_timestamp <= event_close_cutoff))
        if len(execution) < self.values["prior_move_bars"] + 3:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "INSUFFICIENT_EXECUTION_WARMUP")
        current = execution[-1]
        tick = float(event.provenance["tick_size"])
        if not math.isfinite(tick) or tick <= 0:
            raise ValueError("tick_size must be finite and positive")
        tolerance = tick * self.values["zone_tolerance_ticks"]
        swings = confirmed_swings(context, left=self.values["swing_left_bars"], right=self.values["swing_right_bars"])
        levels = construct_levels(swings, as_of=event.close_timestamp, tolerance=tolerance,
                                  lookback=self.values["level_lookback_context_bars"],
                                  max_age=self.values["zone_max_age_context_bars"])
        if not levels:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "NO_CONFIRMED_LEVEL")
        pattern = self.detect_pattern(execution, tick)
        if pattern is None:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "PATTERN_NOT_CONFIRMED")
        if pattern.get("rejection_reason"):
            return self._evaluation(event, Decision.REJECT, (), pattern["rejection_reason"],
                                    {"direction": pattern.get("direction")})
        direction = pattern["direction"]
        pattern_start = int(pattern["pattern_start_index"])
        pattern_end = int(pattern["pattern_end_index"])
        if pattern_start < 0 or pattern_end < pattern_start or pattern_end != len(execution) - 1:
            return self._evaluation(event, Decision.REJECT, (), "PATTERN_IDENTITY_INVALID", {"direction": direction})
        pattern_events = self._execution_events[instrument][pattern_start:pattern_end + 1]
        expected_step = TIMEFRAME_SECONDS[self.values["execution_timeframe"]]
        prior_start = pattern_start - self.values["prior_move_bars"]
        sequence_events = self._execution_events[instrument][prior_start:pattern_end + 1] if prior_start >= 0 else ()
        if len(sequence_events) != self.values["prior_move_bars"] + len(pattern_events) or any(
                b.open_timestamp - a.open_timestamp != expected_step
                for a, b in zip(sequence_events, sequence_events[1:])):
            return self._evaluation(event, Decision.REJECT, (), "PATTERN_DATA_GAP", {"direction": direction})
        prior = execution[:pattern_start][-self.values["prior_move_bars"]:]
        closes = [bar.close for bar in prior]
        prior_move = len(prior) == self.values["prior_move_bars"] and (all(a > b for a, b in zip(closes, closes[1:])) if direction == "LONG" else all(a < b for a, b in zip(closes, closes[1:])))
        if not prior_move:
            return self._evaluation(event, Decision.REJECT, (), "NO_PRIOR_DIRECTIONAL_MOVE", {"direction": direction})
        kind = "SUPPORT" if direction == "LONG" else "RESISTANCE"
        pattern_low, pattern_high = pattern["pattern_low"], pattern["pattern_high"]
        touching = [level for level in levels if level.kind == kind and
                    pattern_low <= level.price + tolerance and pattern_high >= level.price - tolerance and
                    ((current.close > level.price) if direction == "LONG" else (current.close < level.price))]
        if not touching:
            return self._evaluation(event, Decision.REJECT, (), "INVALID_STRUCTURAL_LOCATION", {"direction": direction})
        level = max(touching, key=lambda item: (item.known_at, item.source_swings))
        target_level = opposing_target(levels, direction=direction, entry=current.close, as_of=current.close_timestamp)
        if target_level is None:
            return self._evaluation(event, Decision.REJECT, (), "NO_KNOWN_OPPOSING_TARGET", {"direction": direction, "level": level.payload()})
        stop = (pattern_low - tick * self.values["stop_buffer_ticks"] if direction == "LONG"
                else pattern_high + tick * self.values["stop_buffer_ticks"])
        target = target_level.price
        risk, reward = abs(current.close - stop), abs(target - current.close)
        planned_r = reward / risk if risk else 0.0
        valid = stop < current.close < target if direction == "LONG" else target < current.close < stop
        if not valid:
            return self._evaluation(event, Decision.REJECT, (), "INVALID_PLANNED_GEOMETRY", {"direction": direction, "planned_r": planned_r})
        if planned_r < self.values["minimum_planned_r"]:
            return self._evaluation(event, Decision.REJECT, (), "INSUFFICIENT_PLANNED_R", {"direction": direction, "planned_r": planned_r})
        candidate_id = canonical_hash({"strategy": self.strategy_version_id, "instrument": instrument,
                                       "direction": direction, "level": level.payload(),
                                       "pattern_event": current.open_timestamp})[:24]
        if candidate_id in self.consumed_candidates:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "DUPLICATE_EPISODE", {"direction": direction, "candidate_id": candidate_id})
        stages = (
            StageResult("prior_move", StageStatus.PASS, "structure.prior_move@1", observed={"closes": closes, "direction": direction}, evidence_times=tuple(str(x.close_timestamp) for x in prior)),
            StageResult("structural_location", StageStatus.PASS, "price_action.zone_intersection@1", observed={"pattern_low": pattern_low, "pattern_high": pattern_high}, expected=level.payload(), evidence_times=tuple(str(x.close_timestamp) for x in pattern_events)),
            StageResult("reversal_pattern", StageStatus.PASS, pattern["primitive_id"], observed=pattern["observed"], evidence_times=tuple(str(x.close_timestamp) for x in pattern_events)),
            StageResult("entry_eligibility", StageStatus.NOT_EVALUATED, "entry.next_open@1", expected={"timestamp": current.close_timestamp}, evidence_times=()),
        )
        pending = {"direction": direction, "level": level.price, "level_payload": level.payload(),
                   "tolerance": tolerance, "tick": tick, "stop": stop, "target": target,
                   "planned_r": planned_r, "pattern_close": current.close,
                   "pattern_close_price": current.close, "expected_next_open_timestamp": current.close_timestamp,
                   "pattern_event_time": current.close_timestamp, "confirmation_time": current.close_timestamp,
                   "pattern_start_time": pattern_events[0].open_timestamp,
                   "candidate_id": candidate_id, "stages": [stage.to_dict() for stage in stages]}
        self.pending[instrument] = pending
        self.consumed_candidates.add(candidate_id)
        return self._evaluation(event, Decision.REJECT, stages, "RESEARCH_PROPOSAL_ONLY", {
            "direction": direction, "level": level.payload(), "target_level": target_level.payload(),
            "entry_reference": current.close, "stop": stop, "target": target, "planned_r": planned_r,
            "candidate_id": candidate_id, "pattern_event_time": current.close_timestamp,
            "confirmation_time": current.close_timestamp,
        })

    def consume_market_event(self, event: MarketEvent) -> tuple[Evaluation, ...]:
        if not event.completed or event.timeframe not in {self.values.get("context_timeframe", "H1"), self.values.get("execution_timeframe", "M15")}:
            return ()
        timeframe_seconds = TIMEFRAME_SECONDS.get(event.timeframe)
        if timeframe_seconds is None:
            return ()
        if event.open_timestamp % timeframe_seconds != 0 or event.close_timestamp - event.open_timestamp != timeframe_seconds:
            raise ValueError("timestamp is not aligned to timeframe")
        if "snapshot_id" not in event.provenance or "tick_size" not in event.provenance:
            raise ValueError("snapshot_id and tick_size metadata are required")
        tick = event.provenance["tick_size"]
        if not isinstance(tick, (int, float)) or not math.isfinite(float(tick)) or float(tick) <= 0:
            raise ValueError("tick_size must be finite and positive")
        validate_candle(Candle.from_event(0, event))
        identity = (event.canonical_instrument, event.timeframe, event.open_timestamp, event.close_timestamp)
        content_id = canonical_hash({"open": event.open, "high": event.high, "low": event.low,
                                     "close": event.close, "completed": event.completed,
                                     "source": event.source})
        prior_content = self.candle_content.get(identity)
        if prior_content is not None and prior_content != content_id:
            raise ValueError("conflicting candle revision")
        if prior_content is not None:
            return ()
        event_id = content_id
        series = (event.canonical_instrument, event.timeframe)
        if series in self.last_series_open and event.open_timestamp < self.last_series_open[series]:
            raise ValueError("out-of-order market event")
        context_watermark = None
        if event.timeframe == self.values["context_timeframe"]:
            context_watermark = event.provenance.get("context_watermark_timestamp", event.close_timestamp)
            if not isinstance(context_watermark, int) or context_watermark != event.close_timestamp:
                raise ValueError("context_watermark_timestamp must equal the completed H1 close")
        self.candle_content[identity] = content_id
        self.last_series_open[series] = event.open_timestamp
        self.seen_events.add(event_id)
        self.events.append(event)
        if event.timeframe == self.values["context_timeframe"]:
            self._context_events.setdefault(event.canonical_instrument, []).append(event)
            self._context_events[event.canonical_instrument].sort(key=lambda item: item.open_timestamp)
            self.context_watermarks[event.canonical_instrument] = max(
                self.context_watermarks.get(event.canonical_instrument, 0), context_watermark)
        else:
            self._execution_events.setdefault(event.canonical_instrument, []).append(event)
            self._execution_events[event.canonical_instrument].sort(key=lambda item: item.open_timestamp)
        outputs: list[Evaluation] = []
        if event.timeframe == self.values["execution_timeframe"]:
            self.buffered_execution.setdefault(event.canonical_instrument, {})[event.close_timestamp] = event
        outputs.extend(self._drain_ready(event.canonical_instrument))
        return tuple(outputs)

    def _process_execution_event(self, event: MarketEvent) -> list[Evaluation]:
        outputs: list[Evaluation] = []
        resolved = self._resolve_pending(event)
        if resolved is not None:
            outputs.append(resolved)
        evaluation = self._evaluate_execution_close(event)
        if evaluation is not None:
            outputs.append(evaluation)
        unique: list[Evaluation] = []
        for item in outputs:
            if item.evaluation_hash in self.emitted_evaluations:
                continue
            self.emitted_evaluations.add(item.evaluation_hash)
            reason = item.provenance.get("rejection_reason", "UNKNOWN")
            self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
            unique.append(item)
        return unique

    def _context_covered(self, instrument: str, cutoff: int) -> bool:
        required = cutoff // 3600 * 3600
        context = self._context(instrument, cutoff)
        if not context or context[-1].close_timestamp != required:
            return False
        # No market calendar is pinned for these definitions yet. Missing H1
        # observations fail closed instead of being declared market closures.
        window = context[-self.values["level_lookback_context_bars"]:]
        return all(b.open_timestamp == a.close_timestamp for a, b in zip(window, window[1:]))

    def _drain_ready(self, instrument: str) -> list[Evaluation]:
        watermark = self.context_watermarks.get(instrument, 0)
        ready = [event for event in self.buffered_execution.get(instrument, {}).values()
                 if instrument in self.context_watermarks
                 and event.close_timestamp // 3600 * 3600 <= watermark
                 and self._context_covered(instrument, event.close_timestamp)]
        ready.sort(key=lambda item: (item.close_timestamp, item.open_timestamp))
        outputs: list[Evaluation] = []
        for event in ready:
            self.buffered_execution[instrument].pop(event.close_timestamp, None)
            if event.close_timestamp <= self.context_timeouts.get(instrument, -1):
                continue
            outputs.extend(self._process_execution_event(event))
        return outputs

    def finalize_context(self, instrument: str, watermark_timestamp: int, *, timeout: bool = False) -> tuple[Evaluation, ...]:
        """Finalize an event-time boundary or emit explicit missing-context timeouts."""
        if self.parameters is None:
            raise RuntimeError("evaluator is not initialized")
        if not isinstance(watermark_timestamp, int) or watermark_timestamp < self.context_watermarks.get(instrument, 0):
            raise ValueError("context watermark must be a monotonic integer timestamp")
        if not timeout:
            # Coverage is established by delivered H1 data, never by a caller
            # asserting an arbitrary future watermark.
            required = watermark_timestamp // 3600 * 3600
            if required > self.context_watermarks.get(instrument, 0):
                raise ValueError("context coverage requires completed H1 data")
            return tuple(self._drain_ready(instrument))
        self.context_timeouts[instrument] = max(
            self.context_timeouts.get(instrument, -1), watermark_timestamp)
        outputs: list[Evaluation] = []
        pending = self.pending.get(instrument)
        if pending is not None and pending["expected_next_open_timestamp"] <= watermark_timestamp:
            self.pending.pop(instrument)
            source = next(event for event in reversed(self._execution_events[instrument])
                          if event.close_timestamp == pending["confirmation_time"])
            outputs.append(self._evaluation(source, Decision.REJECT, (), "CONTEXT_UNAVAILABLE_TIMEOUT", {
                "candidate_id": pending["candidate_id"], "direction": pending["direction"],
                "entry_cancelled": True, "intended_entry_time": pending["expected_next_open_timestamp"],
                "timeout_boundary": watermark_timestamp,
            }))
        for event in sorted(self.buffered_execution.get(instrument, {}).values(), key=lambda item: item.close_timestamp):
            if event.close_timestamp <= watermark_timestamp:
                self.buffered_execution[instrument].pop(event.close_timestamp, None)
                outputs.append(self._evaluation(event, Decision.NO_CANDIDATE, (), "CONTEXT_UNAVAILABLE_TIMEOUT"))
        for item in outputs:
            self.emitted_evaluations.add(item.evaluation_hash)
            reason = item.provenance["rejection_reason"]
            self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
        return tuple(outputs)

    def snapshot_state(self) -> dict[str, Any]:
        return {"schema": CHECKPOINT_SCHEMA, "binding": {"strategy_version_id": self.strategy_version_id,
                "parameter_hash": self.parameter_hash, "dependency_manifest_hash": self.dependency_manifest_hash,
                "evaluator_key": self.EVALUATOR_KEY, "evaluator_version": self.EVALUATOR_VERSION,
                "engine_version": self.ENGINE_VERSION},
                "events": deepcopy([asdict(event) for event in self.events]), "seen_events": sorted(self.seen_events),
                "emitted_evaluations": sorted(self.emitted_evaluations),
                "consumed_candidates": sorted(self.consumed_candidates),
                "rejection_counts": dict(self.rejection_counts),
                "pending": deepcopy(self.pending), "context_watermarks": deepcopy(self.context_watermarks),
                "context_timeouts": deepcopy(self.context_timeouts),
                "buffered_execution": {instrument: [asdict(event) for event in events.values()]
                                       for instrument, events in self.buffered_execution.items()},
                "candle_content": [{"identity": list(key), "content": value} for key, value in self.candle_content.items()],
                "last_series_open": [{"series": list(key), "open": value} for key, value in self.last_series_open.items()]}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if self.parameters is None or state.get("schema") != CHECKPOINT_SCHEMA:
            raise ValueError("incompatible checkpoint schema or uninitialized evaluator")
        expected = {"strategy_version_id": self.strategy_version_id, "parameter_hash": self.parameter_hash,
                    "dependency_manifest_hash": self.dependency_manifest_hash,
                    "evaluator_key": self.EVALUATOR_KEY, "evaluator_version": self.EVALUATOR_VERSION,
                    "engine_version": self.ENGINE_VERSION}
        if state.get("binding") != expected:
            raise ValueError("checkpoint binding does not match evaluator")
        self.events = [MarketEvent(**item) for item in deepcopy(state.get("events", []))]
        self._context_events, self._execution_events = {}, {}
        for event in self.events:
            target = self._context_events if event.timeframe == "H1" else self._execution_events
            target.setdefault(event.canonical_instrument, []).append(event)
        self.seen_events = set(deepcopy(state.get("seen_events", [])))
        self.emitted_evaluations = set(deepcopy(state.get("emitted_evaluations", [])))
        self.consumed_candidates = set(deepcopy(state.get("consumed_candidates", [])))
        self.rejection_counts = dict(deepcopy(state.get("rejection_counts", {})))
        self.pending = deepcopy(state.get("pending", {}))
        self.context_watermarks = dict(deepcopy(state.get("context_watermarks", {})))
        self.context_timeouts = dict(deepcopy(state.get("context_timeouts", {})))
        self.buffered_execution = {
            instrument: {event.close_timestamp: event for event in
                         (MarketEvent(**item) for item in events)}
            for instrument, events in deepcopy(state.get("buffered_execution", {})).items()
        }
        self.candle_content = {tuple(item["identity"]): item["content"] for item in state.get("candle_content", [])}
        self.last_series_open = {tuple(item["series"]): item["open"] for item in state.get("last_series_open", [])}

    def diagnostics(self) -> dict[str, Any]:
        return {"research_only": True, "parameter_hash": self.parameter_hash,
                "dependency_manifest_hash": self.dependency_manifest_hash,
                "rejection_counts": dict(self.rejection_counts)}


def contract_strategy_version(strategy_id: str, evaluator_key: str, parameter_schema_id: str,
                              parameters: Mapping[str, Any]) -> StrategyVersion:
    from .models import ParameterSchema
    return StrategyVersion(strategy_id, "V1", evaluator_key,
                           ParameterSchema(parameter_schema_id, {name: {"required": True} for name in parameters}),
                           "RESEARCH")


def contract_parameter_set(parameter_set_id: str, strategy_version_id: str, schema_id: str,
                           parameters: Mapping[str, Any]) -> ParameterSet:
    return ParameterSet(parameter_set_id, strategy_version_id, schema_id, parameters,
                        {"status": "EXPERIMENTAL", "validated": False})
