from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from paper_engine import Signal, _candle, atr, ema, rsi, swings


@dataclass
class MicroScalpConfig:
    symbol: str = "XAUUSDm"
    target_r: float = 1.25
    max_spread_to_stop: float = 0.35
    min_atr_price: float = 0.08
    max_atr_price: float = 8.0
    atr_buffer_fraction: float = 0.10
    max_stop_atr_multiple: float = 0.90
    max_hold_minutes: int = 30
    rsi_confirmation: bool = True


class MicroScalpStrategy:
    """Independent completed-M5 micro-sweep/reclaim strategy."""

    def __init__(self, config: MicroScalpConfig | None = None):
        self.config = config or MicroScalpConfig()

    def evaluate(self, m15: list[dict[str, Any]], m5: list[dict[str, Any]], quote: dict[str, Any], contract: dict[str, float], timestamp: str) -> Signal:
        empty = Signal(self.config.symbol, "NONE", "MICRO_NO_TRADE", None, None, None, 0.0, "M5", [], "insufficient_data", timestamp)
        if len(m15) < 30 or len(m5) < 25:
            return empty
        spread = float(quote.get("ask", 0)) - float(quote.get("bid", 0))
        if spread <= 0:
            empty.invalidation_reason = "spread_filter"; return empty
        atr5 = atr(m5)[-1]
        if atr5 < self.config.min_atr_price or atr5 > self.config.max_atr_price:
            empty.invalidation_reason = "volatility_filter"; return empty
        # All levels are confirmed before the latest two candles. The previous
        # candle is the sweep; the latest candle is the completed reclaim.
        prior = m5[-14:-2]
        highs, lows = swings(prior, 1)
        if not highs or not lows:
            empty.invalidation_reason = "no_confirmed_micro_swings"; return empty
        support, resistance = min(lows[-3:]), max(highs[-3:])
        previous, last = m5[-2], m5[-1]
        close, prev_close = _candle(last, "close"), _candle(previous, "close")
        bid, ask = float(quote["bid"]), float(quote["ask"])
        m15_fast, m15_slow = ema([_candle(x, "close") for x in m15], 8)[-1], ema([_candle(x, "close") for x in m15], 21)[-1]
        rsi5 = rsi([_candle(x, "close") for x in m5])[-1]
        broker_min = max(float(contract.get("tick_size", 0.001)), float(contract.get("stops_level", 0)) * float(contract.get("point", contract.get("tick_size", 0.001))))
        long_sweep = _candle(previous, "low") < support and close > support
        short_sweep = _candle(previous, "high") > resistance and close < resistance
        long_wick = (close - _candle(last, "low")) >= abs(close - _candle(last, "open"))
        short_wick = (_candle(last, "high") - close) >= abs(close - _candle(last, "open"))
        if long_sweep and long_wick and (m15_fast >= m15_slow or rsi5 >= 50) and (not self.config.rsi_confirmation or rsi5 >= 50) and ask < resistance:
            structural = min(_candle(previous, "low"), support)
            buffer = max(atr5 * self.config.atr_buffer_fraction, spread * 1.50, broker_min)
            sl = structural - buffer; risk = ask - sl
            opposing = resistance - ask
            if risk <= 0 or risk > atr5 * self.config.max_stop_atr_multiple:
                empty.invalidation_reason = "stop_too_large"; return empty
            if spread / risk > self.config.max_spread_to_stop:
                empty.invalidation_reason = "spread_to_stop_filter"; return empty
            if opposing < risk * self.config.target_r:
                empty.invalidation_reason = "insufficient_opposing_structure"; return empty
            tp = ask + risk * self.config.target_r
            fp = hashlib.sha256(f"{self.config.symbol}|MICRO_SCALP|LONG|{structural:.3f}|{m5[-1].get('time')}".encode()).hexdigest()[:16]
            return Signal(self.config.symbol, "LONG", "MICRO_SCALP_SWEEP_RECLAIM", ask, sl, tp, 0.70, "M5", ["confirmed micro-low sweep", "completed reclaim close", "M15 context", "retest-quality wick"], None, timestamp, fp)
        if short_sweep and short_wick and (m15_fast <= m15_slow or rsi5 <= 50) and (not self.config.rsi_confirmation or rsi5 <= 50) and bid > support:
            structural = max(_candle(previous, "high"), resistance)
            buffer = max(atr5 * self.config.atr_buffer_fraction, spread * 1.50, broker_min)
            sl = structural + buffer; risk = sl - bid
            opposing = bid - support
            if risk <= 0 or risk > atr5 * self.config.max_stop_atr_multiple:
                empty.invalidation_reason = "stop_too_large"; return empty
            if spread / risk > self.config.max_spread_to_stop:
                empty.invalidation_reason = "spread_to_stop_filter"; return empty
            if opposing < risk * self.config.target_r:
                empty.invalidation_reason = "insufficient_opposing_structure"; return empty
            tp = bid - risk * self.config.target_r
            fp = hashlib.sha256(f"{self.config.symbol}|MICRO_SCALP|SHORT|{structural:.3f}|{m5[-1].get('time')}".encode()).hexdigest()[:16]
            return Signal(self.config.symbol, "SHORT", "MICRO_SCALP_SWEEP_RECLAIM", bid, sl, tp, 0.70, "M5", ["confirmed micro-high sweep", "completed reclaim close", "M15 context", "retest-quality wick"], None, timestamp, fp)
        empty.invalidation_reason = "no_micro_scalp_setup"
        return empty
