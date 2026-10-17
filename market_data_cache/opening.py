"""Opening-price evidence from the existing collector snapshot; no extra reads."""
import math
from core.strategies.entry_timing import CandleOpenObservation


def opening_evidence(rates, previous, now):
    result = {}
    for tf, response in rates.items():
        rows = response.get('rates', []) if isinstance(response, dict) else []
        if not rows or not isinstance(rows[-1], dict):
            continue
        row = rows[-1]
        timestamp, price = row.get('time'), row.get('open')
        if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or
                not math.isfinite(timestamp) or timestamp % 1 or timestamp > now or
                isinstance(price, bool) or not isinstance(price, (int, float)) or
                not math.isfinite(price) or price <= 0):
            continue
        old = previous.get(tf, {})
        if old.get('open_time') == timestamp and old.get('price') != price:
            # Never silently rewrite an opening observation. Mark unusable until
            # a different candle; the completed-bar revision path is unchanged.
            result[tf] = {'open_time': timestamp, 'revision': True}
            continue
        if old.get('open_time') == timestamp and old.get('revision'):
            result[tf] = old
            continue
        result[tf] = {'open_time': int(timestamp), 'price': float(price),
                      'available_at': old.get('available_at', now) if old.get('open_time') == timestamp else now}
    return result


def read_opening(raw, instrument, provider_symbol, timeframe, *, observed_at):
    from market_data_cache.store import TIMEFRAME_SECONDS
    evidence = (raw.get('opening_observations') or {}).get(timeframe)
    if not evidence or evidence.get('revision'):
        return None
    return CandleOpenObservation(instrument, TIMEFRAME_SECONDS[timeframe],
                                 evidence['open_time'], evidence['price'],
                                 evidence['available_at'], observed_at,
                                 f"MT5:{provider_symbol}:{timeframe}:{evidence['open_time']}:OPEN",
                                 'REDIS_CANONICAL_PROVIDER_OPEN')
