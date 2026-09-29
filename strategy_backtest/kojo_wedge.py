"""Research-only deterministic evaluator for KOJO_WEDGE_V1.

The evaluator deliberately consumes completed H1 ``MarketEvent`` values only.
Pivot confirmation, fitted geometry, target selection, and the next-open entry
are all derived from the event prefix available at each call; no future data is
consulted or retained outside the restorable evaluator state.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import EntrySignal, MarketEvent, ParameterSet, SetupLifecycleEvent, StrategyVersion, fingerprint


STRATEGY_ID = "KOJO_WEDGE"
VERSION = "V1"
EVALUATOR_KEY = "KOJO_WEDGE_V1"
INSTRUMENT = "XAUUSD"
TIMEFRAME = "H1"
REJECTION_CODES = (
    "INSUFFICIENT_PIVOTS", "NON_CONVERGING", "NO_BREAKOUT",
    "OPPOSITE_BOUNDARY_INVALIDATION", "STALE_WEDGE", "NO_STRUCTURAL_TARGET",
    "INVALID_STOP_TARGET_GEOMETRY", "NEXT_BAR_ENTRY_UNAVAILABLE",
)


@dataclass(frozen=True)
class Pivot:
    kind: str
    index: int
    timestamp: int
    price: float

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _line(first: Pivot, last: Pivot) -> tuple[float, float]:
    if last.index == first.index:
        return 0.0, first.price
    slope = (last.price - first.price) / (last.index - first.index)
    return slope, first.price - slope * first.index


def _at(line: tuple[float, float], index: int) -> float:
    return line[0] * index + line[1]


def _event_evidence(event: dict[str, Any]) -> dict[str, Any]:
    """Return source-independent candle evidence for replay/parity artifacts."""
    return {
        key: event[key]
        for key in (
            "canonical_instrument", "timeframe", "open_timestamp", "close_timestamp",
            "open", "high", "low", "close", "completed",
        )
    }


class KojoWedgeEvaluator:
    VERSION = "KOJO_WEDGE_V1_EVALUATOR"

    def __init__(self) -> None:
        self.strategy_version: StrategyVersion | None = None
        self.parameters: ParameterSet | None = None
        self.events: list[dict[str, Any]] = []
        self.pivots: list[Pivot] = []
        self.next_pivot_center = 0
        self.candidate: dict[str, Any] | None = None
        self.consumed_candidate_ids: set[str] = set()
        self.emitted_lifecycle: set[tuple[str, str]] = set()
        self.rejections: dict[str, int] = {code: 0 for code in REJECTION_CODES}

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        if strategy_version.strategy_version_id != f"{STRATEGY_ID}@{VERSION}":
            raise ValueError("KOJO_WEDGE evaluator requires KOJO_WEDGE@V1")
        if parameter_set.values.get("instrument", INSTRUMENT) != INSTRUMENT:
            raise ValueError("KOJO_WEDGE_V1 is XAUUSD-only")
        if parameter_set.values.get("timeframe", TIMEFRAME) != TIMEFRAME:
            raise ValueError("KOJO_WEDGE_V1 is H1-only")
        kojo_wedge_parameter_schema().validate(parameter_set.values)
        if parameter_set.values.get("stop_buffer_type") != "PRICE":
            raise ValueError("KOJO_WEDGE_V1 requires a PRICE stop buffer")
        self.strategy_version = strategy_version
        self.parameters = parameter_set

    @property
    def values(self) -> dict[str, Any]:
        if self.parameters is None:
            raise RuntimeError("evaluator is not initialized")
        return dict(self.parameters.values)

    def _reject(self, code: str) -> None:
        self.rejections[code] = self.rejections.get(code, 0) + 1

    def _pivot_at(self, index: int) -> None:
        strength = int(self.values["pivot_strength"])
        if index < strength or index + strength >= len(self.events):
            return
        center = self.events[index]
        left = self.events[index - strength:index]
        right = self.events[index + 1:index + strength + 1]
        highs = [float(x["high"]) for x in left + right]
        lows = [float(x["low"]) for x in left + right]
        high = float(center["high"])
        low = float(center["low"])
        if high > max(highs):
            self.pivots.append(Pivot("HIGH", index, int(center["close_timestamp"]), high))
        if low < min(lows):
            self.pivots.append(Pivot("LOW", index, int(center["close_timestamp"]), low))
        self.pivots.sort(key=lambda pivot: (pivot.index, pivot.kind))

    def _alternating_suffix(self) -> list[Pivot] | None:
        required = int(self.values["minimum_pivots"])
        # Use the latest valid fixed-size window. Older pivots remain available
        # for structural targets but cannot silently enlarge the active wedge.
        first_start = max(0, len(self.pivots) - 12)
        for start in range(len(self.pivots) - required, first_start - 1, -1):
            suffix = self.pivots[start:start + required]
            if any(a.kind == b.kind for a, b in zip(suffix, suffix[1:])):
                continue
            if sum(p.kind == "HIGH" for p in suffix) < int(self.values["minimum_high_pivots"]):
                continue
            if sum(p.kind == "LOW" for p in suffix) < int(self.values["minimum_low_pivots"]):
                continue
            return suffix
        return None

    def _candidate_from_pivots(self) -> tuple[dict[str, Any] | None, str | None]:
        pivots = self._alternating_suffix()
        if pivots is None:
            return None, "INSUFFICIENT_PIVOTS"
        highs = [p for p in pivots if p.kind == "HIGH"]
        lows = [p for p in pivots if p.kind == "LOW"]
        upper = _line(highs[0], highs[-1])
        lower = _line(lows[0], lows[-1])
        start = pivots[0].index
        end = pivots[-1].index
        start_width = _at(upper, start) - _at(lower, start)
        end_width = _at(upper, end) - _at(lower, end)
        if start_width <= 0 or end_width <= 0:
            return None, "NON_CONVERGING"
        convergence = (start_width - end_width) / start_width
        if convergence < float(self.values["convergence_threshold"]):
            return None, "NON_CONVERGING"
        upper_slope, lower_slope = upper[0], lower[0]
        if not ((upper_slope < 0 and lower_slope < 0) or (upper_slope > 0 and lower_slope > 0)):
            return None, "NON_CONVERGING"
        direction = "LONG" if upper_slope < 0 and lower_slope < 0 else "SHORT"
        payload = {
            "pivots": [p.payload() for p in pivots], "upper": upper, "lower": lower,
            "wedge_start_index": start, "wedge_end_index": end,
            "start_width": start_width, "end_width": end_width,
            "convergence": convergence, "direction": direction,
        }
        candidate_id = "KOJO_CAND_" + fingerprint({"strategy": STRATEGY_ID, "version": VERSION,
                                                       "parameter_set": self.parameters.fingerprint,
                                                       "geometry": payload})[:24]
        return {"candidate_id": candidate_id, **payload, "state": "WEDGE_CANDIDATE"}, None

    def _lifecycle(self, candidate: dict[str, Any], status: str, index: int, **extra: Any) -> SetupLifecycleEvent:
        key = (candidate["candidate_id"], status)
        self.emitted_lifecycle.add(key)
        provenance = {"candidate": dict(candidate), "available_through_index": index, "parameter_set_fingerprint": self.parameters.fingerprint, **extra}
        return SetupLifecycleEvent(candidate["candidate_id"], self.strategy_version.strategy_version_id, INSTRUMENT, status, int(self.events[index]["close_timestamp"]), provenance)

    def _target(self, candidate: dict[str, Any], entry: float, breakout_index: int) -> tuple[Pivot | None, float | None]:
        start = candidate["wedge_start_index"]
        upper, lower = tuple(candidate["upper"]), tuple(candidate["lower"])
        candidates = []
        for pivot in self.pivots:
            if pivot.index >= breakout_index or pivot.kind != ("HIGH" if candidate["direction"] == "LONG" else "LOW"):
                continue
            boundary = _at(upper if pivot.kind == "HIGH" else lower, pivot.index)
            outside = pivot.index < start or (pivot.price > boundary if pivot.kind == "HIGH" else pivot.price < boundary)
            if outside and ((candidate["direction"] == "LONG" and pivot.price > entry) or (candidate["direction"] == "SHORT" and pivot.price < entry)):
                candidates.append(pivot)
        if not candidates:
            return None, None
        target = min(candidates, key=lambda p: abs(p.price - entry))
        return target, target.price

    def _entry_levels(self, candidate: dict[str, Any], breakout_index: int, entry: float) -> tuple[float, float, Pivot | None, Pivot | None, str | None]:
        direction = candidate["direction"]
        inside = [p for p in self.pivots if candidate["wedge_start_index"] <= p.index < breakout_index]
        stop_kind = "LOW" if direction == "LONG" else "HIGH"
        anchors = [p for p in inside if p.kind == stop_kind]
        if not anchors:
            return 0.0, 0.0, None, None, "INVALID_STOP_TARGET_GEOMETRY"
        stop_anchor = anchors[-1]
        buffer_value = float(self.values["stop_buffer_value"])
        stop = stop_anchor.price - buffer_value if direction == "LONG" else stop_anchor.price + buffer_value
        target_anchor, target = self._target(candidate, entry, breakout_index)
        if target is None:
            return stop, 0.0, stop_anchor, None, "NO_STRUCTURAL_TARGET"
        if not (stop < entry < target if direction == "LONG" else target < entry < stop):
            return stop, target, stop_anchor, target_anchor, "INVALID_STOP_TARGET_GEOMETRY"
        return stop, target, stop_anchor, target_anchor, None

    def consume_market_event(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        if event.canonical_instrument != INSTRUMENT or event.timeframe != TIMEFRAME or not event.completed:
            return ()
        self.events.append(asdict(event))
        index = len(self.events) - 1
        strength = int(self.values["pivot_strength"])
        center = index - strength
        if center >= self.next_pivot_center:
            self._pivot_at(center)
            self.next_pivot_center = center + 1
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        if self.candidate is None:
            candidate, rejection = self._candidate_from_pivots()
            if candidate and candidate["candidate_id"] not in self.consumed_candidate_ids:
                self.candidate = candidate
                outputs.append(self._lifecycle(candidate, "WEDGE_CANDIDATE", index))
            elif rejection:
                self._reject(rejection)
            return tuple(outputs)
        candidate = self.candidate
        if candidate["state"] == "ENTRY_PENDING_NEXT_OPEN":
            if index == candidate["breakout_index"] + 1:
                entry = float(self.events[index]["open"])
                stop, target, stop_anchor, target_anchor, rejection = self._entry_levels(candidate, index, entry)
                if rejection:
                    self._reject(rejection)
                    candidate["state"] = "CONSUMED"
                    self.consumed_candidate_ids.add(candidate["candidate_id"])
                    outputs.append(self._lifecycle(candidate, "INVALIDATED", index, reason=rejection))
                    self.candidate = None
                    return tuple(outputs)
                signal_id = "KOJO_SIG_" + fingerprint({"candidate_id": candidate["candidate_id"], "entry_index": index, "entry": entry})[:24]
                provenance = {"candidate_id": candidate["candidate_id"], "pivots": candidate["pivots"], "upper": candidate["upper"], "lower": candidate["lower"], "wedge_start_index": candidate["wedge_start_index"], "wedge_end_index": candidate["wedge_end_index"], "start_width": candidate["start_width"], "end_width": candidate["end_width"], "convergence": candidate["convergence"], "breakout_candle": _event_evidence(self.events[candidate["breakout_index"]]), "entry_candle": _event_evidence(self.events[index]), "stop_anchor": stop_anchor.payload(), "target_anchor": target_anchor.payload(), "strategy_version": self.strategy_version.strategy_version_id, "parameter_set_fingerprint": self.parameters.fingerprint, "available_through": self.events[index]["close_timestamp"], "timeframe": TIMEFRAME}
                candidate["state"] = "ENTERED"
                outputs.append(self._lifecycle(candidate, "ENTERED", index, entry=entry, stop=stop, target=target))
                # A successful next-bar entry consumes the economic wedge
                # opportunity.  Keep the identity in restorable state and
                # clear the active candidate so later boundary crossings
                # cannot chase the same wedge.
                candidate["state"] = "CONSUMED"
                self.consumed_candidate_ids.add(candidate["candidate_id"])
                self.candidate = None
                outputs.append(EntrySignal(signal_id, self.strategy_version.strategy_version_id, INSTRUMENT, candidate["direction"], entry, stop, target, int(self.events[candidate["breakout_index"]]["close_timestamp"]), provenance=provenance))
                return tuple(outputs)
            if index > candidate["breakout_index"] + 1:
                self._reject("NEXT_BAR_ENTRY_UNAVAILABLE")
                self.consumed_candidate_ids.add(candidate["candidate_id"])
                outputs.append(self._lifecycle(candidate, "CONSUMED", index, reason="NEXT_BAR_ENTRY_UNAVAILABLE"))
                self.candidate = None
            return tuple(outputs)
        age = index - candidate["wedge_start_index"]
        if age > int(self.values["max_wedge_age_bars"]):
            self._reject("STALE_WEDGE")
            self.consumed_candidate_ids.add(candidate["candidate_id"])
            outputs.append(self._lifecycle(candidate, "EXPIRED", index, reason="STALE_WEDGE"))
            self.candidate = None
            return tuple(outputs)
        upper = _at(tuple(candidate["upper"]), index)
        lower = _at(tuple(candidate["lower"]), index)
        if (candidate["direction"] == "LONG" and float(event.close) < lower) or (candidate["direction"] == "SHORT" and float(event.close) > upper):
            self._reject("OPPOSITE_BOUNDARY_INVALIDATION")
            self.consumed_candidate_ids.add(candidate["candidate_id"])
            outputs.append(self._lifecycle(candidate, "INVALIDATED", index, reason="OPPOSITE_BOUNDARY_INVALIDATION"))
            self.candidate = None
            return tuple(outputs)
        breakout = (candidate["direction"] == "LONG" and float(event.close) > upper) or (candidate["direction"] == "SHORT" and float(event.close) < lower)
        if breakout:
            candidate["breakout_index"] = index
            candidate["state"] = "ENTRY_PENDING_NEXT_OPEN"
            outputs.append(self._lifecycle(candidate, "BREAKOUT_CONFIRMED", index, breakout_candle=_event_evidence(self.events[index])))
        return tuple(outputs)

    def snapshot_state(self) -> dict[str, Any]:
        return {"events": list(self.events), "pivots": [p.payload() for p in self.pivots], "next_pivot_center": self.next_pivot_center, "candidate": self.candidate, "consumed_candidate_ids": sorted(self.consumed_candidate_ids), "emitted_lifecycle": [list(x) for x in sorted(self.emitted_lifecycle)], "rejections": dict(self.rejections)}

    def restore_state(self, state: dict[str, Any]) -> None:
        self.events = list(state.get("events", []))
        self.pivots = [Pivot(**x) for x in state.get("pivots", [])]
        self.next_pivot_center = int(state.get("next_pivot_center", 0))
        self.candidate = state.get("candidate")
        self.consumed_candidate_ids = set(state.get("consumed_candidate_ids", []))
        self.emitted_lifecycle = {tuple(x) for x in state.get("emitted_lifecycle", [])}
        self.rejections = dict(state.get("rejections", {}))

    def diagnostics(self) -> dict[str, Any]:
        return {"rejection_counts": dict(self.rejections), "confirmed_pivots": [p.payload() for p in self.pivots], "emitted_candidate_ids": sorted(self.consumed_candidate_ids)}


def kojo_wedge_diagnostic_artifact(signal: EntrySignal) -> dict[str, Any]:
    """Return the inspectable, source-independent artifact for one signal."""
    return {
        "artifact_type": "KOJO_WEDGE_V1_SIGNAL_DIAGNOSTIC",
        "signal": signal.identity_payload(),
        "geometry": {
            key: signal.provenance[key]
            for key in ("pivots", "upper", "lower", "wedge_start_index", "wedge_end_index", "start_width", "end_width", "convergence")
        },
        "breakout_candle": signal.provenance["breakout_candle"],
        "entry_candle": signal.provenance["entry_candle"],
        "stop_anchor": signal.provenance["stop_anchor"],
        "target_anchor": signal.provenance["target_anchor"],
    }


def write_kojo_wedge_diagnostic_artifact(signal: EntrySignal, root: str | Path) -> Path:
    """Persist one research diagnostic; never touches production state."""
    import json

    path = Path(root) / "kojo_wedge_v1" / signal.signal_id
    path.mkdir(parents=True, exist_ok=True)
    artifact = path / "diagnostic.json"
    artifact.write_text(json.dumps(kojo_wedge_diagnostic_artifact(signal), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return artifact


def kojo_wedge_parameter_schema():
    from .models import ParameterSchema
    return ParameterSchema("kojo-wedge-v1", {
        "pivot_strength": {"required": True, "minimum": 1, "maximum": 20},
        "minimum_pivots": {"required": True, "minimum": 4, "maximum": 20},
        "minimum_high_pivots": {"required": True, "minimum": 2, "maximum": 10},
        "minimum_low_pivots": {"required": True, "minimum": 2, "maximum": 10},
        "convergence_threshold": {"required": True, "minimum": 0.0, "maximum": 1.0},
        "max_wedge_age_bars": {"required": True, "minimum": 4, "maximum": 500},
        "stop_buffer_type": {"required": True, "enum": ["PRICE"]},
        "stop_buffer_value": {"required": True, "minimum": 0.0, "maximum": 1000.0},
    })


def kojo_wedge_baseline_parameter_set():
    from .models import ParameterSet
    return ParameterSet("kojo-wedge-v1-baseline", f"{STRATEGY_ID}@{VERSION}", "kojo-wedge-v1", {
        "pivot_strength": 2, "minimum_pivots": 4, "minimum_high_pivots": 2,
        "minimum_low_pivots": 2, "convergence_threshold": 0.10,
        "max_wedge_age_bars": 80, "stop_buffer_type": "PRICE", "stop_buffer_value": 0.5,
    }, {"source": "KOJO_CHAT_DECISION", "instrument": INSTRUMENT, "timeframe": TIMEFRAME,
        "baseline_rationale": "Small causal pivot baseline; four alternating pivots; 10% compression; 80 H1 bars; fixed 0.5-price structural buffer. Values are deterministic defaults, not performance-selected."})
