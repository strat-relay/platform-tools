"""Research-only Engulfing Continuation V1 evaluator."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from core.strategies.evaluation import (
    Decision, DecisionTrace, Evaluation, ReasonCode, StageResult, StageStatus,
    TraceFidelity, canonical_hash,
)

from .models import MarketEvent, ParameterSchema, ParameterSet, StrategyVersion
from .price_action_primitives import (
    Candle, SRLevel, confirmed_swings, construct_levels, engulfing_confirmation,
    opposing_target, structural_stop,
)


STRATEGY_ID = "PRICE_ACTION_ENGULFING_CONTINUATION"
VERSION = "V1"
STRATEGY_VERSION_ID = f"{STRATEGY_ID}@{VERSION}"
EVALUATOR_KEY = "PRICE_ACTION_ENGULFING_CONTINUATION_V1_RESEARCH"
EVALUATOR_VERSION = "price-action-engulfing-continuation-v1-evaluator-1"
ENGINE_VERSION = "research-sequence-engine-v1"

PRIMITIVE_MANIFEST = {
    "structure.confirmed_swings@1": "strict-left-right-2-confirmed-on-right-close",
    "structure.trend@1": "two-confirmed-higher-highs-and-higher-lows-or-lower-highs-and-lower-lows",
    "structure.sr_levels@1": "confirmed-pivot-price-clusters-nearest-selection",
    "price_action.zone_interaction@1": "execution-extreme-enters-known-structural-zone",
    "price_action.engulfing@1": "strict-body-engulfing-minimum-body",
    "risk.structural_stop@1": "pullback-extreme-plus-tick-buffer",
    "risk.opposing_target@1": "nearest-known-opposing-level",
}

RESEARCH_PARAMETER_VALUES = {
    "context_timeframe": "H1",
    "execution_timeframe": "M15",
    "swing_left_bars": 2,
    "swing_right_bars": 2,
    "zone_tolerance_ticks": 8,
    "zone_max_age_context_bars": 120,
    "max_pullback_bars": 6,
    "allow_same_candle_interaction_confirmation": False,
    "minimum_engulfing_body_ticks": 4,
    "stop_buffer_ticks": 2,
    "entry_gap_cancel_ticks": 8,
    "minimum_planned_r": 1.5,
}

PARAMETER_HASH = canonical_hash(RESEARCH_PARAMETER_VALUES)
DEPENDENCY_MANIFEST_HASH = canonical_hash(PRIMITIVE_MANIFEST)


def _reason(code: str) -> ReasonCode:
    from core.strategies.evaluation.models import default_reason_codes
    registry = default_reason_codes()
    known = {item.code: item for item in registry.all()}
    return known["NO_CANDIDATE" if code == "NO_SETUP" else "NO_VALID_ENTRY_SIGNAL"]


def trend_from_swings(swings: tuple[Any, ...]) -> str | None:
    """Return a trend only from two confirmed swings of each kind."""
    highs = [s for s in swings if s.kind == "RESISTANCE"]
    lows = [s for s in swings if s.kind == "SUPPORT"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    high_up = highs[-1].price > highs[-2].price
    low_up = lows[-1].price > lows[-2].price
    high_down = highs[-1].price < highs[-2].price
    low_down = lows[-1].price < lows[-2].price
    if high_up and low_up:
        return "UP"
    if high_down and low_down:
        return "DOWN"
    return None


def _event_id(event: MarketEvent) -> str:
    return canonical_hash(event.identity_payload())


class EngulfingContinuationEvaluator:
    VERSION = EVALUATOR_VERSION

    def __init__(self) -> None:
        self.strategy_version: StrategyVersion | None = None
        self.parameters: ParameterSet | None = None
        self.events: list[MarketEvent] = []
        self.seen_events: set[str] = set()
        self.emitted_evaluations: set[str] = set()
        self.pending: dict[str, dict[str, Any]] = {}
        self.consumed_candidates: set[str] = set()
        self.rejection_counts: dict[str, int] = {}

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        if strategy_version.strategy_version_id != STRATEGY_VERSION_ID:
            raise ValueError(f"evaluator requires {STRATEGY_VERSION_ID}")
        if parameter_set.values != RESEARCH_PARAMETER_VALUES:
            raise ValueError("Engulfing Continuation V1 requires the frozen experimental parameter manifest")
        self.strategy_version = strategy_version
        self.parameters = parameter_set

    @property
    def values(self) -> Mapping[str, Any]:
        if self.parameters is None:
            raise RuntimeError("evaluator is not initialized")
        return self.parameters.values

    def _evaluation(self, event: MarketEvent, decision: Decision, stages: tuple[StageResult, ...],
                    reason: str, extra: Mapping[str, Any] | None = None) -> Evaluation:
        assert self.parameters is not None
        extra = dict(extra or {})
        candidate_id = extra.get("candidate_id") or canonical_hash({
            "strategy": STRATEGY_VERSION_ID, "instrument": event.canonical_instrument,
            "decision_time": event.close_timestamp, "direction": extra.get("direction"),
            "zone": extra.get("zone"),
        })[:24]
        trace = DecisionTrace(stages, decision, reason_codes=(_reason(reason),), fidelity=TraceFidelity.L3)
        provenance = {
            "research_only": True, "execution_eligible": False,
            "strategy_definition": {"family": "Price Action", "name": "Engulfing Continuation", "version": VERSION, "status": "RESEARCH"},
            "parameter_values": dict(self.values), "parameter_hash": PARAMETER_HASH,
            "primitive_manifest": PRIMITIVE_MANIFEST, "dependency_manifest_hash": DEPENDENCY_MANIFEST_HASH,
            "evaluator_version": EVALUATOR_VERSION, "engine_version": ENGINE_VERSION,
            "instrument_timeframe_roles": {"context": self.values["context_timeframe"], "execution": self.values["execution_timeframe"]},
            "data_snapshot_id": event.provenance.get("snapshot_id", "synthetic-or-supplied"),
            "information_cutoff": event.close_timestamp, "rejection_reason": reason, **extra,
        }
        return Evaluation(STRATEGY_ID, event.canonical_instrument, str(event.close_timestamp), decision, trace,
                           direction=extra.get("direction"), strategy_version=STRATEGY_VERSION_ID,
                           parameter_set_id=self.parameters.parameter_set_id, candidate_id=candidate_id,
                           trace_fidelity=TraceFidelity.L3, runtime_version=ENGINE_VERSION,
                           evaluator_version=EVALUATOR_VERSION, provenance=provenance)

    def _resolve_pending(self, event: MarketEvent) -> Evaluation | None:
        pending = self.pending.pop(event.canonical_instrument, None)
        if pending is None:
            return None
        direction = pending["direction"]
        entry = float(event.open)
        level = float(pending["zone"]["price"])
        tick = float(pending["tick"])
        stop, target = float(pending["stop"]), float(pending["target"])
        gap = tick * self.values["entry_gap_cancel_ticks"]
        valid_side = stop < entry < target if direction == "LONG" else target < entry < stop
        context_after_confirmation = [x for x in self.events
                                      if x.canonical_instrument == event.canonical_instrument and
                                      x.timeframe == self.values["context_timeframe"] and
                                      x.close_timestamp > pending["confirmation_time"] and
                                      x.close_timestamp <= event.open_timestamp]
        tolerance = tick * self.values["zone_tolerance_ticks"]
        trend_invalidated = any((direction == "LONG" and x.close < level - tolerance) or
                                (direction == "SHORT" and x.close > level + tolerance)
                                for x in context_after_confirmation)
        actual_r = abs(target - entry) / abs(entry - stop) if entry != stop else 0.0
        cancelled = ((direction == "LONG" and entry < level - gap) or
                     (direction == "SHORT" and entry > level + gap) or not valid_side or
                     trend_invalidated or actual_r < self.values["minimum_planned_r"])
        if trend_invalidated:
            reason = "HTF_TREND_INVALIDATED"
        elif actual_r < self.values["minimum_planned_r"]:
            reason = "INSUFFICIENT_ENTRY_R"
        else:
            reason = "ENTRY_GAP_CANCELLED" if cancelled else "ENTRY_OBSERVED_RESEARCH_ONLY"
        stages = tuple(
            stage if isinstance(stage, StageResult) else StageResult(
                stage["stage_id"], StageStatus(stage["status"]), stage.get("primitive_id"),
                stage.get("observed"), stage.get("expected"), stage.get("margin"),
                tuple(stage.get("evidence_times", ())), metadata=stage.get("metadata", {})
            )
            for stage in pending["stages"]
        )
        return self._evaluation(event, Decision.REJECT, stages,
                                reason,
                                {"direction": direction, "entry_open": entry, "stop": stop,
                                 "target": target, "planned_r": pending["planned_r"],
                                 "entry_cancelled": cancelled, "actual_entry_r": actual_r,
                                 "candidate_id": pending["candidate_id"],
                                 "information_cutoff": event.open_timestamp})

    def _context(self, instrument: str, cutoff: int) -> tuple[Candle, ...]:
        return tuple(Candle.from_event(i, event) for i, event in enumerate(
            x for x in self.events if x.canonical_instrument == instrument and
            x.timeframe == self.values["context_timeframe"] and x.close_timestamp <= cutoff))

    def _evaluate_execution_close(self, event: MarketEvent) -> Evaluation | None:
        if event.timeframe != self.values["execution_timeframe"]:
            return None
        context = self._context(event.canonical_instrument, event.close_timestamp)
        minimum = self.values["swing_left_bars"] + self.values["swing_right_bars"] + 1
        if len(context) < minimum:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "INSUFFICIENT_CONTEXT")
        swings = confirmed_swings(context, left=self.values["swing_left_bars"], right=self.values["swing_right_bars"])
        trend = trend_from_swings(swings)
        if trend is None:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "NO_CONFIRMED_TREND")
        tick = float(event.provenance.get("tick_size", 1.0))
        tolerance = tick * self.values["zone_tolerance_ticks"]
        levels = construct_levels(swings, as_of=event.close_timestamp, tolerance=tolerance,
                                  lookback=80, max_age=self.values["zone_max_age_context_bars"])
        zone_kind = "SUPPORT" if trend == "UP" else "RESISTANCE"
        zones = [level for level in levels if level.kind == zone_kind]
        if not zones:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "NO_STRUCTURAL_ZONE")
        execution = tuple(Candle.from_event(i, x) for i, x in enumerate(
            x for x in self.events if x.canonical_instrument == event.canonical_instrument and
            x.timeframe == self.values["execution_timeframe"] and x.completed))
        current = execution[-1]
        # Zone selection is structural and stable for the episode; it must not
        # move to an older zone merely because a later candle travels farther.
        zone = max(zones, key=lambda item: (item.known_at, item.source_swings))
        direction = "LONG" if trend == "UP" else "SHORT"
        interactions = [bar for bar in execution[:-1] if
                        (bar.low <= zone.price + tolerance if direction == "LONG" else bar.high >= zone.price - tolerance)]
        if not interactions:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "NO_PULLBACK")
        pullback = interactions[-1]
        if current.index - pullback.index > self.values["max_pullback_bars"]:
            return self._evaluation(event, Decision.REJECT, (), "PULLBACK_EXPIRED", {"direction": direction, "zone": zone.payload()})
        if pullback.index == current.index and not self.values["allow_same_candle_interaction_confirmation"]:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "WAITING_AFTER_PULLBACK", {"direction": direction})
        previous = execution[current.index - 1] if current.index > 0 else current
        body = abs(current.close - current.open)
        if body < tick * self.values["minimum_engulfing_body_ticks"]:
            return self._evaluation(event, Decision.REJECT, (), "ENGULFING_BODY_TOO_SMALL", {"direction": direction})
        if not engulfing_confirmation(previous, current, direction=direction):
            return self._evaluation(event, Decision.REJECT, (), "NOT_TREND_DIRECTION_ENGULFING", {"direction": direction})
        stop = structural_stop(pullback, direction=direction, buffer=tick * self.values["stop_buffer_ticks"])
        target_level = opposing_target(levels, direction=direction, entry=current.close, as_of=current.close_timestamp)
        if target_level is None:
            return self._evaluation(event, Decision.REJECT, (), "NO_OPPOSING_TARGET", {"direction": direction, "zone": zone.payload()})
        target = target_level.price
        risk, reward = abs(current.close - stop), abs(target - current.close)
        planned_r = reward / risk if risk else 0.0
        if planned_r < self.values["minimum_planned_r"] or not (stop < current.close < target if direction == "LONG" else target < current.close < stop):
            return self._evaluation(event, Decision.REJECT, (), "INSUFFICIENT_PLANNED_R", {"direction": direction, "planned_r": planned_r})
        candidate_id = canonical_hash({"strategy": STRATEGY_VERSION_ID, "instrument": event.canonical_instrument,
                                       "direction": direction, "zone": zone.payload(), "pullback": pullback.index})[:24]
        if candidate_id in self.consumed_candidates:
            return self._evaluation(event, Decision.NO_CANDIDATE, (), "DUPLICATE_EPISODE", {"direction": direction, "candidate_id": candidate_id})
        stages = (
            StageResult("trend", StageStatus.PASS, "structure.trend@1", observed={"trend": trend, "known_at": max(s.known_at for s in swings)}),
            StageResult("pullback_zone", StageStatus.PASS, "price_action.zone_interaction@1", observed=pullback.payload(), expected=zone.payload()),
            StageResult("engulfing", StageStatus.PASS, "price_action.engulfing@1", observed=current.payload(), expected={"direction": direction, "minimum_body_ticks": self.values["minimum_engulfing_body_ticks"]}),
        )
        pending = {"direction": direction, "zone": zone.payload(), "stop": stop, "target": target,
                   "planned_r": planned_r, "tick": tick, "stages": stages, "candidate_id": candidate_id,
                   "confirmation_time": current.close_timestamp}
        self.pending[event.canonical_instrument] = pending
        return self._evaluation(event, Decision.REJECT, stages, "RESEARCH_PROPOSAL_ONLY",
                                {"direction": direction, "zone": zone.payload(), "target_level": target_level.payload(),
                                 "entry_reference": current.close, "stop": stop, "target": target,
                                 "planned_r": planned_r, "candidate_id": candidate_id})

    def consume_market_event(self, event: MarketEvent) -> tuple[Evaluation, ...]:
        if not event.completed or event.timeframe not in {self.values.get("context_timeframe", "H1"), self.values.get("execution_timeframe", "M15")}:
            return ()
        event_id = _event_id(event)
        if event_id in self.seen_events:
            return ()
        if self.events and event.close_timestamp < self.events[-1].close_timestamp:
            raise ValueError("out-of-order market event")
        values = (event.open, event.high, event.low, event.close)
        if any(value != value or value in (float("inf"), float("-inf")) for value in values):
            raise ValueError("invalid OHLC value")
        self.seen_events.add(event_id)
        self.events.append(event)
        outputs: list[Evaluation] = []
        if event.timeframe == self.values.get("execution_timeframe", "M15"):
            resolved = self._resolve_pending(event)
            if resolved is not None:
                outputs.append(resolved)
        evaluation = self._evaluate_execution_close(event)
        if evaluation is not None:
            outputs.append(evaluation)
        unique: list[Evaluation] = []
        for item in outputs:
            if item.evaluation_hash not in self.emitted_evaluations:
                self.emitted_evaluations.add(item.evaluation_hash)
                self.rejection_counts[item.provenance["rejection_reason"]] = self.rejection_counts.get(item.provenance["rejection_reason"], 0) + 1
                unique.append(item)
        return tuple(unique)

    def snapshot_state(self) -> dict[str, Any]:
        pending = {instrument: {**value, "stages": [stage.to_dict() for stage in value["stages"]]}
                   for instrument, value in self.pending.items()}
        return {"events": [asdict(event) for event in self.events], "seen_events": sorted(self.seen_events),
                "emitted_evaluations": sorted(self.emitted_evaluations), "pending": pending,
                "consumed_candidates": sorted(self.consumed_candidates), "rejection_counts": dict(self.rejection_counts)}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self.events = [MarketEvent(**item) for item in state.get("events", [])]
        self.seen_events = set(state.get("seen_events", []))
        self.emitted_evaluations = set(state.get("emitted_evaluations", []))
        self.pending = dict(state.get("pending", {}))
        self.consumed_candidates = set(state.get("consumed_candidates", []))
        self.rejection_counts = dict(state.get("rejection_counts", {}))

    def diagnostics(self) -> dict[str, Any]:
        return {"research_only": True, "parameter_hash": PARAMETER_HASH,
                "dependency_manifest_hash": DEPENDENCY_MANIFEST_HASH,
                "rejection_counts": dict(self.rejection_counts)}


def strategy_version() -> StrategyVersion:
    return StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY,
                           ParameterSchema("price-action-engulfing-continuation-v1",
                                           {name: {"required": True} for name in RESEARCH_PARAMETER_VALUES}),
                           "RESEARCH")


def parameter_set() -> ParameterSet:
    return ParameterSet("price-action-engulfing-continuation-v1-experimental", STRATEGY_VERSION_ID,
                        "price-action-engulfing-continuation-v1", RESEARCH_PARAMETER_VALUES,
                        {"status": "EXPERIMENTAL", "validated": False})
