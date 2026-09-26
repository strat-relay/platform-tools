"""Production-safe Liquidity evaluator boundary.

This module deliberately accepts market snapshots, not paper/forward state files.  It
reuses the frozen Liquidity rule implementation while keeping parameter sets and
instance identity explicit.  It has no broker, order, or bridge-write dependency.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from liquidity_displacement import LiquidityDisplacementConfig, LiquidityDisplacementStrategy
from orchestration.models import StrategySignal, stable_id
from liquidity_market_data import LiveMarketSnapshot

STRATEGY_ID = "LIQUIDITY_DISPLACEMENT_SCALP_V1"
STRATEGY_VERSION = "V1"
_FROZEN_RULE_SOURCE = Path(__file__).resolve().parents[1] / "liquidity_displacement.py"
CODE_FINGERPRINT = hashlib.sha256(_FROZEN_RULE_SOURCE.read_bytes()).hexdigest()


@dataclass(frozen=True)
class LiquidityParameterSet:
    parameter_set_id: str
    instance_id: str
    canonical_instrument: str
    broker_symbol: str
    entry_fraction: float
    max_retrace_candles: int
    target_r: float = 1.25
    max_hold_minutes: int = 120

    @property
    def config_fingerprint(self) -> str:
        payload = {
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "parameter_set_id": self.parameter_set_id,
            "canonical_instrument": self.canonical_instrument,
            "entry_fraction": self.entry_fraction,
            "max_retrace_candles": self.max_retrace_candles,
            "target_r": self.target_r,
            "max_hold_minutes": self.max_hold_minutes,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


PARAMETER_SETS: dict[str, LiquidityParameterSet] = {
    "liquidity-xau-base": LiquidityParameterSet("liquidity-v1-xau-50", "liquidity-xau-base", "XAUUSD", "XAUUSDm", 0.50, 3),
    "liquidity-xau33": LiquidityParameterSet("liquidity-v1-xau-33", "liquidity-xau33", "XAUUSD", "XAUUSDm", 1 / 3, 5),
    "liquidity-btc25": LiquidityParameterSet("liquidity-v1-btc-25", "liquidity-btc25", "BTCUSD", "BTCUSDm", 0.25, 5),
    "liquidity-usdjpy25": LiquidityParameterSet("liquidity-v1-usdjpy-25", "liquidity-usdjpy25", "USDJPY", "USDJPYm", 0.25, 5),
}


class MembershipReader(Protocol):
    def active_instruments(self, *, strategy_id: str, instance_id: str) -> list[dict[str, str]]: ...


class PaperStateRejected(ValueError):
    """A paper/forward artifact was offered to the live evaluator boundary."""


class SetupStateStore(Protocol):
    def load(self, instance_id: str, canonical_instrument: str) -> list[dict[str, Any]]: ...
    def save(self, state: dict[str, Any]) -> None: ...


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _entry_for_fraction(candidate: dict[str, Any], fraction: float) -> float:
    displacement = candidate["displacement_index"]
    # The frozen strategy stores the completed M5 bars used to create the candidate.
    bar = candidate["_m5"][displacement]
    low, high = float(bar["low"]), float(bar["high"])
    return high - (high - low) * fraction if candidate["direction"] == "LONG" else low + (high - low) * fraction


class LiquidityLiveEvaluator:
    """Evaluate one explicit parameter set from a live market snapshot.

    ``snapshot`` must contain completed ``M5``/``M15`` bars, a quote, contract
    metadata, and ``source_kind='LIVE_MARKET'``.  No state-file shape is accepted.
    The signal is emitted only after the strategy's observed retracement fill, which
    preserves the existing paper strategy semantics without inventing a pending-order
    lifecycle that V2 does not currently support.
    """

    def __init__(self, parameter_set: LiquidityParameterSet, *, strategy: LiquidityDisplacementStrategy | None = None):
        self.parameter_set = parameter_set
        self.strategy = strategy or LiquidityDisplacementStrategy(LiquidityDisplacementConfig(
            symbol=parameter_set.broker_symbol,
            target_r=parameter_set.target_r,
            max_hold_minutes=parameter_set.max_hold_minutes,
            max_retrace_candles=parameter_set.max_retrace_candles,
        ))
        self._published: set[str] = set()
        self._setups: dict[str, dict[str, Any]] = {}
        self._next_scan_index = 40

    def restore(self, rows: list[dict[str, Any]]) -> None:
        self._setups = {str(row["setup_id"]): dict(row) for row in rows}
        entered = [row for row in rows if row.get("entry_signal_id")]
        self._published = {str(row["entry_signal_id"]) for row in entered}
        self._next_scan_index = max([int(row.get("scan_index", 40)) for row in rows] or [40])

    def export_state(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._setups.values()]

    def _new_setup(self, candidate: dict[str, Any], *, sweep_index: int) -> dict[str, Any]:
        direction = str(candidate["direction"])
        setup_id = stable_id("LQSETUP", {"instance": self.parameter_set.instance_id,
                                          "parameter_set": self.parameter_set.parameter_set_id,
                                          "sweep_time": int(candidate["sweep_time"]),
                                          "direction": direction})
        entry = float(candidate["entry"])
        stop = float(candidate["stop_loss"])
        target = entry + (entry - stop) * self.parameter_set.target_r if direction == "LONG" else entry - (stop - entry) * self.parameter_set.target_r
        return {"setup_id": setup_id, "instance_id": self.parameter_set.instance_id,
                "parameter_set_id": self.parameter_set.parameter_set_id,
                "canonical_instrument": self.parameter_set.canonical_instrument,
                "direction": direction, "state": "PENDING_RETRACE", "sweep_index": sweep_index,
                "sweep_time": int(candidate["sweep_time"]),
                "displacement_index": int(candidate["displacement_index"]),
                "entry": entry, "stop": stop, "target": target, "risk": abs(entry - stop),
                "scan_index": sweep_index, "entry_signal_id": None,
                "lifecycle": ["SWEEP", "RECLAIM", "DISPLACEMENT", "MSS"]}

    def _signal_for_fill(self, setup: dict[str, Any], *, bar: dict[str, Any], evaluation_time: str,
                         broker_symbol: str) -> StrategySignal:
        event_time = datetime.fromtimestamp(int(bar["time"]) + 300, timezone.utc).isoformat().replace("+00:00", "Z")
        source_event_id = stable_id("LIQUIDITY_EVT", {"setup_id": setup["setup_id"], "entry_time": event_time})
        signal_id = stable_id("SIG", {"strategy_id": STRATEGY_ID, "strategy_version": STRATEGY_VERSION,
                                       "strategy_instance_id": self.parameter_set.instance_id,
                                       "parameter_set_id": self.parameter_set.parameter_set_id,
                                       "source_event_id": source_event_id})
        setup["entry_signal_id"] = signal_id
        setup["state"] = "ENTERED"
        return StrategySignal(
            signal_id=signal_id, schema_version="strategy-signal-v1", strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION, strategy_instance_id=self.parameter_set.instance_id,
            source_event_id=source_event_id, market_event_id=None, setup_id=setup["setup_id"],
            entry_opportunity_id=source_event_id, economic_position_id=None,
            created_at=evaluation_time, signal_timestamp=event_time, symbol=broker_symbol,
            canonical_symbol=self.parameter_set.canonical_instrument, broker_symbol_hint=broker_symbol,
            direction=setup["direction"], entry_type="MARKET", entry_price=setup["entry"],
            stop_price=setup["stop"], target_price=setup["target"], risk_distance=setup["risk"],
            target_distance=abs(setup["target"] - setup["entry"]), target_r=self.parameter_set.target_r,
            timeframe="M5", lower_timeframe=None, higher_timeframes=("M15",),
            entry_mechanism=("LIQUIDITY_SWEEP", "RECLAIM", "DISPLACEMENT", "MICRO_STRUCTURE_SHIFT", "RETRACE_FILL"),
            strategy_metadata={"parameter_set_id": self.parameter_set.parameter_set_id,
                               "entry_fraction": self.parameter_set.entry_fraction,
                               "max_retrace_candles": self.parameter_set.max_retrace_candles},
            provenance={"source": "liquidity_live_market_evaluator", "source_kind": "LIVE_MARKET",
                        "validated_by": "mt5-read-22347", "code_fingerprint": CODE_FINGERPRINT,
                        "config_fingerprint": self.parameter_set.config_fingerprint,
                        "paper_only": False, "setup_lifecycle": "STRATEGY_OBSERVED_FILL"},
            decision_time=event_time, signal_emitted_at=evaluation_time)

    def evaluate(self, snapshot: LiveMarketSnapshot, *, evaluation_time: str) -> StrategySignal | None:
        if not isinstance(snapshot, LiveMarketSnapshot) or snapshot.source_kind != "LIVE_MARKET" or snapshot.validated_by != "mt5-read-22347":
            raise PaperStateRejected("Liquidity live evaluator requires a validated 22347 LiveMarketSnapshot")
        m5 = list(snapshot.M5)
        m15 = list(snapshot.M15)
        quote = snapshot.quote
        contract = snapshot.contract
        if not m5 or not m15 or not quote or not contract:
            return None
        broker_symbol = snapshot.provider_symbol
        signals: list[StrategySignal] = []
        # Scan only bars not previously inspected as sweep candidates.  A candidate is
        # retained and advanced until its retracement window is complete.
        for i in range(max(40, self._next_scan_index), max(40, len(m5) - 1)):
            candidate = self.strategy.find_candidate(m15, m5, i, quote, contract, evaluation_time)
            if not candidate:
                continue
            candidate = dict(candidate)
            candidate["_m5"] = m5
            candidate["entry"] = _entry_for_fraction(candidate, self.parameter_set.entry_fraction)
            candidate["risk"] = (candidate["entry"] - candidate["stop_loss"] if candidate["direction"] == "LONG"
                                  else candidate["stop_loss"] - candidate["entry"])
            if candidate["risk"] <= 0:
                continue
            candidate["sweep_time"] = int(m5[i]["time"])
            setup = self._new_setup(candidate, sweep_index=i)
            self._setups.setdefault(setup["setup_id"], setup)
        self._next_scan_index = max(self._next_scan_index, len(m5) - 1)
        for setup in self._setups.values():
            if setup["state"] != "PENDING_RETRACE":
                continue
            start = int(setup["displacement_index"])
            last = min(len(m5), start + 1 + self.parameter_set.max_retrace_candles)
            for j in range(start + 1, last):
                bar = m5[j]
                touched = float(bar["low"]) <= setup["entry"] if setup["direction"] == "LONG" else float(bar["high"]) >= setup["entry"]
                held = float(bar["close"]) >= setup["entry"] if setup["direction"] == "LONG" else float(bar["close"]) <= setup["entry"]
                if touched and held:
                    signal = self._signal_for_fill(setup, bar=bar, evaluation_time=evaluation_time,
                                                   broker_symbol=broker_symbol)
                    signals.append(signal)
                    self._published.add(signal.signal_id)
                    break
            else:
                if len(m5) >= start + 1 + self.parameter_set.max_retrace_candles:
                    setup["state"] = "EXPIRED"
        return signals[0] if signals else None
