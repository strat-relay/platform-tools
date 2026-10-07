"""Kojo Structure Reclaim V1 — deterministic strategy evaluator.

Research hypothesis (RESEARCH_HYPOTHESIS_FROM_LIVE_OBSERVATION):

  LONG setup:
    1. H1: bullish structural break — completed H1 close above a causally-known resistance level
    2. H1/M15: price pulls back toward the reclaimed structure
    3. M15: sellers fail; retest holds near the level
    4. M15: bullish confirmation candle (BULLISH_ENGULFING | REJECTION_WICK | CONTINUATION_CLOSE)
    5. Entry: MARKET at next M15 open after completed M15 confirmation

  SHORT setup mirrors LONG exactly.

State machine per active setup:
  SETUP_DETECTED
    → WAITING_FOR_RETEST  (H1 bar after break shows price has not immediately failed)
    → RETEST_SEEN         (H1 or M15 shows price returning toward level within tolerance)
    → CONFIRMED           (M15 bullish/bearish confirmation pattern at level)
    → CONSUMED            (terminal — entry signal emitted)

  WAITING_FOR_RETEST → EXPIRED         (> max_retest_wait_h1_bars without retest)
  WAITING_FOR_RETEST → INVALIDATED     (price breaks back through the structural level)
  RETEST_SEEN        → EXPIRED         (> max_confirmation_wait_m15_bars without confirmation)
  RETEST_SEEN        → INVALIDATED     (M15 bar closes through the level in wrong direction)

Causal invariants:
  - Structural levels are confirmed only when pivot_strength bars on BOTH sides are available.
  - Break: completed H1 close only.  Wicks alone are insufficient.
  - Retest: H1 or M15 bar approaching within retest_tolerance_atr * ATR of the level.
  - Confirmation: M15 completed candle only.
  - Signal decision_timestamp = M15 confirmation bar close_timestamp (= next bar's open_timestamp).
  - Entry price = next M15 bar's open (observed before signal emission; same-bar fill).
  - EMA 20/50/100/200: H1 closes, persisted in evidence.  EMA NEVER gates an entry.
  - Stop: structural swing extreme from M15 bars within the retest zone, plus buffer.
  - TP1: nearest pre-entry structural objective in trade direction (H1 confirmed swings).
  - TP2: next structural objective above TP1 (H1), if it exists.

This evaluator is XAUUSD-only, H1 context + M15 confirmation.  It does NOT implement
Kojo discretionary management (partials, runners).  V1 is static entry/exit evaluation.

BROKER_WRITES = 0.  No production state is touched by this evaluator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .models import (
    EntrySignal,
    MarketEvent,
    ParameterSchema,
    ParameterSet,
    SetupLifecycleEvent,
    StrategyVersion,
    fingerprint,
)

# ─── identity ──────────────────────────────────────────────────────────────────

STRATEGY_ID = "KOJO_STRUCTURE_RECLAIM_V1"
VERSION = "V1"
EVALUATOR_KEY = "kojo_structure_reclaim"
INSTRUMENT = "XAUUSD"
TIMEFRAME_H1 = "H1"
TIMEFRAME_M15 = "M15"
H1_SECONDS = 3600
M15_SECONDS = 900

# ─── setup states ──────────────────────────────────────────────────────────────

SETUP_DETECTED = "SETUP_DETECTED"
WAITING_FOR_RETEST = "WAITING_FOR_RETEST"
RETEST_SEEN = "RETEST_SEEN"
CONFIRMED = "CONFIRMED"
CONFIRMED_PENDING_NEXT_OPEN = "CONFIRMED_PENDING_NEXT_OPEN"
CONSUMED = "CONSUMED"
EXPIRED = "EXPIRED"
INVALIDATED = "INVALIDATED"

TERMINAL_STATES = {CONSUMED, EXPIRED, INVALIDATED}

# ─── helpers ───────────────────────────────────────────────────────────────────

def _bar(event: MarketEvent) -> dict[str, Any]:
    return {
        "time": event.open_timestamp,
        "open": event.open,
        "high": event.high,
        "low": event.low,
        "close": event.close,
    }


def _event_evidence(event: MarketEvent) -> dict[str, Any]:
    return {
        "canonical_instrument": event.canonical_instrument,
        "timeframe": event.timeframe,
        "open_timestamp": event.open_timestamp,
        "close_timestamp": event.close_timestamp,
        "open": event.open,
        "high": event.high,
        "low": event.low,
        "close": event.close,
        "completed": event.completed,
    }


def _ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average.  Same algorithm as context_structure_retrace.indicators."""
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1 - alpha) * out[-1])
    return out


def _atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    """Average true range of the most recent bar in the list.  Returns 0.0 if insufficient data."""
    if len(bars) < 2:
        return float(bars[0]["high"]) - float(bars[0]["low"]) if bars else 0.0
    trs: list[float] = []
    previous: float | None = None
    for b in bars:
        high = float(b["high"])
        low = float(b["low"])
        close = float(b["close"])
        if previous is None:
            trs.append(high - low)
        else:
            trs.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    rolling = 0.0
    n = min(period, len(trs))
    for v in trs[-n:]:
        rolling += v
    return rolling / n if n else 0.0


def _ema_snapshot(h1_bars: list[dict[str, Any]]) -> dict[str, Any]:
    """EMA 20/50/100/200 state from H1 closes.  Context/confluence only — never a signal gate."""
    closes = [float(b["close"]) for b in h1_bars]
    result: dict[str, Any] = {}
    for period in (20, 50, 100, 200):
        series = _ema(closes, period)
        last = series[-1] if series else None
        result[str(period)] = last
    if len(closes) >= 2:
        values = [result.get(str(p)) for p in (20, 50, 100, 200)]
        values = [v for v in values if v is not None]
        close = closes[-1]
        result["price_above_ema20"] = close > result["20"] if result.get("20") is not None else None
        result["price_above_ema50"] = close > result["50"] if result.get("50") is not None else None
        result["ema20_above_ema50"] = (
            result["20"] > result["50"]
            if result.get("20") is not None and result.get("50") is not None
            else None
        )
    return result


def _confirmed_swings(bars: list[dict[str, Any]], as_of_close_ts: int, lookback: int) -> list[dict[str, Any]]:
    """Causal confirmed swing pivots from H1 bars.

    A pivot at index i is confirmed when bars[i + lookback] is available AND
    bars[i + lookback].close_timestamp <= as_of_close_ts.

    This is equivalent to context_structure_retrace.sr.confirmed_swings but inlined
    to avoid a hard dependency on that package from the backtest module.
    """
    # Only include bars whose close is fully known as of as_of_close_ts.
    available = [b for b in bars if b["time"] + H1_SECONDS <= as_of_close_ts]
    out: list[dict[str, Any]] = []
    for i in range(lookback, len(available) - lookback):
        # The pivot at i is confirmed only when the bar at i+lookback has closed.
        confirmation_close_ts = available[i + lookback]["time"] + H1_SECONDS
        if confirmation_close_ts > as_of_close_ts:
            continue
        window = available[i - lookback: i + lookback + 1]
        hi = float(available[i]["high"])
        lo = float(available[i]["low"])
        level_ts = available[i]["time"]
        if hi >= max(float(b["high"]) for b in window):
            out.append({
                "type": "RESISTANCE",
                "price": hi,
                "h1_open_timestamp": level_ts,
                "confirmed_at_close_ts": confirmation_close_ts,
                "level_id": f"R-{level_ts}-{hi:.5f}",
            })
        if lo <= min(float(b["low"]) for b in window):
            out.append({
                "type": "SUPPORT",
                "price": lo,
                "h1_open_timestamp": level_ts,
                "confirmed_at_close_ts": confirmation_close_ts,
                "level_id": f"S-{level_ts}-{lo:.5f}",
            })
    return out


# ─── confirmation pattern detection ────────────────────────────────────────────

def _bullish_engulfing(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    """Current candle body engulfs previous bearish body."""
    prev_o, prev_c = float(prev["open"]), float(prev["close"])
    curr_o, curr_c = float(curr["open"]), float(curr["close"])
    bearish_prev = prev_c < prev_o
    bullish_curr = curr_c > curr_o
    return bearish_prev and bullish_curr and curr_o <= prev_c and curr_c >= prev_o


def _bearish_engulfing(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    """Current candle body engulfs previous bullish body."""
    prev_o, prev_c = float(prev["open"]), float(prev["close"])
    curr_o, curr_c = float(curr["open"]), float(curr["close"])
    bullish_prev = prev_c > prev_o
    bearish_curr = curr_c < curr_o
    return bullish_prev and bearish_curr and curr_o >= prev_c and curr_c <= prev_o


def _rejection_wick_bullish(curr: dict[str, Any]) -> bool:
    """Long lower wick, close in upper third of range.  Bullish rejection of lower prices."""
    o, h, lo, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
    body = abs(c - o)
    full_range = h - lo
    if full_range < 1e-9:
        return False
    lower_wick = min(o, c) - lo
    upper_wick = h - max(o, c)
    return (lower_wick >= 2 * body) and (lower_wick >= upper_wick * 2) and (c > o)


def _rejection_wick_bearish(curr: dict[str, Any]) -> bool:
    """Long upper wick, close in lower third of range.  Bearish rejection of upper prices."""
    o, h, lo, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
    body = abs(c - o)
    full_range = h - lo
    if full_range < 1e-9:
        return False
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    return (upper_wick >= 2 * body) and (upper_wick >= lower_wick * 2) and (c < o)


def _continuation_close_bullish(curr: dict[str, Any]) -> bool:
    """Bullish close above midpoint of bar range."""
    h, lo, c = float(curr["high"]), float(curr["low"]), float(curr["close"])
    return c > (h + lo) / 2 and float(curr["close"]) > float(curr["open"])


def _continuation_close_bearish(curr: dict[str, Any]) -> bool:
    """Bearish close below midpoint of bar range."""
    h, lo, c = float(curr["high"]), float(curr["low"]), float(curr["close"])
    return c < (h + lo) / 2 and float(curr["close"]) < float(curr["open"])


def _m15_confirmation_type(m15_bars: list[dict[str, Any]], index: int, direction: str) -> str | None:
    """Return the confirmation pattern type at m15_bars[index], or None."""
    if index < 1 or index >= len(m15_bars):
        return None
    curr = m15_bars[index]
    prev = m15_bars[index - 1]
    if direction == "LONG":
        if _bullish_engulfing(prev, curr):
            return "BULLISH_ENGULFING"
        if _rejection_wick_bullish(curr):
            return "REJECTION_WICK"
        if _continuation_close_bullish(curr):
            return "CONTINUATION_CLOSE"
    else:
        if _bearish_engulfing(prev, curr):
            return "BEARISH_ENGULFING"
        if _rejection_wick_bearish(curr):
            return "REJECTION_WICK"
        if _continuation_close_bearish(curr):
            return "CONTINUATION_CLOSE"
    return None


# ─── evaluator ─────────────────────────────────────────────────────────────────

class KojoStructureReclaimEvaluator:
    """Deterministic, causal evaluator for KOJO_STRUCTURE_RECLAIM_V1.

    Feed must contain H1 and M15 MarketEvents for XAUUSD, chronologically ordered.
    Only completed candles are processed.  The evaluator never reads future data.

    BROKER_WRITES = 0.
    """

    VERSION = "KOJO_STRUCTURE_RECLAIM_V1_EVALUATOR"

    def __init__(self) -> None:
        self.strategy_version: StrategyVersion | None = None
        self.parameters: ParameterSet | None = None
        self._h1_bars: list[dict[str, Any]] = []   # H1 bar dicts, ordered by time
        self._m15_bars: list[dict[str, Any]] = []  # M15 bar dicts, ordered by time
        self._setups: dict[str, dict[str, Any]] = {}   # setup_id → setup dict
        self._consumed_setup_ids: set[str] = set()     # terminal state guard
        self._emitted_lifecycle: set[tuple[str, str]] = set()
        self._rejections: dict[str, int] = {}

    # ── initialization ─────────────────────────────────────────────────────────

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        if strategy_version.strategy_version_id != f"{STRATEGY_ID}@{VERSION}":
            raise ValueError(
                f"KojoStructureReclaimEvaluator requires {STRATEGY_ID}@{VERSION}, "
                f"got {strategy_version.strategy_version_id}"
            )
        schema = kojo_structure_reclaim_parameter_schema()
        schema.validate(parameter_set.values)
        self.strategy_version = strategy_version
        self.parameters = parameter_set

    @property
    def _values(self) -> dict[str, Any]:
        if self.parameters is None:
            raise RuntimeError("evaluator not initialized")
        return dict(self.parameters.values)

    # ── event routing ──────────────────────────────────────────────────────────

    def consume_market_event(
        self, event: MarketEvent
    ) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        if event.canonical_instrument != INSTRUMENT or not event.completed:
            return ()
        if event.timeframe == TIMEFRAME_H1:
            return self._on_h1(event)
        if event.timeframe == TIMEFRAME_M15:
            return self._on_m15(event)
        return ()

    # ── H1 event handler ───────────────────────────────────────────────────────

    def _on_h1(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        self._h1_bars.append(_bar(event))
        index = len(self._h1_bars) - 1
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        current_close_ts = event.close_timestamp

        # Advance existing setups on H1.
        for setup in list(self._setups.values()):
            if setup["state"] in TERMINAL_STATES:
                continue
            adv = self._advance_setup_on_h1(setup, event, index)
            outputs.extend(adv)

        # Detect new structural breaks after advancing existing setups.
        new_outputs = self._detect_h1_breaks(event, index, current_close_ts)
        outputs.extend(new_outputs)

        return tuple(outputs)

    def _detect_h1_breaks(
        self, event: MarketEvent, h1_index: int, close_ts: int
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        """Identify if this H1 bar closes through a known structural level."""
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        if h1_index < 1:
            return outputs

        values = self._values
        pivot_strength = int(values["pivot_strength"])
        current_close = float(event.close)

        # Get all causally-known levels AS OF the previous H1 bar's close.
        # We must NOT use levels that require knowing this bar's data for confirmation.
        prev_close_ts = self._h1_bars[h1_index - 1]["time"] + H1_SECONDS
        levels = _confirmed_swings(self._h1_bars, prev_close_ts, pivot_strength)

        # Deduplicate: skip levels already being tracked by an active setup.
        active_level_ids = {
            s["structural_level_id"]
            for s in self._setups.values()
            if s["state"] not in TERMINAL_STATES
        }

        for level in levels:
            lid = level["level_id"]
            if lid in active_level_ids:
                continue

            # LONG: close breaks above RESISTANCE
            if level["type"] == "RESISTANCE" and current_close > level["price"]:
                setup = self._create_setup(event, h1_index, level, "LONG")
                outputs.extend(self._emit_lifecycle(setup, SETUP_DETECTED, event))
                self._setups[setup["setup_id"]] = setup

            # SHORT: close breaks below SUPPORT
            elif level["type"] == "SUPPORT" and current_close < level["price"]:
                setup = self._create_setup(event, h1_index, level, "SHORT")
                outputs.extend(self._emit_lifecycle(setup, SETUP_DETECTED, event))
                self._setups[setup["setup_id"]] = setup

        return outputs

    def _create_setup(
        self,
        break_event: MarketEvent,
        h1_break_index: int,
        level: dict[str, Any],
        direction: str,
    ) -> dict[str, Any]:
        ema_state = _ema_snapshot(self._h1_bars)
        setup_id = "KSRV1_" + fingerprint({
            "strategy": STRATEGY_ID,
            "version": VERSION,
            "parameter_set_fingerprint": self.parameters.fingerprint,
            "level_id": level["level_id"],
            "break_h1_open_ts": break_event.open_timestamp,
            "direction": direction,
        })[:24]
        return {
            "setup_id": setup_id,
            "direction": direction,
            "structural_level_id": level["level_id"],
            "structural_level_price": level["price"],
            "structural_level_type": level["type"],
            "structure_timeframe": TIMEFRAME_H1,
            "break_timestamp": break_event.close_timestamp,
            "break_direction": direction,
            "break_h1_index": h1_break_index,
            "break_h1_close": float(break_event.close),
            "break_h1_evidence": _event_evidence(break_event),
            "ema_state": ema_state,
            "retest_timestamp": None,
            "retest_h1_index": None,
            "retest_m15_index": None,
            "confirmation_timestamp": None,
            "confirmation_m15_index": None,
            "confirmation_type": None,
            "state": WAITING_FOR_RETEST,
            "stop_basis": None,
            "structural_extreme": None,
            "stop_buffer": float(self._values["stop_buffer_value"]),
            "final_stop": None,
            "tp1": None,
            "tp2": None,
            "intended_entry": None,
            "m15_bars_in_retest": [],   # M15 bars observed during retest phase
            "expiry_h1_index": h1_break_index + int(self._values["max_retest_wait_h1_bars"]),
        }

    def _advance_setup_on_h1(
        self, setup: dict[str, Any], event: MarketEvent, h1_index: int
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        state = setup["state"]
        direction = setup["direction"]
        level_price = float(setup["structural_level_price"])
        values = self._values

        if state == WAITING_FOR_RETEST:
            # Check expiry.
            if h1_index >= setup["expiry_h1_index"]:
                setup["state"] = EXPIRED
                self._consumed_setup_ids.add(setup["setup_id"])
                outputs.extend(self._emit_lifecycle(setup, EXPIRED, event, reason="MAX_RETEST_WAIT_EXCEEDED"))
                return outputs

            # Invalidation: price closes back through the structural level in wrong direction.
            close = float(event.close)
            if direction == "LONG" and close < level_price:
                setup["state"] = INVALIDATED
                self._consumed_setup_ids.add(setup["setup_id"])
                outputs.extend(self._emit_lifecycle(setup, INVALIDATED, event, reason="H1_CLOSE_BELOW_LEVEL"))
                return outputs
            if direction == "SHORT" and close > level_price:
                setup["state"] = INVALIDATED
                self._consumed_setup_ids.add(setup["setup_id"])
                outputs.extend(self._emit_lifecycle(setup, INVALIDATED, event, reason="H1_CLOSE_ABOVE_LEVEL"))
                return outputs

            # Retest check on H1: candle low (LONG) or high (SHORT) approaches within tolerance.
            h1_atr = _atr(self._h1_bars, 14)
            tolerance = float(values["retest_tolerance_atr"]) * h1_atr
            retest = False
            if direction == "LONG":
                retest = (float(event.low) <= level_price + tolerance and
                          float(event.close) >= level_price - tolerance)
            else:
                retest = (float(event.high) >= level_price - tolerance and
                          float(event.close) <= level_price + tolerance)

            if retest:
                setup["retest_timestamp"] = event.close_timestamp
                setup["retest_h1_index"] = h1_index
                setup["state"] = RETEST_SEEN
                # Reset confirmation expiry.
                setup["confirmation_expiry_m15_count"] = int(values["max_confirmation_wait_m15_bars"])
                setup["retest_m15_bars_seen"] = 0
                outputs.extend(self._emit_lifecycle(setup, RETEST_SEEN, event))

        return outputs

    # ── M15 event handler ──────────────────────────────────────────────────────

    def _on_m15(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        self._m15_bars.append(_bar(event))
        m15_index = len(self._m15_bars) - 1
        outputs: list[SetupLifecycleEvent | EntrySignal] = []

        for setup in list(self._setups.values()):
            if setup["state"] in TERMINAL_STATES:
                continue
            adv = self._advance_setup_on_m15(setup, event, m15_index)
            outputs.extend(adv)

        return tuple(outputs)

    def _advance_setup_on_m15(
        self, setup: dict[str, Any], event: MarketEvent, m15_index: int
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        state = setup["state"]
        direction = setup["direction"]
        level_price = float(setup["structural_level_price"])
        values = self._values

        if state == WAITING_FOR_RETEST:
            # Fine-grained retest detection on M15.
            m15_atr = _atr(self._m15_bars, 14)
            tolerance = float(values["retest_tolerance_atr"]) * m15_atr
            retest = False
            if direction == "LONG":
                retest = (float(event.low) <= level_price + tolerance and
                          float(event.close) >= level_price - tolerance)
            else:
                retest = (float(event.high) >= level_price - tolerance and
                          float(event.close) <= level_price + tolerance)
            if retest:
                setup["retest_timestamp"] = event.close_timestamp
                setup["retest_m15_index"] = m15_index
                setup["state"] = RETEST_SEEN
                setup["confirmation_expiry_m15_count"] = int(values["max_confirmation_wait_m15_bars"])
                setup["retest_m15_bars_seen"] = 0
                outputs.extend(self._emit_lifecycle(setup, RETEST_SEEN, event))
            return outputs

        if state == RETEST_SEEN:
            setup["retest_m15_bars_seen"] = setup.get("retest_m15_bars_seen", 0) + 1
            setup.setdefault("m15_bars_in_retest", []).append(m15_index)

            # Expiry: too many M15 bars without confirmation.
            if setup["retest_m15_bars_seen"] > int(values["max_confirmation_wait_m15_bars"]):
                setup["state"] = EXPIRED
                self._consumed_setup_ids.add(setup["setup_id"])
                outputs.extend(
                    self._emit_lifecycle(setup, EXPIRED, event, reason="MAX_CONFIRMATION_WAIT_EXCEEDED")
                )
                return outputs

            # Invalidation: M15 close through the level in the wrong direction.
            if direction == "LONG" and float(event.close) < level_price:
                setup["state"] = INVALIDATED
                self._consumed_setup_ids.add(setup["setup_id"])
                outputs.extend(
                    self._emit_lifecycle(setup, INVALIDATED, event, reason="M15_CLOSE_BELOW_LEVEL_DURING_RETEST")
                )
                return outputs
            if direction == "SHORT" and float(event.close) > level_price:
                setup["state"] = INVALIDATED
                self._consumed_setup_ids.add(setup["setup_id"])
                outputs.extend(
                    self._emit_lifecycle(setup, INVALIDATED, event, reason="M15_CLOSE_ABOVE_LEVEL_DURING_RETEST")
                )
                return outputs

            # Check for M15 confirmation pattern.
            conf_type = _m15_confirmation_type(self._m15_bars, m15_index, direction)
            if conf_type is not None:
                setup["confirmation_timestamp"] = event.close_timestamp
                setup["confirmation_m15_index"] = m15_index
                setup["confirmation_type"] = conf_type
                setup["state"] = CONFIRMED_PENDING_NEXT_OPEN
                outputs.extend(self._emit_lifecycle(setup, CONFIRMED, event, confirmation_type=conf_type))
            return outputs

        if state == CONFIRMED_PENDING_NEXT_OPEN:
            # This is the NEXT M15 bar after confirmation.  Use its open as entry price.
            entry_price = float(event.open)
            decision_ts = setup["confirmation_timestamp"]  # confirmation bar's close_ts

            entry_outputs = self._try_emit_signal(setup, event, m15_index, entry_price, decision_ts)
            if entry_outputs:
                outputs.extend(entry_outputs)
            # If geometry is invalid the setup is invalidated; either way it's done.
            return outputs

        return outputs

    def _try_emit_signal(
        self,
        setup: dict[str, Any],
        event: MarketEvent,
        m15_index: int,
        entry_price: float,
        decision_ts: int,
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        """Compute stop, targets, validate geometry, emit signal or invalidate."""
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        direction = setup["direction"]
        level_price = float(setup["structural_level_price"])
        values = self._values
        buffer = float(values["stop_buffer_value"])

        # Stop: structural swing extreme from M15 retest zone.
        stop_price, stop_basis, structural_extreme = self._compute_stop(
            setup, entry_price, direction, level_price, buffer
        )
        if stop_price is None:
            self._reject("INVALID_STOP_GEOMETRY")
            setup["state"] = INVALIDATED
            self._consumed_setup_ids.add(setup["setup_id"])
            outputs.extend(
                self._emit_lifecycle(setup, INVALIDATED, event, reason="INVALID_STOP_GEOMETRY")
            )
            return outputs

        # Targets: nearest and next H1 structural objectives.
        tp1, tp2 = self._compute_targets(direction, entry_price)
        if tp1 is None:
            self._reject("NO_STRUCTURAL_TARGET")
            setup["state"] = INVALIDATED
            self._consumed_setup_ids.add(setup["setup_id"])
            outputs.extend(
                self._emit_lifecycle(setup, INVALIDATED, event, reason="NO_STRUCTURAL_TARGET")
            )
            return outputs

        # Geometry validation.
        if direction == "LONG":
            valid = stop_price < entry_price < tp1
        else:
            valid = tp1 < entry_price < stop_price
        if not valid:
            self._reject("INVALID_SIGNAL_GEOMETRY")
            setup["state"] = INVALIDATED
            self._consumed_setup_ids.add(setup["setup_id"])
            outputs.extend(
                self._emit_lifecycle(setup, INVALIDATED, event, reason="INVALID_SIGNAL_GEOMETRY")
            )
            return outputs

        # Persist geometry.
        setup["stop_basis"] = stop_basis
        setup["structural_extreme"] = structural_extreme
        setup["final_stop"] = stop_price
        setup["tp1"] = tp1
        setup["tp2"] = tp2
        setup["intended_entry"] = entry_price

        # Build signal.
        signal_id = "KSRV1_SIG_" + fingerprint({
            "setup_id": setup["setup_id"],
            "confirmation_timestamp": setup["confirmation_timestamp"],
            "direction": direction,
            "entry_price": entry_price,
        })[:24]

        provenance = {
            "strategy_id": STRATEGY_ID,
            "strategy_version": self.strategy_version.strategy_version_id,
            "parameter_set_fingerprint": self.parameters.fingerprint,
            "evaluator_version": self.VERSION,
            "timeframe": TIMEFRAME_H1,
            "entry_timeframe": TIMEFRAME_M15,
            "setup_id": setup["setup_id"],
            "structural_level_id": setup["structural_level_id"],
            "structural_level_price": setup["structural_level_price"],
            "structural_level_type": setup["structural_level_type"],
            "structure_timeframe": setup["structure_timeframe"],
            "break_timestamp": setup["break_timestamp"],
            "break_direction": setup["break_direction"],
            "break_h1_evidence": setup["break_h1_evidence"],
            "retest_timestamp": setup["retest_timestamp"],
            "confirmation_timestamp": setup["confirmation_timestamp"],
            "confirmation_type": setup["confirmation_type"],
            "ema_state": setup["ema_state"],
            "stop_basis": stop_basis,
            "structural_extreme": structural_extreme,
            "stop_buffer_value": buffer,
            "final_stop": stop_price,
            "tp1": tp1,
            "tp2": tp2,
            "intended_entry": entry_price,
            "available_through": event.close_timestamp,
        }

        setup["state"] = CONSUMED
        self._consumed_setup_ids.add(setup["setup_id"])
        outputs.extend(self._emit_lifecycle(setup, CONSUMED, event, entry=entry_price))
        outputs.append(EntrySignal(
            signal_id=signal_id,
            strategy_version_id=self.strategy_version.strategy_version_id,
            canonical_instrument=INSTRUMENT,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=tp1,
            decision_timestamp=decision_ts,
            order_type="MARKET",
            provenance=provenance,
        ))
        return outputs

    def _compute_stop(
        self,
        setup: dict[str, Any],
        entry_price: float,
        direction: str,
        level_price: float,
        buffer: float,
    ) -> tuple[float | None, str | None, float | None]:
        """Stop = structural swing extreme from M15 retest zone, minus buffer for LONG."""
        # Collect M15 bars that occurred after the break and before/during retest.
        retest_m15_indices = setup.get("m15_bars_in_retest", [])
        m15_bars_in_zone = [self._m15_bars[i] for i in retest_m15_indices if i < len(self._m15_bars)]

        if direction == "LONG":
            # Stop below the lowest low of M15 bars in the retest zone.
            if m15_bars_in_zone:
                extreme = min(float(b["low"]) for b in m15_bars_in_zone)
                stop = extreme - buffer
                if stop < entry_price:  # valid geometry
                    return stop, "M15_RETEST_ZONE_SWING_LOW", extreme
            # Fallback: use level_price minus buffer.
            stop = level_price - buffer
            if stop < entry_price:
                return stop, "STRUCTURAL_LEVEL_MINUS_BUFFER", level_price
            return None, None, None
        else:
            # Stop above the highest high of M15 bars in the retest zone.
            if m15_bars_in_zone:
                extreme = max(float(b["high"]) for b in m15_bars_in_zone)
                stop = extreme + buffer
                if stop > entry_price:  # valid geometry
                    return stop, "M15_RETEST_ZONE_SWING_HIGH", extreme
            stop = level_price + buffer
            if stop > entry_price:
                return stop, "STRUCTURAL_LEVEL_PLUS_BUFFER", level_price
            return None, None, None

    def _compute_targets(
        self, direction: str, entry_price: float
    ) -> tuple[float | None, float | None]:
        """TP1/TP2: nearest/next H1 structural objectives in trade direction."""
        current_close_ts = self._h1_bars[-1]["time"] + H1_SECONDS if self._h1_bars else 0
        values = self._values
        pivot_strength = int(values["pivot_strength"])
        levels = _confirmed_swings(self._h1_bars, current_close_ts, pivot_strength)

        if direction == "LONG":
            candidates = sorted(
                [lv["price"] for lv in levels if lv["type"] == "RESISTANCE" and lv["price"] > entry_price]
            )
        else:
            candidates = sorted(
                [lv["price"] for lv in levels if lv["type"] == "SUPPORT" and lv["price"] < entry_price],
                reverse=True,
            )

        tp1 = candidates[0] if candidates else None
        tp2 = candidates[1] if len(candidates) > 1 else None
        return tp1, tp2

    # ── lifecycle events ───────────────────────────────────────────────────────

    def _emit_lifecycle(
        self, setup: dict[str, Any], status: str, event: MarketEvent, **extra: Any
    ) -> list[SetupLifecycleEvent]:
        key = (setup["setup_id"], status)
        if key in self._emitted_lifecycle:
            return []
        self._emitted_lifecycle.add(key)
        provenance = {
            "setup_id": setup["setup_id"],
            "structural_level_id": setup["structural_level_id"],
            "structural_level_price": setup["structural_level_price"],
            "direction": setup["direction"],
            "state": setup["state"],
            "parameter_set_fingerprint": self.parameters.fingerprint,
            **extra,
        }
        return [SetupLifecycleEvent(
            setup_id=setup["setup_id"],
            strategy_version_id=self.strategy_version.strategy_version_id,
            canonical_instrument=INSTRUMENT,
            status=status,
            event_timestamp=event.close_timestamp,
            provenance=provenance,
        )]

    def _reject(self, code: str) -> None:
        self._rejections[code] = self._rejections.get(code, 0) + 1

    # ── snapshot / restore (checkpoint/restart parity) ─────────────────────────

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "h1_bars": list(self._h1_bars),
            "m15_bars": list(self._m15_bars),
            "setups": {k: dict(v) for k, v in self._setups.items()},
            "consumed_setup_ids": sorted(self._consumed_setup_ids),
            "emitted_lifecycle": [list(x) for x in sorted(self._emitted_lifecycle)],
            "rejections": dict(self._rejections),
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._h1_bars = list(state.get("h1_bars", []))
        self._m15_bars = list(state.get("m15_bars", []))
        self._setups = {k: dict(v) for k, v in state.get("setups", {}).items()}
        self._consumed_setup_ids = set(state.get("consumed_setup_ids", []))
        self._emitted_lifecycle = {tuple(x) for x in state.get("emitted_lifecycle", [])}
        self._rejections = dict(state.get("rejections", {}))

    def diagnostics(self) -> dict[str, Any]:
        return {
            "rejection_counts": dict(self._rejections),
            "active_setup_count": sum(
                1 for s in self._setups.values() if s["state"] not in TERMINAL_STATES
            ),
            "consumed_setup_ids": sorted(self._consumed_setup_ids),
            "setup_states": {k: v["state"] for k, v in self._setups.items()},
        }


# ─── parameter schema ──────────────────────────────────────────────────────────

def kojo_structure_reclaim_parameter_schema() -> ParameterSchema:
    return ParameterSchema(
        "kojo-structure-reclaim-v1",
        {
            "pivot_strength": {
                "required": True,
                "minimum": 1,
                "maximum": 10,
                "description": "H1 swing pivot lookback bars on each side",
            },
            "retest_tolerance_atr": {
                "required": True,
                "minimum": 0.1,
                "maximum": 3.0,
                "description": "Fraction of ATR within which price must approach the level to count as retest",
            },
            "max_retest_wait_h1_bars": {
                "required": True,
                "minimum": 4,
                "maximum": 100,
                "description": "Max H1 bars to wait for retest after structural break",
            },
            "max_confirmation_wait_m15_bars": {
                "required": True,
                "minimum": 4,
                "maximum": 100,
                "description": "Max M15 bars to wait for confirmation after retest",
            },
            "stop_buffer_type": {
                "required": True,
                "enum": ["PRICE"],
                "description": "Stop buffer type (PRICE only in V1)",
            },
            "stop_buffer_value": {
                "required": True,
                "minimum": 0.0,
                "maximum": 1000.0,
                "description": "Price units beyond structural swing extreme for stop",
            },
        },
    )


def kojo_structure_reclaim_baseline_parameter_set(strategy_version_id: str | None = None) -> ParameterSet:
    """Research hypothesis baseline.  Values are causal deterministic defaults, NOT performance-selected.

    Rationale:
      - pivot_strength=2: same as KOJO_WEDGE_V1 baseline; two confirming bars each side
      - retest_tolerance_atr=0.5: half ATR approach; conservative enough to avoid noise
      - max_retest_wait_h1_bars=24: one calendar day of H1 bars; after that the context is stale
      - max_confirmation_wait_m15_bars=16: 4 hours of M15 bars after retest before expiry
      - stop_buffer_value=1.0: 1 price unit beyond structural extreme; consistent with XAU scale
    """
    sv_id = strategy_version_id or f"{STRATEGY_ID}@{VERSION}"
    return ParameterSet(
        parameter_set_id="kojo-structure-reclaim-v1-baseline",
        strategy_version_id=sv_id,
        schema_id="kojo-structure-reclaim-v1",
        values={
            "pivot_strength": 2,
            "retest_tolerance_atr": 0.5,
            "max_retest_wait_h1_bars": 24,
            "max_confirmation_wait_m15_bars": 16,
            "stop_buffer_type": "PRICE",
            "stop_buffer_value": 1.0,
        },
        provenance={
            "source": "RESEARCH_HYPOTHESIS_FROM_LIVE_OBSERVATION",
            "instrument": INSTRUMENT,
            "context_timeframe": TIMEFRAME_H1,
            "entry_timeframe": TIMEFRAME_M15,
            "baseline_rationale": (
                "Causal deterministic defaults; values chosen before any data was examined. "
                "NOT performance-optimized. PARAMETER_SEARCH=false."
            ),
        },
    )
