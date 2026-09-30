"""Translate canonical signal levels into broker-native protective levels.

Canonical strategy prices are reference/chart (Bid-side) prices.  MT5 market
orders enter BUY at Ask and SELL at Bid; a BUY position exits on Bid while a
SELL position exits on Ask.  Therefore a SELL protective level must be shifted
by the submission-time spread so that the broker-side trigger corresponds to
the same canonical reference level.

This module contains no broker I/O and never mutates canonical signal data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


REFERENCE_PRICE_SIDE = "BID"
EXECUTION_PRICING_VERSION = "BROKER_AWARE_BID_REFERENCE_V1"


class ExecutionPricingError(ValueError):
    """The broker cannot safely accept the translated protective levels."""


@dataclass(frozen=True)
class BrokerProtection:
    canonical_stop_price: float
    canonical_target_price: float | None
    broker_stop_price: float
    broker_target_price: float | None
    bid_at_submission: float
    ask_at_submission: float
    spread_at_submission: float
    broker_symbol: str
    reference_price_side: str = REFERENCE_PRICE_SIDE
    adjustment_method: str = EXECUTION_PRICING_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_stop_price": self.canonical_stop_price,
            "canonical_target_price": self.canonical_target_price,
            "broker_stop_price": self.broker_stop_price,
            "broker_target_price": self.broker_target_price,
            "bid_at_submission": self.bid_at_submission,
            "ask_at_submission": self.ask_at_submission,
            "spread_at_submission": self.spread_at_submission,
            "broker_symbol": self.broker_symbol,
            "reference_price_side": self.reference_price_side,
            "execution_pricing_version": self.adjustment_method,
        }


def _tick_round(value: float, tick: float, digits: int, *, up: bool) -> float:
    if tick <= 0:
        return round(value, digits)
    units = value / tick
    rounded = math.ceil(units - 1e-12) if up else math.floor(units + 1e-12)
    return round(rounded * tick, digits)


def broker_protection_levels(*, direction: str, canonical_stop_price: float,
                             canonical_target_price: float | None, broker_symbol: str,
                             bid: float, ask: float, metadata: dict[str, Any]) -> BrokerProtection:
    """Return broker-native SL/TP while preserving canonical prices.

    The canonical/reference side is BID.  SELL exits use ASK, so both SELL
    levels add the observed submission spread.  BUY exits use BID, so BUY
    levels remain at the canonical values.  Rounding is conservative: stops
    round away from the executable market and targets round toward the
    favorable side.
    """
    direction = str(direction).upper()
    if direction not in {"LONG", "SHORT"}:
        raise ExecutionPricingError("UNSUPPORTED_DIRECTION")
    bid, ask = float(bid), float(ask)
    if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid):
        raise ExecutionPricingError("INVALID_BROKER_QUOTE")
    canonical_stop = float(canonical_stop_price)
    canonical_target = None if canonical_target_price is None else float(canonical_target_price)
    if not math.isfinite(canonical_stop) or (canonical_target is not None and not math.isfinite(canonical_target)):
        raise ExecutionPricingError("INVALID_CANONICAL_LEVEL")
    tick = float(metadata.get("tick_size") or 0.0)
    point = float(metadata.get("point") or tick or 0.0)
    digits = int((metadata.get("raw") or {}).get("digits", metadata.get("digits", 8)))
    if tick <= 0:
        raise ExecutionPricingError("BROKER_TICK_SIZE_UNAVAILABLE")
    minimum = max(tick, float(metadata.get("stops_level") or 0) * point,
                  float(metadata.get("freeze_level") or 0) * point)
    spread = ask - bid
    adjustment = spread if direction == "SHORT" else 0.0
    if direction == "LONG":
        broker_stop = _tick_round(canonical_stop, tick, digits, up=False)
        broker_target = (_tick_round(canonical_target, tick, digits, up=True)
                         if canonical_target is not None else None)
        stop_ok = bid - broker_stop >= minimum - 1e-12
        target_ok = broker_target is None or broker_target - bid >= minimum - 1e-12
    else:
        broker_stop = _tick_round(canonical_stop + adjustment, tick, digits, up=True)
        broker_target = (_tick_round(canonical_target + adjustment, tick, digits, up=False)
                         if canonical_target is not None else None)
        stop_ok = broker_stop - ask >= minimum - 1e-12
        target_ok = broker_target is None or ask - broker_target >= minimum - 1e-12
    if not stop_ok or not target_ok:
        raise ExecutionPricingError("BROKER_STOP_DISTANCE_OR_FREEZE_CONSTRAINT")
    return BrokerProtection(canonical_stop, canonical_target, broker_stop, broker_target,
                            bid, ask, spread, broker_symbol)


def executable_exit_reached(*, direction: str, exit_kind: str, canonical_level: float,
                            bid: float, ask: float) -> bool:
    """Trade-manager check for a canonical level on its executable side."""
    if direction == "LONG":
        executable = float(bid)
        return executable >= float(canonical_level) if exit_kind == "TARGET" else executable <= float(canonical_level)
    if direction == "SHORT":
        executable = float(ask)
        return executable <= float(canonical_level) if exit_kind == "TARGET" else executable >= float(canonical_level)
    raise ExecutionPricingError("UNSUPPORTED_DIRECTION")
