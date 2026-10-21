"""Freeze a real quote at entry observation; never reconstruct historical bid/ask."""
from core.strategies.entry_timing import finite


def capture_entry_quote(cached, *, instrument, provider_symbol, direction,
                        entry_time, observed_at, max_age_seconds):
    if direction not in {'LONG', 'SHORT'}:
        raise ValueError('entry quote direction required')
    if not finite(max_age_seconds) or not 0 < max_age_seconds <= 15:
        raise ValueError('entry quote freshness must be in (0, 15] seconds')
    if not isinstance(cached, dict) or not isinstance(cached.get('quote'), dict):
        raise ValueError('contemporaneous entry quote unavailable')
    quote = cached['quote']
    bid, ask, provider_at, received_at = (quote.get('bid'), quote.get('ask'),
                                       quote.get('time'), cached.get('observed_at'))
    if not all(finite(x) for x in (bid, ask, provider_at, received_at, entry_time, observed_at)):
        raise ValueError('invalid entry quote values/timestamps')
    if not 0 < bid <= ask or not entry_time <= provider_at <= received_at <= observed_at:
        raise ValueError('invalid entry quote geometry/clock order')
    if observed_at-provider_at > max_age_seconds:
        raise ValueError('stale entry quote')
    if quote.get('symbol', provider_symbol) != provider_symbol:
        raise ValueError('entry quote provider mismatch')
    return {'schema': 'entry-quote.v1', 'instrument': instrument,
            'provider_symbol': provider_symbol, 'source': 'REDIS_HOT_QUOTE',
            'bid': bid, 'ask': ask, 'spread': ask-bid,
            'provider_timestamp': provider_at, 'cache_observed_at': received_at,
            'observed_at': observed_at, 'quote_age_seconds': observed_at-provider_at,
            'seconds_after_entry_open': observed_at-entry_time,
            'price_side': 'ASK' if direction == 'LONG' else 'BID',
            'tentative_entry_price': ask if direction == 'LONG' else bid}
