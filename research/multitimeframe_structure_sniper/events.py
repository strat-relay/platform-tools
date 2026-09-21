"""Causal structural event extraction for offline research replay."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Sequence

from .engine import (Bar, H1Scenario, build_structure_map, classify_h1_scenario,
                     compression_geometry, confirmed_swings, m15_confirmations,
                     sniper_trigger, support_resistance_levels)


def event_id(kind: str, *parts: object) -> str:
    raw = "|".join([kind, *[str(x) for x in parts]])
    return f"{kind.lower()}_{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


@dataclass(frozen=True)
class ResearchEvent:
    event_id: str
    event_type: str
    symbol: str
    timeframe: str
    direction: str | None
    event_timestamp: datetime
    confirmation_timestamp: datetime
    source_level_id: str | None
    source_swing_ids: tuple[str, ...]
    price: float | None
    candle_index: int
    data_available_through: datetime
    metadata: dict

    def record(self) -> dict:
        return asdict(self)


def _event(kind, symbol, tf, direction, ts, *, level=None, swings=(), price=None, index=0, metadata=None):
    return ResearchEvent(event_id(kind, symbol, tf, ts, level, *swings), kind, symbol, tf, direction, ts, ts,
                         level, tuple(swings), price, index, ts, metadata or {})


def _level_events(series: Sequence[Bar], symbol: str):
    bars = list(series); swings = confirmed_swings(bars, bars[-1].close_timestamp if bars else datetime.min.replace(tzinfo=bars[0].timestamp.tzinfo), 2, 2)
    levels = []
    for s in swings:
        sid = event_id("SWING", symbol, s.timestamp, s.kind, s.price)
        levels.append((sid, s))
    breaks = []; retests = []; holds = []
    for lid, swing in levels:
        for i, b in enumerate(bars):
            if b.timestamp <= swing.timestamp + timedelta(minutes=60 if b.timeframe == "H1" else 5):
                continue
            is_long_break = swing.kind == "HIGH" and b.close > swing.price
            is_short_break = swing.kind == "LOW" and b.close < swing.price
            if not (is_long_break or is_short_break):
                continue
            direction = "LONG" if is_long_break else "SHORT"
            br = _event("LEVEL_BREAK", symbol, b.timeframe, direction, b.timestamp, level=lid, swings=(lid,), price=b.close, index=i,
                         metadata={"level_price": swing.price, "penetration": abs(b.close - swing.price), "break_close": b.close})
            breaks.append(br)
            for j in range(i + 1, len(bars)):
                r = bars[j]
                touched = (direction == "LONG" and r.low <= swing.price and r.close >= swing.price) or (direction == "SHORT" and r.high >= swing.price and r.close <= swing.price)
                if not touched:
                    continue
                rt = _event("LEVEL_RETEST", symbol, r.timeframe, direction, r.timestamp, level=lid, swings=(lid,), price=r.close, index=j,
                             metadata={"break_event_id": br.event_id, "level_price": swing.price, "bars_since_break": j - i, "penetration": abs((r.low if direction == "LONG" else r.high) - swing.price)})
                retests.append(rt)
                hold = (direction == "LONG" and r.close > swing.price and r.close >= r.open) or (direction == "SHORT" and r.close < swing.price and r.close <= r.open)
                if hold:
                    holds.append(_event("LEVEL_HOLD", symbol, r.timeframe, direction, r.timestamp, level=lid, swings=(lid,), price=r.close, index=j,
                                        metadata={"retest_event_id": rt.event_id, "close_location": "ABOVE" if direction == "LONG" else "BELOW", "rejection_distance": abs(r.close - swing.price)}))
                break
            break
    return levels, breaks, retests, holds


def extract_liquidity_events(series: Sequence[Bar], symbol: str):
    bars = list(series); refs = []; sweeps = []; reclaims = []
    swings = confirmed_swings(bars, bars[-1].close_timestamp, 2, 2) if bars else ()
    for s in swings:
        sid = event_id("LIQUIDITY_REFERENCE", symbol, s.timestamp, s.kind, s.price)
        refs.append(_event("LIQUIDITY_REFERENCE", symbol, bars[0].timeframe, "LONG" if s.kind == "LOW" else "SHORT", s.timestamp,
                           level=sid, swings=(sid,), price=s.price, index=s.index, metadata={"reference_type": "SWING_LOW" if s.kind == "LOW" else "SWING_HIGH"}))
        for i in range(s.index + 1, len(bars)):
            b = bars[i]
            swept = (s.kind == "LOW" and b.low < s.price) or (s.kind == "HIGH" and b.high > s.price)
            if not swept:
                continue
            direction = "LONG" if s.kind == "LOW" else "SHORT"
            sw = _event("LIQUIDITY_SWEEP", symbol, b.timeframe, direction, b.timestamp, level=sid, swings=(sid,), price=b.low if direction == "LONG" else b.high, index=i,
                         metadata={"reference_price": s.price, "penetration": abs((b.low if direction == "LONG" else b.high) - s.price)})
            sweeps.append(sw)
            for j in range(i + 1, len(bars)):
                r = bars[j]
                reclaimed = (direction == "LONG" and r.close > s.price) or (direction == "SHORT" and r.close < s.price)
                if reclaimed:
                    reclaims.append(_event("LIQUIDITY_RECLAIM", symbol, r.timeframe, direction, r.timestamp, level=sid, swings=(sid,), price=r.close, index=j,
                                           metadata={"sweep_event_id": sw.event_id, "bars_since_sweep": j - i, "reference_price": s.price}))
                    break
            break
    return refs, sweeps, reclaims


def extract_compression_events(series: Sequence[Bar], symbol: str):
    bars = list(series); states = []; breaks = []; seen = set()
    for i in range(10, len(bars)):
        # Match the existing bounded research context window; this keeps the
        # offline census finite without changing any strategy threshold.
        prefix = bars[max(0, i - 300):i]
        geom = compression_geometry(prefix, bars[i - 1].close_timestamp)
        if not (geom.get("bearish") or geom.get("bullish")):
            continue
        direction = "SHORT" if geom["bearish"] else "LONG"
        level = geom["support"] if direction == "SHORT" else geom["resistance"]
        swings = confirmed_swings(prefix, prefix[-1].close_timestamp, 2, 2)
        swing_ids = tuple(event_id("SWING", symbol, s.timestamp, s.kind, s.price) for s in swings[-4:])
        cid = event_id("COMPRESSION_STATE", symbol, direction, level, swing_ids)
        if cid not in seen:
            states.append(_event("COMPRESSION_STATE", symbol, bars[0].timeframe, direction, prefix[-1].timestamp, level=cid, swings=swing_ids, price=level, index=i - 1,
                                 metadata={"support": geom["support"], "resistance": geom["resistance"], "test_count": geom["support_tests"] if direction == "SHORT" else geom["resistance_tests"], "slope": geom["high_slope_per_minute"] if direction == "SHORT" else geom["low_slope_per_minute"], "width_atr": geom["width_atr"]}))
            seen.add(cid)
        broken = (direction == "SHORT" and bars[i].close < geom["support"]) or (direction == "LONG" and bars[i].close > geom["resistance"])
        if broken:
            breaks.append(_event("COMPRESSION_BREAK", symbol, bars[i].timeframe, direction, bars[i].timestamp, level=cid, swings=swing_ids, price=bars[i].close, index=i,
                                 metadata={"compression_id": cid, "level": level, "penetration": abs(bars[i].close - level)}))
    return states, breaks


def extract_events(series: Sequence[Bar], symbol: str):
    levels, breaks, retests, holds = _level_events(series, symbol)
    refs, sweeps, reclaims = extract_liquidity_events(series, symbol)
    compressions, compression_breaks = extract_compression_events(series, symbol)
    level_events = [_event("STRUCTURE_LEVEL", symbol, series[0].timeframe, "LONG" if s.kind == "LOW" else "SHORT", s.timestamp,
                           level=lid, swings=(lid,), price=s.price, index=s.index, metadata={"level_type": s.kind}) for lid, s in levels]
    all_events = [e for group in (level_events, breaks, retests, holds, refs, sweeps, reclaims, compressions, compression_breaks) for e in group]
    dedup = {e.event_id: e for e in all_events}
    return {"levels": levels, "breaks": breaks, "retests": retests, "holds": holds, "liquidity_references": refs,
            "sweeps": sweeps, "reclaims": reclaims, "compression_states": compressions, "compression_breaks": compression_breaks,
            "all": list(dedup.values())}


def dispatch_m5(series: Sequence[Bar], as_of: datetime, families=None):
    families = families or ("MICRO_BOS", "MICRO_CHOCH", "BREAK_RETEST", "REJECTION_WICK", "ENGULFING", "TOUCH_CLOSE_HOLD", "CLOSE_RECLAIM", "SWEEP_RECLAIM", "MICRO_HIGHER_LOW", "MICRO_LOWER_HIGH")
    results = []
    for direction in ("LONG", "SHORT"):
        for family in families:
            trigger = sniper_trigger(series, as_of, direction, family)
            results.append({"direction": direction, "family": family, "passed": trigger is not None, "trigger": trigger})
    return results
