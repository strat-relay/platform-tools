"""Research-only Support/Resistance Retest V1.

This module is intentionally separate from the executable signal path.  It consumes
completed ``MarketEvent`` values and emits canonical ``Evaluation`` records.  A
successful setup is a research proposal, never an ``EntrySignal``.

The functions in this file are pure primitives.  Their identities are pinned in
``PRIMITIVE_MANIFEST``; changing their semantics requires a new identity.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from core.strategies.evaluation import (
    Decision,
    DecisionTrace,
    Evaluation,
    ReasonCode,
    StageResult,
    StageStatus,
    TraceFidelity,
    canonical_hash,
)

from .models import MarketEvent, ParameterSet, StrategyVersion
from .price_action_primitives import (
    Candle, SRLevel, confirmed_swings, construct_levels, engulfing_confirmation,
    opposing_target, structural_stop,
)


STRATEGY_ID = "PRICE_ACTION_SUPPORT_RESISTANCE_RETEST"
VERSION = "V1"
STRATEGY_VERSION_ID = f"{STRATEGY_ID}@{VERSION}"
EVALUATOR_KEY = "PRICE_ACTION_SR_RETEST_V1_RESEARCH"
EVALUATOR_VERSION = "price-action-sr-retest-v1-evaluator-1"
ENGINE_VERSION = "research-sequence-engine-v1"

PRIMITIVE_MANIFEST = {
    "structure.confirmed_swings@1": "strict-left-right-2-confirmed-on-right-close",
    "structure.sr_levels@1": "confirmed-pivot-price-clusters-nearest-selection",
    "price_action.close_break@1": "close-beyond-level-with-tick-tolerance",
    "price_action.retest@1": "subsequent-touch-and-role-hold",
    "price_action.rejection@1": "close-location-and-wick-ratio",
    "price_action.engulfing@1": "strict-body-engulfing",
    "risk.structural_stop@1": "retest-extreme-plus-tick-buffer",
    "risk.opposing_target@1": "nearest-known-opposing-level",
}

RESEARCH_PARAMETER_VALUES = {
    "context_timeframe": "H1",
    "execution_timeframe": "M15",
    "swing_left_bars": 2,
    "swing_right_bars": 2,
    "level_lookback_context_bars": 80,
    "level_tolerance_ticks": 8,
    "break_buffer_ticks": 1,
    "max_retest_bars": 4,
    "allow_retest_confirmation": False,
    "rejection_min_wick_body_ratio": 1.5,
    "rejection_close_location": 0.65,
    "stop_buffer_ticks": 2,
    "minimum_planned_r": 1.5,
    "level_max_age_context_bars": 120,
    "entry_gap_cancel_ticks": 8,
}

PARAMETER_HASH = canonical_hash(RESEARCH_PARAMETER_VALUES)
DEPENDENCY_MANIFEST_HASH = canonical_hash(PRIMITIVE_MANIFEST)


def _reason(code: str, description: str, *, terminal: bool = True) -> ReasonCode:
    # The canonical model's registry is intentionally small and shared with legacy
    # adapters.  These generic codes preserve compatibility while strategy-specific
    # evidence is recorded in stage metadata and provenance.
    from core.strategies.evaluation.models import default_reason_codes

    registry = default_reason_codes()
    known = {item.code: item for item in registry.all()}
    if code in known:
        return known[code]
    return known["NO_VALID_ENTRY_SIGNAL"] if terminal else known["DATA_UNAVAILABLE"]


def close_break(candle: Candle, level: SRLevel, *, direction: str, tolerance: float,
                buffer: float) -> bool:
    if direction == "LONG":
        return level.kind == "RESISTANCE" and candle.close > level.price + tolerance + buffer
    if direction == "SHORT":
        return level.kind == "SUPPORT" and candle.close < level.price - tolerance - buffer
    raise ValueError("direction must be LONG or SHORT")


def retest_interaction(candle: Candle, level: SRLevel, *, direction: str, tolerance: float) -> bool:
    if direction == "LONG":
        return candle.low <= level.price + tolerance and candle.close >= level.price
    if direction == "SHORT":
        return candle.high >= level.price - tolerance and candle.close <= level.price
    raise ValueError("direction must be LONG or SHORT")


def rejection_confirmation(previous: Candle, candle: Candle, *, direction: str,
                           min_wick_body_ratio: float, close_location: float) -> bool:
    body = abs(candle.close - candle.open)
    body = max(body, 1e-12)
    if direction == "LONG":
        wick = min(candle.open, candle.close) - candle.low
        location = (candle.close - candle.low) / max(candle.high - candle.low, 1e-12)
        return candle.close > candle.open and wick / body >= min_wick_body_ratio and location >= close_location
    if direction == "SHORT":
        wick = candle.high - max(candle.open, candle.close)
        location = (candle.high - candle.close) / max(candle.high - candle.low, 1e-12)
        return candle.close < candle.open and wick / body >= min_wick_body_ratio and location >= close_location
    raise ValueError("direction must be LONG or SHORT")


def _event_id(event: MarketEvent) -> str:
    return canonical_hash(event.identity_payload())


class SupportResistanceRetestEvaluator:
    """Single deterministic evaluator for replay and future observation."""

    VERSION = EVALUATOR_VERSION

    def __init__(self) -> None:
        self.strategy_version: StrategyVersion | None = None
        self.parameters: ParameterSet | None = None
        self.events: list[MarketEvent] = []
        self.seen_events: set[str] = set()
        self.emitted_evaluations: set[str] = set()
        self.rejection_counts: dict[str, int] = {}
        self.pending: dict[str, dict[str, Any]] = {}

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        if strategy_version.strategy_version_id != STRATEGY_VERSION_ID:
            raise ValueError(f"evaluator requires {STRATEGY_VERSION_ID}")
        if parameter_set.values != RESEARCH_PARAMETER_VALUES:
            raise ValueError("SR Retest V1 requires the frozen experimental parameter manifest")
        self.strategy_version = strategy_version
        self.parameters = parameter_set

    @property
    def values(self) -> Mapping[str, Any]:
        if self.parameters is None:
            raise RuntimeError("evaluator is not initialized")
        return self.parameters.values

    def _record(self, reason: str) -> None:
        self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1

    def _evaluate_execution_close(self, event: MarketEvent) -> Evaluation | None:
        assert self.strategy_version is not None and self.parameters is not None
        if event.timeframe != self.values["execution_timeframe"]:
            return None
        instrument = event.canonical_instrument
        context_events = [x for x in self.events if x.canonical_instrument == instrument and
                          x.timeframe == self.values["context_timeframe"] and x.close_timestamp <= event.close_timestamp]
        execution_events = [x for x in self.events if x.canonical_instrument == instrument and
                            x.timeframe == self.values["execution_timeframe"] and x.completed]
        if len(context_events) < self.values["swing_left_bars"] + self.values["swing_right_bars"] + 1:
            return self._evaluation(event, instrument, Decision.NO_CANDIDATE, (), "INSUFFICIENT_CONTEXT")
        context = tuple(Candle.from_event(i, x) for i, x in enumerate(context_events))
        current = Candle.from_event(len(execution_events) - 1, event)
        tick_size = float(event.provenance.get("tick_size", 1.0))
        tolerance = tick_size * self.values["level_tolerance_ticks"]
        break_buffer = tick_size * self.values["break_buffer_ticks"]
        swings = confirmed_swings(context, left=self.values["swing_left_bars"], right=self.values["swing_right_bars"])
        levels = construct_levels(swings, as_of=event.close_timestamp, tolerance=tolerance,
                                  lookback=self.values["level_lookback_context_bars"],
                                  max_age=self.values["level_max_age_context_bars"])
        if not levels:
            return self._evaluation(event, instrument, Decision.NO_CANDIDATE, (), "NO_CONFIRMED_LEVEL")
        result = self._scan_setup(tuple(Candle.from_event(i, x) for i, x in enumerate(execution_events)),
                                  current, levels, tick_size)
        extra = result[3] if len(result) > 3 else {}
        pending = extra.pop("_pending", None)
        if pending is not None:
            self.pending[instrument] = pending
        return self._evaluation(event, instrument, result[0], result[1], result[2], extra)

    def _resolve_pending(self, event: MarketEvent) -> Evaluation | None:
        pending = self.pending.pop(event.canonical_instrument, None)
        if pending is None:
            return None
        direction = pending["direction"]
        level = float(pending["level"]["price"])
        entry = float(event.open)
        tick = float(pending["tick"])
        gap = tick * self.values["entry_gap_cancel_ticks"]
        stop = float(pending["stop"])
        target = float(pending["target"])
        valid_side = stop < entry < target if direction == "LONG" else target < entry < stop
        cancelled = ((direction == "LONG" and entry < level - gap) or
                     (direction == "SHORT" and entry > level + gap) or not valid_side)
        stages = tuple(
            stage if isinstance(stage, StageResult) else StageResult(
                stage["stage_id"], StageStatus(stage["status"]), stage.get("primitive_id"),
                stage.get("observed"), stage.get("expected"), stage.get("margin"),
                tuple(stage.get("evidence_times", ())), metadata=stage.get("metadata", {})
            )
            for stage in pending["stages"]
        )
        return self._evaluation(event, event.canonical_instrument, Decision.REJECT,
                                stages,
                                "ENTRY_GAP_CANCELLED" if cancelled else "ENTRY_OBSERVED_RESEARCH_ONLY",
                                {"direction": direction, "entry_open": entry, "stop": stop,
                                 "target": target, "planned_r": pending["planned_r"],
                                 "entry_cancelled": cancelled, "information_cutoff": event.open_timestamp})

    def _scan_setup(self, execution: tuple[Candle, ...], current: Candle, levels: tuple[SRLevel, ...], tick: float):
        tolerance = tick * self.values["level_tolerance_ticks"]
        buffer = tick * self.values["break_buffer_ticks"]
        candidates = []
        for level in levels:
            for direction in ("LONG", "SHORT"):
                breaks = [bar for bar in execution[:-1] if bar.index < current.index and close_break(
                    bar, level, direction=direction, tolerance=tolerance, buffer=buffer)]
                if not breaks:
                    continue
                breakout = breaks[-1]
                subsequent = [bar for bar in execution if breakout.index < bar.index <= current.index]
                if len(subsequent) > self.values["max_retest_bars"]:
                    continue
                for pos, retest in enumerate(subsequent):
                    if not retest_interaction(retest, level, direction=direction, tolerance=tolerance):
                        continue
                    if pos == 0 and not self.values["allow_retest_confirmation"]:
                        continue
                    previous = execution[retest.index - 1] if retest.index > 0 else retest
                    confirmed = rejection_confirmation(previous, retest, direction=direction,
                                                        min_wick_body_ratio=self.values["rejection_min_wick_body_ratio"],
                                                        close_location=self.values["rejection_close_location"]) or \
                        engulfing_confirmation(previous, retest, direction=direction)
                    if not confirmed:
                        continue
                    entry = current.close if current.index == retest.index else None
                    if retest.index != current.index:
                        continue
                    # The next open is deliberately not exposed until the following
                    # execution event.  This evaluation records a pending proposal.
                    stop = structural_stop(retest, direction=direction, buffer=tick * self.values["stop_buffer_ticks"])
                    target_level = opposing_target(levels, direction=direction, entry=retest.close,
                                                   as_of=retest.close_timestamp)
                    if target_level is None:
                        return Decision.REJECT, (StageResult("break", StageStatus.PASS, "price_action.close_break@1"),
                                                 StageResult("retest", StageStatus.PASS, "price_action.retest@1"),
                                                 StageResult("confirmation", StageStatus.PASS, "price_action.rejection@1")), "NO_OPPOSING_TARGET", {}
                    target = target_level.price
                    risk = abs(retest.close - stop)
                    reward = abs(target - retest.close)
                    planned_r = reward / risk if risk else 0.0
                    if planned_r < self.values["minimum_planned_r"]:
                        return Decision.REJECT, (), "INSUFFICIENT_PLANNED_R", {"planned_r": planned_r}
                    stages = (
                        StageResult("break", StageStatus.PASS, "price_action.close_break@1", observed=breakout.payload(), expected=level.payload()),
                        StageResult("retest", StageStatus.PASS, "price_action.retest@1", observed=retest.payload(), expected={"level": level.price}),
                        StageResult("confirmation", StageStatus.PASS, "price_action.rejection@1", observed={"bar": retest.payload(), "entry_open_available_next": True}),
                    )
                    return Decision.REJECT, stages, "RESEARCH_PROPOSAL_ONLY", {"direction": direction, "entry_reference": retest.close, "stop": stop, "target": target, "planned_r": planned_r, "level": level.payload(), "target_level": target_level.payload(), "_pending": {"direction": direction, "level": level.payload(), "stop": stop, "target": target, "planned_r": planned_r, "stages": stages, "tick": tick}}
        return Decision.NO_CANDIDATE, (), "NO_SETUP", {}

    def _evaluation(self, event: MarketEvent, instrument: str, decision: Decision,
                    stages: tuple[StageResult, ...], rejection: str, extra: Mapping[str, Any] | None = None) -> Evaluation:
        assert self.strategy_version is not None and self.parameters is not None
        extra = dict(extra or {})
        candidate_id = canonical_hash({"strategy": STRATEGY_VERSION_ID, "instrument": instrument,
                                       "decision_time": event.close_timestamp, "direction": extra.get("direction"),
                                       "level": extra.get("level")})[:24]
        reason = _reason("NO_VALID_ENTRY_SIGNAL" if rejection != "NO_SETUP" else "NO_CANDIDATE", rejection)
        trace = DecisionTrace(tuple(stages), decision, reason_codes=(reason,), fidelity=TraceFidelity.L3)
        provenance = {
            "research_only": True, "execution_eligible": False,
            "strategy_definition": {"family": "Price Action", "name": "Support/Resistance Retest", "version": VERSION, "status": "RESEARCH"},
            "parameter_values": dict(self.values), "parameter_hash": PARAMETER_HASH,
            "primitive_manifest": PRIMITIVE_MANIFEST, "dependency_manifest_hash": DEPENDENCY_MANIFEST_HASH,
            "evaluator_version": EVALUATOR_VERSION, "engine_version": ENGINE_VERSION,
            "instrument_timeframe_roles": {"context": self.values["context_timeframe"], "execution": self.values["execution_timeframe"]},
            "data_snapshot_id": event.provenance.get("snapshot_id", "synthetic-or-supplied"),
            "information_cutoff": event.close_timestamp, "rejection_reason": rejection, **extra,
        }
        return Evaluation(STRATEGY_ID, instrument, str(event.close_timestamp), decision, trace,
                           direction=extra.get("direction"), strategy_version=STRATEGY_VERSION_ID,
                           parameter_set_id=self.parameters.parameter_set_id, candidate_id=candidate_id,
                           trace_fidelity=TraceFidelity.L3, runtime_version=ENGINE_VERSION,
                           evaluator_version=EVALUATOR_VERSION, provenance=provenance)

    def consume_market_event(self, event: MarketEvent) -> tuple[Evaluation, ...]:
        if not event.completed or event.timeframe not in {self.values.get("context_timeframe", "H1"), self.values.get("execution_timeframe", "M15")}:
            return ()
        event_id = _event_id(event)
        if event_id in self.seen_events:
            return ()
        if self.events and event.close_timestamp < self.events[-1].close_timestamp:
            raise ValueError("out-of-order market event")
        for value in (event.open, event.high, event.low, event.close):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError("invalid OHLC value")
        self.seen_events.add(event_id)
        self.events.append(event)
        outputs: list[Evaluation] = []
        if event.timeframe == self.values.get("execution_timeframe", "M15"):
            pending_evaluation = self._resolve_pending(event)
            if pending_evaluation is not None:
                outputs.append(pending_evaluation)
        evaluation = self._evaluate_execution_close(event)
        if evaluation is not None:
            outputs.append(evaluation)
        unique: list[Evaluation] = []
        for item in outputs:
            if item.evaluation_hash in self.emitted_evaluations:
                continue
            self.emitted_evaluations.add(item.evaluation_hash)
            self._record(item.provenance.get("rejection_reason", "UNKNOWN"))
            unique.append(item)
        return tuple(unique)

    def snapshot_state(self) -> dict[str, Any]:
        pending = {instrument: {**value, "stages": [stage.to_dict() for stage in value["stages"]]}
                   for instrument, value in self.pending.items()}
        return {"events": [asdict(event) for event in self.events], "seen_events": sorted(self.seen_events),
                "emitted_evaluations": sorted(self.emitted_evaluations), "rejection_counts": dict(self.rejection_counts),
                "pending": pending}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self.events = [MarketEvent(**item) for item in state.get("events", [])]
        self.seen_events = set(state.get("seen_events", []))
        self.emitted_evaluations = set(state.get("emitted_evaluations", []))
        self.rejection_counts = dict(state.get("rejection_counts", {}))
        self.pending = dict(state.get("pending", {}))

    def diagnostics(self) -> dict[str, Any]:
        return {"rejection_counts": dict(self.rejection_counts), "parameter_hash": PARAMETER_HASH,
                "dependency_manifest_hash": DEPENDENCY_MANIFEST_HASH, "research_only": True}


def strategy_version() -> StrategyVersion:
    from .models import ParameterSchema
    fields = {name: {"required": True} for name in RESEARCH_PARAMETER_VALUES}
    return StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, ParameterSchema("price-action-sr-retest-v1", fields), "RESEARCH")


def parameter_set() -> ParameterSet:
    return ParameterSet("price-action-sr-retest-v1-experimental", STRATEGY_VERSION_ID,
                        "price-action-sr-retest-v1", RESEARCH_PARAMETER_VALUES,
                        {"status": "EXPERIMENTAL", "validated": False})
