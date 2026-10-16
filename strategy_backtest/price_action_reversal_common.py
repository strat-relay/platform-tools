"""Shared causal lifecycle for the research-only Price Action reversals.

Pattern modules provide only their completed-candle detector and frozen contract.
This module owns the common H1 level selection, M15 prior-move gate, next-open
paper-entry handling, provenance, deduplication, and checkpoint semantics.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from core.strategies.evaluation import (
    Decision, DecisionTrace, Evaluation, ReasonCode, StageResult, StageStatus,
    TraceFidelity, canonical_hash,
)

from .models import MarketEvent, ParameterSet, StrategyVersion
from .price_action_primitives import Candle, confirmed_swings, construct_levels, opposing_target


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
        trace = DecisionTrace(stages, decision, reason_codes=(_reason("NO_CANDIDATE" if reason.startswith("INSUFFICIENT") else "NO_VALID_ENTRY_SIGNAL"),), fidelity=TraceFidelity.L3)
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
            "information_cutoff": event.close_timestamp, "rejection_reason": reason,
            "pattern_event_time": extra.pop("pattern_event_time", None),
            "confirmation_time": extra.pop("confirmation_time", None),
            "processing_time": event.provenance.get("processing_time"),
            **extra,
        }
        return Evaluation(self.STRATEGY_ID, event.canonical_instrument, str(event.close_timestamp), decision,
                           trace, direction=extra.get("direction"), strategy_version=self.strategy_version_id,
                           parameter_set_id=self.parameters.parameter_set_id, candidate_id=candidate_id,
                           trace_fidelity=TraceFidelity.L3, runtime_version=self.ENGINE_VERSION,
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
        gap = tick * self.values["entry_gap_cancel_ticks"]
        adverse_gap = direction == "LONG" and entry < pending["pattern_close"] - gap or \
            direction == "SHORT" and entry > pending["pattern_close"] + gap
        structural_invalid = direction == "LONG" and entry < level - tolerance or \
            direction == "SHORT" and entry > level + tolerance
        valid_side = stop < entry < target if direction == "LONG" else target < entry < stop
        actual_r = abs(target - entry) / abs(entry - stop) if entry != stop else 0.0
        if adverse_gap:
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
        return self._evaluation(event, Decision.REJECT, stages, reason, {
            "direction": direction, "entry_open": entry, "stop": stop, "target": target,
            "planned_r": pending["planned_r"], "actual_entry_r": actual_r,
            "entry_cancelled": reason != "ENTRY_OBSERVED_RESEARCH_ONLY",
            "intended_entry_time": pending["pattern_close"], "accepted_paper_entry_time": event.open_timestamp,
            "pattern_event_time": pending["pattern_event_time"], "confirmation_time": pending["confirmation_time"],
            "candidate_id": pending["candidate_id"], "level": pending["level_payload"],
            "entry_gap_distance": abs(entry - pending["pattern_close"]),
            "structural_distance": abs(entry - level),
        })

    def _evaluate_execution_close(self, event: MarketEvent) -> Evaluation | None:
        if event.timeframe != self.values["execution_timeframe"]:
            return None
        instrument = event.canonical_instrument
        context = self._context(instrument, event.close_timestamp)
        minimum = self.values["swing_left_bars"] + self.values["swing_right_bars"] + 1
        if len(context) < minimum:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "INSUFFICIENT_CONTEXT")
        execution = tuple(Candle.from_event(i, x) for i, x in enumerate(
            event for event in self._execution_events.get(instrument, []) if event.completed))
        if len(execution) < self.values["prior_move_bars"] + 3:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "INSUFFICIENT_EXECUTION_WARMUP")
        current = execution[-1]
        tick = float(event.provenance.get("tick_size", 1.0))
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
        prior = execution[-(self.values["prior_move_bars"] + 1):-1]
        closes = [bar.close for bar in prior]
        prior_move = all(a > b for a, b in zip(closes, closes[1:])) if direction == "LONG" else all(a < b for a, b in zip(closes, closes[1:]))
        if not prior_move:
            return self._evaluation(event, Decision.REJECT, (), "NO_PRIOR_DIRECTIONAL_MOVE", {"direction": direction})
        kind = "SUPPORT" if direction == "LONG" else "RESISTANCE"
        touching = [level for level in levels if level.kind == kind and
                    ((current.low <= level.price + tolerance) if direction == "LONG" else (current.high >= level.price - tolerance))]
        if not touching:
            return self._evaluation(event, Decision.REJECT, (), "INVALID_STRUCTURAL_LOCATION", {"direction": direction})
        level = max(touching, key=lambda item: (item.known_at, item.source_swings))
        target_level = opposing_target(levels, direction=direction, entry=current.close, as_of=current.close_timestamp)
        if target_level is None:
            return self._evaluation(event, Decision.REJECT, (), "NO_KNOWN_OPPOSING_TARGET", {"direction": direction, "level": level.payload()})
        stop = (current.low - tick * self.values["stop_buffer_ticks"] if direction == "LONG"
                else current.high + tick * self.values["stop_buffer_ticks"])
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
            StageResult("prior_move", StageStatus.PASS, "structure.prior_move@1", observed={"closes": closes, "direction": direction}),
            StageResult("structural_location", StageStatus.PASS, "price_action.zone_interaction@1", observed=current.payload(), expected=level.payload()),
            StageResult("reversal_pattern", StageStatus.PASS, pattern["primitive_id"], observed=pattern["observed"]),
        )
        pending = {"direction": direction, "level": level.price, "level_payload": level.payload(),
                   "tolerance": tolerance, "tick": tick, "stop": stop, "target": target,
                   "planned_r": planned_r, "pattern_close": current.close,
                   "pattern_event_time": current.close_timestamp, "confirmation_time": current.close_timestamp,
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
        event_id = self._event_id(event)
        if event_id in self.seen_events:
            return ()
        if self.events and (event.close_timestamp < self.events[-1].close_timestamp or
                            event.close_timestamp == self.events[-1].close_timestamp and event.open_timestamp < self.events[-1].open_timestamp):
            raise ValueError("out-of-order market event")
        values = (event.open, event.high, event.low, event.close)
        if any(value != value or value in (float("inf"), float("-inf")) for value in values):
            raise ValueError("invalid OHLC value")
        self.seen_events.add(event_id)
        self.events.append(event)
        if event.timeframe == self.values["context_timeframe"]:
            self._context_events.setdefault(event.canonical_instrument, []).append(event)
        else:
            self._execution_events.setdefault(event.canonical_instrument, []).append(event)
        outputs: list[Evaluation] = []
        resolved = self._resolve_pending(event)
        if resolved is not None:
            outputs.append(resolved)
        evaluation = self._evaluate_execution_close(event)
        if evaluation is not None:
            outputs.append(evaluation)
        unique = []
        for item in outputs:
            if item.evaluation_hash in self.emitted_evaluations:
                continue
            self.emitted_evaluations.add(item.evaluation_hash)
            reason = item.provenance.get("rejection_reason", "UNKNOWN")
            self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1
            unique.append(item)
        return tuple(unique)

    def snapshot_state(self) -> dict[str, Any]:
        return {"events": [asdict(event) for event in self.events], "seen_events": sorted(self.seen_events),
                "emitted_evaluations": sorted(self.emitted_evaluations),
                "consumed_candidates": sorted(self.consumed_candidates),
                "rejection_counts": dict(self.rejection_counts),
                "pending": self.pending}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self.events = [MarketEvent(**item) for item in state.get("events", [])]
        self._context_events, self._execution_events = {}, {}
        for event in self.events:
            target = self._context_events if event.timeframe == "H1" else self._execution_events
            target.setdefault(event.canonical_instrument, []).append(event)
        self.seen_events = set(state.get("seen_events", []))
        self.emitted_evaluations = set(state.get("emitted_evaluations", []))
        self.consumed_candidates = set(state.get("consumed_candidates", []))
        self.rejection_counts = dict(state.get("rejection_counts", {}))
        self.pending = dict(state.get("pending", {}))

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
