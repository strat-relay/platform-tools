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

    def evaluate(self, snapshot: dict[str, Any], *, evaluation_time: str) -> StrategySignal | None:
        if snapshot.get("source_kind") != "LIVE_MARKET":
            raise PaperStateRejected("Liquidity live evaluator accepts only source_kind=LIVE_MARKET")
        m5 = list(snapshot.get("M5") or [])
        m15 = list(snapshot.get("M15") or [])
        quote = snapshot.get("quote") or {}
        contract = snapshot.get("contract") or {}
        if not m5 or not m15 or not quote or not contract:
            return None
        broker_symbol = str(snapshot.get("provider_symbol") or self.parameter_set.broker_symbol)
        i = len(m5) - 1
        candidate = self.strategy.find_candidate(m15, m5, i, quote, contract, evaluation_time)
        if not candidate:
            return None
        candidate = dict(candidate)
        candidate["_m5"] = m5
        candidate["entry"] = _entry_for_fraction(candidate, self.parameter_set.entry_fraction)
        candidate["risk"] = (candidate["entry"] - candidate["stop_loss"] if candidate["direction"] == "LONG"
                              else candidate["stop_loss"] - candidate["entry"])
        if candidate["risk"] <= 0:
            return None
        start = int(candidate["displacement_index"])
        fill_index = None
        for j in range(start + 1, min(len(m5), start + 1 + self.parameter_set.max_retrace_candles)):
            bar = m5[j]
            touched = float(bar["low"]) <= candidate["entry"] if candidate["direction"] == "LONG" else float(bar["high"]) >= candidate["entry"]
            held = float(bar["close"]) >= candidate["entry"] if candidate["direction"] == "LONG" else float(bar["close"]) <= candidate["entry"]
            if touched and held:
                fill_index = j
                break
        if fill_index is None:
            return None
        event_time = datetime.fromtimestamp(int(m5[fill_index]["time"]) + 300, timezone.utc).isoformat().replace("+00:00", "Z")
        source_event_id = stable_id("LIQUIDITY_EVT", {
            "instance_id": self.parameter_set.instance_id,
            "parameter_set_id": self.parameter_set.parameter_set_id,
            "setup_id": candidate.get("setup_id") or f"{m5[i]['time']}:{candidate['direction']}",
            "entry_time": event_time,
        })
        signal_id = stable_id("SIG", {
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "strategy_instance_id": self.parameter_set.instance_id,
            "parameter_set_id": self.parameter_set.parameter_set_id,
            "source_event_id": source_event_id,
        })
        if signal_id in self._published:
            return None
        entry = float(candidate["entry"])
        stop = float(candidate["stop_loss"])
        target = entry + (entry - stop) * self.parameter_set.target_r if candidate["direction"] == "LONG" else entry - (stop - entry) * self.parameter_set.target_r
        self._published.add(signal_id)
        return StrategySignal(
            signal_id=signal_id, schema_version="strategy-signal-v1", strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION, strategy_instance_id=self.parameter_set.instance_id,
            source_event_id=source_event_id, market_event_id=None,
            setup_id=str(candidate.get("setup_id") or source_event_id),
            entry_opportunity_id=source_event_id, economic_position_id=None,
            created_at=evaluation_time, signal_timestamp=event_time,
            symbol=broker_symbol, canonical_symbol=self.parameter_set.canonical_instrument,
            broker_symbol_hint=broker_symbol, direction=str(candidate["direction"]),
            entry_type="MARKET", entry_price=entry, stop_price=stop, target_price=target,
            risk_distance=abs(entry - stop), target_distance=abs(target - entry),
            target_r=self.parameter_set.target_r, timeframe="M5", lower_timeframe=None, higher_timeframes=("M15",),
            entry_mechanism=("LIQUIDITY_SWEEP", "RECLAIM", "DISPLACEMENT", "MICRO_STRUCTURE_SHIFT", "RETRACE_FILL"),
            strategy_metadata={"parameter_set_id": self.parameter_set.parameter_set_id, "entry_fraction": self.parameter_set.entry_fraction,
                               "max_retrace_candles": self.parameter_set.max_retrace_candles},
            provenance={"source": "liquidity_live_market_evaluator", "source_kind": "LIVE_MARKET",
                        "code_fingerprint": CODE_FINGERPRINT, "config_fingerprint": self.parameter_set.config_fingerprint,
                        "paper_only": False, "setup_lifecycle": "STRATEGY_OBSERVED_FILL"},
            decision_time=event_time, signal_emitted_at=evaluation_time,
        )
