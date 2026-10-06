"""Raw-OHLC adapters for the research intraday variants.

These adapters reuse the parent candle-derived functions.  They accept ordinary
completed M5 ``MarketEvent`` values and derive higher timeframes internally.
No caller-supplied semantic labels are read.  Liquidity market metadata
(spread/contract constraints) is optional at the wire boundary but required by
the unchanged parent liquidity predicate; when absent, the setup is reported as
metadata-blocked rather than reinterpreted.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from context_structure_retrace.config import ResearchTimeframes
from context_structure_retrace.data import CausalReplay
from context_structure_retrace.patterns import detect_patterns
from context_structure_retrace.replay import feature_snapshot
from context_structure_retrace_forward import _geometry, _m5_mechanisms, _spread, make_setup
from liquidity_displacement import LiquidityDisplacementConfig, LiquidityDisplacementStrategy

from .intraday_adapters import CausalMultiTimeframeState, TIMEFRAME_SECONDS
from .models import EntrySignal, MarketEvent, ParameterSet, SetupLifecycleEvent, StrategyVersion, fingerprint


def _bar(event: MarketEvent) -> dict[str, Any]:
    return {
        "time": event.open_timestamp, "open": event.open, "high": event.high,
        "low": event.low, "close": event.close,
        "spread": event.provenance.get("spread_points", 0),
        "tick_volume": event.provenance.get("tick_volume", 0),
    }


def _replay(state: CausalMultiTimeframeState, as_of: int) -> CausalReplay:
    bars: dict[str, list[dict[str, Any]]] = {}
    for timeframe in ("M5", "M15", "H1", "H4"):
        events = state.completed(timeframe, as_of=as_of) if timeframe != "M5" else tuple(e for e in state.events if e.close_timestamp <= as_of)
        bars[timeframe] = [_bar(event) for event in events]
    return CausalReplay(bars)


def _quote_contract(event: MarketEvent) -> tuple[dict[str, float], dict[str, float]]:
    spread = float(event.provenance.get("spread_price", 0.0) or 0.0)
    point = float(event.provenance.get("point", event.provenance.get("tick_size", 0.001)) or 0.001)
    return {"bid": event.close, "ask": event.close + spread}, {"point": point, "tick_size": point, "stops_level": float(event.provenance.get("stops_level", 0.0) or 0.0)}


class _RawBase:
    base_timeframe = "M5"

    def __init__(self) -> None:
        self.strategy_version: StrategyVersion | None = None
        self.parameter_set: ParameterSet | None = None
        self.state = CausalMultiTimeframeState(self.base_timeframe)
        self._seen_events: set[int] = set()

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        strategy_version.validate_parameter_set(parameter_set)
        self.strategy_version = strategy_version
        self.parameter_set = parameter_set

    def _check(self, event: MarketEvent) -> bool:
        if self.strategy_version is None or self.parameter_set is None:
            raise RuntimeError("evaluator is not initialized")
        if event.timeframe != self.base_timeframe or not event.completed:
            return False
        if event.open_timestamp in self._seen_events:
            return False
        self._seen_events.add(event.open_timestamp)
        self.state.append(event)
        return True

    def _expiry(self, decision_timestamp: int) -> int:
        return decision_timestamp + int(self.parameter_set.values["max_hold_minutes"]) * 60

    def snapshot_state(self) -> dict[str, Any]:
        return {"market": self.state.snapshot_state(), "seen_events": sorted(self._seen_events), "adapter": self._snapshot_adapter_state()}

    def restore_state(self, state: dict[str, Any]) -> None:
        self.state.restore_state(state.get("market", {}))
        self._seen_events = set(int(x) for x in state.get("seen_events", []))
        self._restore_adapter_state(state.get("adapter", {}))

    def _snapshot_adapter_state(self) -> dict[str, Any]:
        return {}

    def _restore_adapter_state(self, state: dict[str, Any]) -> None:
        del state


class ContextRawOhlcEvaluator(_RawBase):
    """Context adapter using parent pattern, snapshot, geometry and lifecycle code."""

    VERSION = "CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1_RAW_OHLC_ADAPTER"
    strategy_id = "CONTEXT_STRUCTURE_RETRACE_INTRADAY_V1"
    timeframes = ResearchTimeframes(execution="M15", lower=("M5",), higher=("H1", "H4"))

    def __init__(self) -> None:
        super().__init__()
        self.setups: dict[str, dict[str, Any]] = {}
        self.pattern_ids: set[str] = set()

    def _snapshot_adapter_state(self) -> dict[str, Any]:
        return {"setups": self.setups, "pattern_ids": sorted(self.pattern_ids)}

    def _restore_adapter_state(self, state: dict[str, Any]) -> None:
        self.setups = {str(k): dict(v) for k, v in state.get("setups", {}).items()}
        self.pattern_ids = set(state.get("pattern_ids", []))

    def _entry_geometry(self, setup: dict[str, Any], bar: dict[str, Any], quote: dict[str, Any],
                        contract: dict[str, Any]) -> dict[str, Any]:
        spread = _spread(bar, contract, quote)
        executable = float(setup["entry_level"]) + spread / 2 if setup["direction"] == "LONG" else float(setup["entry_level"]) - spread / 2
        return _geometry(setup["event_bar"], setup["direction"], setup["context_snapshot"], executable, spread,
                         setup["context_snapshot"]["timeframes"]["M15"]["ema_context"].get("atr"))

    def _new_setups(self, event: MarketEvent) -> list[SetupLifecycleEvent]:
        replay = _replay(self.state, event.close_timestamp)
        m15 = replay.bars_by_timeframe.get("M15", [])
        if not m15:
            return []
        snapshot = feature_snapshot(replay, event.canonical_instrument, event.close_timestamp, timeframes=self.timeframes)
        quote, contract = _quote_contract(event)
        outputs = []
        for pattern in detect_patterns(m15, "M15", event.close_timestamp, event.canonical_instrument):
            if pattern["event_id"] in self.pattern_ids:
                continue
            self.pattern_ids.add(pattern["event_id"])
            setup = make_setup(event.canonical_instrument, pattern, m15[-1], snapshot, quote, contract)
            setup["intraday_variant"] = self.strategy_version.strategy_version_id
            setup["status"] = "WAITING_FOR_RETRACE"
            self.setups[setup["setup_id"]] = setup
            outputs.append(SetupLifecycleEvent(setup["setup_id"], self.strategy_version.strategy_version_id, event.canonical_instrument, "SETUP_DETECTED", int(pattern["timestamp"]), {"parent_pattern": pattern["pattern"], "completed_candle_only": True, "available_through": event.close_timestamp}))
        return outputs

    def _process_setups(self, event: MarketEvent) -> list[SetupLifecycleEvent | EntrySignal]:
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        bar = _bar(event)
        quote, contract = _quote_contract(event)
        m5 = [_bar(item) for item in self.state.events]
        for setup in list(self.setups.values()):
            if setup["status"] != "WAITING_FOR_RETRACE" or event.close_timestamp <= int(setup["setup_timestamp"]):
                continue
            direction = setup["direction"]
            if (direction == "LONG" and bar["low"] <= float(setup["event_bar"]["low"])) or (direction == "SHORT" and bar["high"] >= float(setup["event_bar"]["high"])):
                setup["status"] = "INVALIDATED_NO_REENTRY"
                outputs.append(SetupLifecycleEvent(setup["setup_id"], self.strategy_version.strategy_version_id, event.canonical_instrument, "INVALIDATED_NO_REENTRY", event.close_timestamp, {"completed_candle_only": True}))
                continue
            level = float(setup["entry_level"])
            touched = bar["low"] <= level <= bar["high"]
            held = bar["close"] >= level if direction == "LONG" else bar["close"] <= level
            if not (touched and held):
                continue
            spread = _spread(bar, contract, quote)
            executable = level + spread / 2 if direction == "LONG" else level - spread / 2
            geometry = self._entry_geometry(setup, bar, quote, contract)
            if geometry["target_direction_state"] != "TARGET_BEYOND_ENTRY":
                setup["status"] = "NO_REMAINING_TARGET_UNDER_CURRENT_SETUP_GEOMETRY"
                outputs.append(SetupLifecycleEvent(setup["setup_id"], self.strategy_version.strategy_version_id, event.canonical_instrument, setup["status"], event.close_timestamp, {"geometry": geometry, "completed_candle_only": True}))
                continue
            if geometry.get("v2_eligible") is False:
                setup["status"] = "RR_BELOW_MINIMUM"
                outputs.append(SetupLifecycleEvent(setup["setup_id"], self.strategy_version.strategy_version_id,
                                                    event.canonical_instrument, "RR_BELOW_MINIMUM", event.close_timestamp,
                                                    {"completed_candle_only": True, "geometry": geometry,
                                                     "rejection_reason": "RR_BELOW_MINIMUM"}))
                continue
            signal_id = "SIG_" + fingerprint({"setup_id": setup["setup_id"], "decision": event.close_timestamp, "entry": executable, "stop": geometry["stop"], "target": geometry["effective_target"]})[:24]
            setup["status"] = "FILLED"
            provenance = {"setup_id": setup["setup_id"], "parent_entry_semantics": "DEPTH_ONLY", "parent_stop_semantics": "ORIGINATING_SETUP_EXTREME", "parent_target_semantics": "STRUCTURE_CAPPED_EXTENSION", "completed_candle_only": True}
            if geometry.get("v2_eligible"):
                provenance.update({"planned_r": geometry["target_R"], "minimum_required_r": geometry["minimum_required_r"],
                                   "target_source": geometry.get("target_source"), "target_structure": geometry.get("target_structure"),
                                   "rejected_target_candidates": geometry.get("rejected_target_candidates", [])})
            outputs.extend((SetupLifecycleEvent(setup["setup_id"], self.strategy_version.strategy_version_id, event.canonical_instrument, "ENTERED", event.close_timestamp, {"completed_candle_only": True, "entry": executable, "stop": geometry["stop"], "target": geometry["effective_target"]}), EntrySignal(signal_id, self.strategy_version.strategy_version_id, event.canonical_instrument, direction, executable, geometry["stop"], geometry["effective_target"], event.close_timestamp, "MARKET", self._expiry(event.close_timestamp), provenance)))
        return outputs

    def consume_market_event(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        if not self._check(event):
            return ()
        outputs = self._new_setups(event) if event.close_timestamp % TIMEFRAME_SECONDS["M15"] == 0 else []
        outputs.extend(self._process_setups(event))
        return tuple(outputs)


class ContextV2RawOhlcEvaluator(ContextRawOhlcEvaluator):
    """Research/shadow V2: V1 setup lifecycle plus a structural >=1R target gate."""

    VERSION = "CONTEXT_STRUCTURE_RETRACE_V2_RAW_OHLC_RESEARCH"
    strategy_id = "CONTEXT_STRUCTURE_RETRACE_V2"

    def _entry_geometry(self, setup: dict[str, Any], bar: dict[str, Any], quote: dict[str, Any],
                        contract: dict[str, Any]) -> dict[str, Any]:
        from context_structure_retrace_v2 import v2_geometry
        spread = _spread(bar, contract, quote)
        executable = float(setup["entry_level"]) + spread / 2 if setup["direction"] == "LONG" else float(setup["entry_level"]) - spread / 2
        return v2_geometry(setup["event_bar"], setup["direction"], setup["context_snapshot"], executable, spread,
                           setup["context_snapshot"]["timeframes"]["M15"]["ema_context"].get("atr"))

class LiquidityRawOhlcEvaluator(_RawBase):
    """Liquidity adapter calling the unchanged parent candle predicate causally."""

    VERSION = "LIQUIDITY_DISPLACEMENT_INTRADAY_V1_RAW_OHLC_ADAPTER"
    strategy_id = "LIQUIDITY_DISPLACEMENT_INTRADAY_V1"

    def __init__(self) -> None:
        super().__init__()
        self.strategy = LiquidityDisplacementStrategy(LiquidityDisplacementConfig(max_hold_minutes=1440, max_retrace_candles=3))
        self.setups: dict[str, dict[str, Any]] = {}
        self.scanned: set[int] = set()
        self.blocked_no_metadata = 0

    def _snapshot_adapter_state(self) -> dict[str, Any]:
        return {"setups": self.setups, "scanned": sorted(self.scanned), "blocked_no_metadata": self.blocked_no_metadata}

    def _restore_adapter_state(self, state: dict[str, Any]) -> None:
        self.setups = {str(k): dict(v) for k, v in state.get("setups", {}).items()}
        self.scanned = set(int(x) for x in state.get("scanned", []))
        self.blocked_no_metadata = int(state.get("blocked_no_metadata", 0))

    def consume_market_event(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        if not self._check(event):
            return ()
        m5 = [_bar(item) for item in self.state.events]
        m15 = [_bar(item) for item in self.state.completed("M15", as_of=event.close_timestamp)]
        if len(m5) < 40 or not m15:
            return ()
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        # The unchanged parent predicate identifies a sweep at ``index`` and
        # then needs up to max_structure_break_candles *future* completed bars
        # to observe displacement/MSS.  Do not permanently mark an index as
        # scanned until that causal window is available.
        last_resolvable_index = len(m5) - 1 - self.strategy.config.max_structure_break_candles
        for index in range(40, last_resolvable_index + 1):
            if index in self.scanned:
                continue
            self.scanned.add(index)
            sweep_event = self.state.events[index]
            quote, contract = _quote_contract(sweep_event)
            if quote["ask"] <= quote["bid"]:
                self.blocked_no_metadata += 1
                continue
            candidate = self.strategy.find_candidate(m15, m5, index, quote, contract, datetime.fromtimestamp(sweep_event.close_timestamp, timezone.utc).isoformat())
            if not candidate:
                continue
            setup_id = "LQSETUP_" + fingerprint({"strategy": self.strategy_id, "parameter_set": self.parameter_set.fingerprint, "sweep_time": candidate["sweep_time"] if "sweep_time" in candidate else m5[index]["time"], "direction": candidate["direction"]})[:24]
            if setup_id in self.setups:
                continue
            entry = float(candidate["entry"])
            stop = float(candidate["stop_loss"])
            target = float(candidate.get("next_opposing_level") or 0.0)
            if (candidate["direction"] == "LONG" and not target > entry) or (candidate["direction"] == "SHORT" and not target < entry):
                continue
            setup = {"setup_id": setup_id, "candidate": candidate, "entry": entry, "stop": stop, "target": target, "state": "PENDING_RETRACE", "created_at": event.close_timestamp}
            self.setups[setup_id] = setup
            outputs.append(SetupLifecycleEvent(setup_id, self.strategy_version.strategy_version_id, event.canonical_instrument, "SWEEP_RECLAIM_DISPLACEMENT_MSS", event.close_timestamp, {"completed_candle_only": True, "candidate": candidate}))
        for setup in self.setups.values():
            if setup["state"] != "PENDING_RETRACE":
                continue
            candidate = setup["candidate"]
            start = int(candidate["displacement_index"])
            if len(m5) <= start:
                continue
            for j in range(start + 1, min(len(m5), start + 1 + int(self.parameter_set.values["retracement_window_bars"]))):
                bar = m5[j]
                touched = bar["low"] <= setup["entry"] if candidate["direction"] == "LONG" else bar["high"] >= setup["entry"]
                held = bar["close"] >= setup["entry"] if candidate["direction"] == "LONG" else bar["close"] <= setup["entry"]
                if not (touched and held):
                    continue
                decision = int(bar["time"]) + TIMEFRAME_SECONDS["M5"]
                signal_id = "SIG_" + fingerprint({"setup_id": setup["setup_id"], "decision": decision, "entry": setup["entry"], "stop": setup["stop"], "target": setup["target"]})[:24]
                setup["state"] = "ENTERED"
                outputs.extend((SetupLifecycleEvent(setup["setup_id"], self.strategy_version.strategy_version_id, event.canonical_instrument, "M5_RETRACEMENT_ENTRY", decision, {"completed_candle_only": True}), EntrySignal(signal_id, self.strategy_version.strategy_version_id, event.canonical_instrument, candidate["direction"], setup["entry"], setup["stop"], setup["target"], decision, "MARKET", self._expiry(decision), {"setup_id": setup["setup_id"], "parent_stages": ["SWEEP", "RECLAIM", "DISPLACEMENT", "MSS", "RETRACEMENT"], "target_semantics": "RESEARCH_HYPOTHESIS_STRUCTURAL_OPPOSING_LEVEL", "completed_candle_only": True})))
                break
            else:
                if len(m5) >= start + 1 + int(self.parameter_set.values["retracement_window_bars"]):
                    setup["state"] = "EXPIRED"
        return tuple(outputs)


def register_raw_ohlc_evaluators(registry: Any) -> Any:
    registry.register("context_structure_retrace_intraday_v1", ContextRawOhlcEvaluator)
    registry.register("context_structure_retrace_v2_research", ContextV2RawOhlcEvaluator)
    registry.register("liquidity_displacement_intraday_v1", LiquidityRawOhlcEvaluator)
    return registry
