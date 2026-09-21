"""Deterministic MT5 paper-trading strategy, risk, and analytics engine.

This module is intentionally independent from the live order tools.  The
paper scheduler accepts market snapshots and can only create simulated
positions; it has no callback or import path to submit real orders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Iterable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candle(c: dict[str, Any], key: str) -> float:
    return float(c[key])


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    if len(values) < 2:
        return [50.0] * len(values)
    gains: list[float] = []
    losses: list[float] = []
    for a, b in zip(values, values[1:]):
        change = b - a
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    out = [50.0]
    for i in range(len(gains)):
        window_g = gains[max(0, i - period + 1): i + 1]
        window_l = losses[max(0, i - period + 1): i + 1]
        avg_g = sum(window_g) / len(window_g)
        avg_l = sum(window_l) / len(window_l)
        out.append(100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l)))
    return out


def atr(candles: list[dict[str, Any]], period: int = 14) -> list[float]:
    if not candles:
        return []
    trs: list[float] = []
    prev_close: float | None = None
    for c in candles:
        high, low, close = _candle(c, "high"), _candle(c, "low"), _candle(c, "close")
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)) if prev_close is not None else high - low)
        prev_close = close
    out: list[float] = []
    for i in range(len(trs)):
        out.append(sum(trs[max(0, i - period + 1): i + 1]) / min(period, i + 1))
    return out


def swings(candles: list[dict[str, Any]], lookback: int = 2) -> tuple[list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    for i in range(lookback, len(candles) - lookback):
        high = _candle(candles[i], "high")
        low = _candle(candles[i], "low")
        if high == max(_candle(x, "high") for x in candles[i - lookback:i + lookback + 1]):
            highs.append(high)
        if low == min(_candle(x, "low") for x in candles[i - lookback:i + lookback + 1]):
            lows.append(low)
    return highs, lows


@dataclass
class Signal:
    symbol: str
    direction: str
    setup_type: str
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    confidence_score: float
    timeframe: str
    reasons: list[str]
    invalidation_reason: str | None
    timestamp: str
    fingerprint: str | None = None

    def json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StrategyConfig:
    symbol: str = "XAUUSDm"
    execution_timeframe: str = "M5"
    risk_pct: float = 0.5
    max_daily_loss_pct: float = 10.0
    max_open_scalps: int = 1
    max_trades_per_session: int = 6
    max_consecutive_losses: int = 3
    cooldown_after_loss_minutes: int = 30
    cooldown_after_consecutive_losses_minutes: int = 120
    max_scalp_minutes: int = 120
    max_spread_price: float = 0.80
    min_atr_price: float = 0.20
    max_atr_price: float = 15.0
    reward_risk_min: float = 1.5
    reward_risk_target: float = 2.0
    atr_buffer_multiplier: float = 1.0
    rsi_confirmation: bool = True
    trailing_distance_price: float | None = None
    break_even_r: float | None = 1.0
    engine_enabled: bool = False
    kill_switch_file: str = "/tmp/mt5-paper-engine.stop"


@dataclass
class SizeResult:
    status: str
    lots: float = 0.0
    risk_dollars: float = 0.0
    stop_distance: float = 0.0
    reason: str | None = None


def size_position(balance: float, entry: float, stop: float, risk_pct: float,
                  tick_size: float, tick_value: float, min_lot: float,
                  max_lot: float, lot_step: float) -> SizeResult:
    distance = abs(entry - stop)
    risk_dollars = balance * risk_pct / 100.0
    if distance <= 0 or tick_size <= 0 or tick_value <= 0:
        return SizeResult("TRADE_SKIPPED_INVALID_CONTRACT", reason="invalid tick or stop distance")
    raw = risk_dollars / ((distance / tick_size) * tick_value)
    if raw < min_lot:
        return SizeResult("TRADE_SKIPPED_MINIMUM_LOT_EXCEEDS_RISK", risk_dollars=risk_dollars, stop_distance=distance, reason=f"safe size {raw:.8f} below minimum {min_lot}")
    lots = min(max_lot, math.floor(raw / lot_step + 1e-12) * lot_step) if lot_step > 0 else raw
    if lots < min_lot:
        return SizeResult("TRADE_SKIPPED_MINIMUM_LOT_EXCEEDS_RISK", risk_dollars=risk_dollars, stop_distance=distance, reason="rounding down would be below minimum lot")
    return SizeResult("OK", round(lots, 8), risk_dollars, distance)


class StrategyEngine:
    def __init__(self, config: StrategyConfig | None = None):
        self.config = config or StrategyConfig()

    def evaluate(self, h1: list[dict[str, Any]], m15: list[dict[str, Any]], m5: list[dict[str, Any]], quote: dict[str, Any], timestamp: str | None = None) -> Signal:
        ts = timestamp or _now()
        empty = Signal(self.config.symbol, "NONE", "NO_TRADE", None, None, None, 0.0, "M5", [], "insufficient_data", ts)
        if len(h1) < 30 or len(m15) < 30 or len(m5) < 25:
            return empty
        closes_h1 = [_candle(c, "close") for c in h1]
        closes_m15 = [_candle(c, "close") for c in m15]
        closes_m5 = [_candle(c, "close") for c in m5]
        atr5 = atr(m5)[-1]
        if atr5 < self.config.min_atr_price or atr5 > self.config.max_atr_price:
            empty.invalidation_reason = "volatility_filter"
            return empty
        spread = float(quote.get("ask", 0)) - float(quote.get("bid", 0))
        if spread <= 0 or spread > self.config.max_spread_price:
            empty.invalidation_reason = "spread_filter"
            return empty
        h1_fast, h1_slow = ema(closes_h1, 20)[-1], ema(closes_h1, 50)[-1]
        m15_fast, m15_slow = ema(closes_m15, 20)[-1], ema(closes_m15, 50)[-1]
        rsi5 = rsi(closes_m5)[-1]
        highs15, lows15 = swings(m15[-30:], 2)
        highs5, lows5 = swings(m5[-20:], 2)
        if not highs5 or not lows5:
            empty.invalidation_reason = "no_recent_swings"
            return empty
        last = m5[-1]
        previous = m5[-2]
        last_close = _candle(last, "close")
        low_level = min(lows5[-3:])
        high_level = max(highs5[-3:])
        bullish_context = h1_fast > h1_slow and m15_fast >= m15_slow
        bearish_context = h1_fast < h1_slow and m15_fast <= m15_slow
        support = min(lows15[-3:]) if lows15 else low_level
        resistance = max(highs15[-3:]) if highs15 else high_level
        bid, ask = float(quote["bid"]), float(quote["ask"])
        long_sweep = _candle(previous, "low") < low_level and last_close > low_level
        short_sweep = _candle(previous, "high") > high_level and last_close < high_level
        long_reclaim = _candle(previous, "close") <= resistance and last_close > resistance
        short_reject = _candle(previous, "close") >= support and last_close < support
        if (bullish_context or long_sweep) and (long_sweep or long_reclaim) and (not self.config.rsi_confirmation or rsi5 >= 50) and ask < resistance:
            structural = low_level if long_sweep else resistance
            sl = structural - max(atr5 * 0.15 * self.config.atr_buffer_multiplier, spread)
            risk = ask - sl
            tp = min(resistance, ask + risk * self.config.reward_risk_target) if resistance > ask else ask + risk * self.config.reward_risk_target
            if tp - ask < risk * min(self.config.reward_risk_min, self.config.reward_risk_target):
                empty.invalidation_reason = "insufficient_reward_risk"
                return empty
            fp = hashlib.sha256(f"{self.config.symbol}|LONG|{('SWEEP' if long_sweep else 'RETEST')}|{structural:.3f}|{m5[-1].get('time')}".encode()).hexdigest()[:16]
            return Signal(self.config.symbol, "LONG", "SWEEP" if long_sweep else "RETEST", ask, sl, tp, 0.75 if long_sweep else 0.68, "M5", ["bullish H1/M15 context" if bullish_context else "support reaction", "M5 recovery close", "RSI14 confirms momentum"], None, ts, fp)
        if (bearish_context or short_sweep) and (short_sweep or short_reject) and (not self.config.rsi_confirmation or rsi5 <= 50) and bid > support:
            structural = high_level if short_sweep else support
            sl = structural + max(atr5 * 0.15 * self.config.atr_buffer_multiplier, spread)
            risk = sl - bid
            tp = max(support, bid - risk * self.config.reward_risk_target) if support < bid else bid - risk * self.config.reward_risk_target
            if bid - tp < risk * min(self.config.reward_risk_min, self.config.reward_risk_target):
                empty.invalidation_reason = "insufficient_reward_risk"
                return empty
            fp = hashlib.sha256(f"{self.config.symbol}|SHORT|{('SWEEP' if short_sweep else 'RETEST')}|{structural:.3f}|{m5[-1].get('time')}".encode()).hexdigest()[:16]
            return Signal(self.config.symbol, "SHORT", "SWEEP" if short_sweep else "RETEST", bid, sl, tp, 0.75 if short_sweep else 0.68, "M5", ["bearish H1/M15 context" if bearish_context else "resistance reaction", "M5 rejection close", "RSI14 confirms momentum"], None, ts, fp)
        empty.invalidation_reason = "no_qualified_setup"
        return empty


@dataclass
class PaperPosition:
    id: str
    signal: dict[str, Any]
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    risk_dollars: float
    opened_at: str
    status: str = "OPEN"
    exit: float | None = None
    exit_reason: str | None = None
    closed_at: str | None = None
    realized_r: float = 0.0


class PaperEngine:
    def __init__(self, config: StrategyConfig | None = None, audit_path: str = "paper_audit.jsonl"):
        self.config = config or StrategyConfig()
        self.positions: list[PaperPosition] = []
        self.closed: list[PaperPosition] = []
        self.trade_fingerprints: set[str] = set()
        self.realized_pnl = 0.0
        self.consecutive_losses = 0
        self.trades_today = 0
        self.last_loss_at: float | None = None
        self.audit_path = Path(audit_path)

    def _audit(self, event: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")

    def approve_signal(self, signal_id: str) -> dict[str, str]:
        """Future manual boundary; deliberately never submits a live order."""
        return {"signal_id": signal_id, "status": "AWAITING_MANUAL_APPROVAL"}

    def evaluate(self, signal: Signal, balance: float, contract: dict[str, float], quote: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        reasons: list[str] = []
        decision = "NO_TRADE"
        size: SizeResult | None = None
        now = now or time.time()
        daily_limit = balance * self.config.max_daily_loss_pct / 100.0
        open_risk = sum(p.risk_dollars for p in self.positions)
        realized_loss = max(0.0, -self.realized_pnl)
        if not self.config.engine_enabled or Path(self.config.kill_switch_file).exists():
            reasons.append("engine_disabled")
        elif signal.direction == "NONE":
            reasons.append(signal.invalidation_reason or "no_trade")
        elif len(self.positions) >= self.config.max_open_scalps:
            reasons.append("max_open_scalps")
        elif self.trades_today >= self.config.max_trades_per_session:
            reasons.append("max_trades_per_session")
        elif self.consecutive_losses >= self.config.max_consecutive_losses:
            reasons.append("max_consecutive_losses")
        elif realized_loss + open_risk >= daily_limit:
            reasons.append("daily_loss_limit")
        elif self.last_loss_at is not None and now - self.last_loss_at < (self.config.cooldown_after_consecutive_losses_minutes if self.consecutive_losses >= 2 else self.config.cooldown_after_loss_minutes) * 60:
            reasons.append("loss_cooldown")
        elif signal.fingerprint and signal.fingerprint in self.trade_fingerprints:
            reasons.append("duplicate_signal")
        else:
            size = size_position(balance, float(signal.entry), float(signal.stop_loss), self.config.risk_pct, contract["tick_size"], contract["tick_value"], contract["min_lot"], contract["max_lot"], contract["lot_step"])
            if size.status != "OK":
                reasons.append(size.status)
            else:
                decision = "PAPER_ENTRY"
                position = PaperPosition(hashlib.sha256(f"{signal.fingerprint}|{signal.timestamp}".encode()).hexdigest()[:16], signal.json(), size.lots, float(signal.entry), float(signal.stop_loss), float(signal.take_profit), size.risk_dollars, signal.timestamp)
                self.positions.append(position)
                self.trade_fingerprints.add(signal.fingerprint or position.id)
                self.trades_today += 1
        result = {"timestamp": _now(), "signal": signal.json(), "decision": decision, "reasons": reasons, "calculated_lot": size.lots if size else 0.0, "risk_dollars": size.risk_dollars if size else 0.0}
        self._audit(result)
        return result

    def process_candle(self, candle: dict[str, Any], now_epoch: float) -> None:
        high, low = float(candle["high"]), float(candle["low"])
        for position in list(self.positions):
            long = position.signal["direction"] == "LONG"
            opened_epoch = datetime.fromisoformat(position.opened_at).timestamp()
            if now_epoch - opened_epoch >= self.config.max_scalp_minutes * 60:
                position.exit = float(candle["close"])
                position.exit_reason = "TIME_EXIT"
                position.closed_at = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat()
                position.realized_r = (position.exit - position.entry) / abs(position.entry - position.stop_loss) if long else (position.entry - position.exit) / abs(position.entry - position.stop_loss)
                position.status = "CLOSED"
                self.positions.remove(position)
                self.closed.append(position)
                self.realized_pnl += position.realized_r * position.risk_dollars
                self._audit({"timestamp": _now(), "decision": "PAPER_EXIT", "position": asdict(position)})
                continue
            if self.config.trailing_distance_price:
                desired = (float(candle["close"]) - self.config.trailing_distance_price) if long else (float(candle["close"]) + self.config.trailing_distance_price)
                if (long and desired > position.stop_loss) or ((not long) and desired < position.stop_loss):
                    position.stop_loss = desired
            hit_sl = low <= position.stop_loss if long else high >= position.stop_loss
            hit_tp = high >= position.take_profit if long else low <= position.take_profit
            if hit_sl or hit_tp:
                # Conservative same-candle handling: assume SL first.
                position.exit = position.stop_loss if hit_sl else position.take_profit
                position.exit_reason = "SL" if hit_sl else "TP"
                position.closed_at = datetime.fromtimestamp(now_epoch, timezone.utc).isoformat()
                position.realized_r = -1.0 if hit_sl else abs(position.exit - position.entry) / abs(position.entry - position.stop_loss)
                position.status = "CLOSED"
                self.positions.remove(position)
                self.closed.append(position)
                if position.realized_r < 0:
                    self.consecutive_losses += 1
                    self.last_loss_at = now_epoch
                else:
                    self.consecutive_losses = 0
                self.realized_pnl += position.realized_r * position.risk_dollars
                self._audit({"timestamp": _now(), "decision": "PAPER_EXIT", "position": asdict(position)})

    def analytics(self) -> dict[str, Any]:
        results = [p.realized_r for p in self.closed]
        wins = [r for r in results if r > 0]
        losses = [r for r in results if r < 0]
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in results:
            equity += r
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return {"trades": len(results), "win_rate": len(wins) / len(results) if results else 0.0, "profit_factor": gross_win / gross_loss if gross_loss else None, "expectancy_r": sum(results) / len(results) if results else 0.0, "average_r": sum(results) / len(results) if results else 0.0, "max_drawdown_r": max_dd, "open_positions": len(self.positions), "trades_per_day": self.trades_today}
