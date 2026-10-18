"""Conservative weekend display policy; never authorizes data, signals or execution.

Only the common guaranteed weekend window is covered: Friday 17:00 through
Sunday 17:00 America/New_York. Symbol-specific later reopening, holidays and
daily breaks remain unclassified. Broker session metadata is not available in
the current bridge. Crypto and unclassified instruments must never inherit this policy.
"""
from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

POLICY = 'library-mt5-fx-metal-weekend-display.v1'
WEEKEND_ASSET_CLASSES = frozenset({'FX', 'METAL'})
STEPS = {'M5':300, 'M15':900, 'H1':3600, 'H4':14400}


def weekend_window(asset_class, now):
    """Return the supported weekend window without asserting feed health."""
    if asset_class not in WEEKEND_ASSET_CLASSES or not math.isfinite(now):
        return None
    try:
        zone = ZoneInfo('America/New_York')
    except ZoneInfoNotFoundError:
        return None
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(zone)
    friday = local.date()-timedelta(days=(local.weekday()-4)%7)
    start = datetime.combine(friday,time(17),zone).timestamp()
    end = datetime.combine(friday+timedelta(days=2),time(17),zone).timestamp()
    if not start <= now < end:
        return None
    return {'policy': POLICY, 'basis': 'EXPECTED_PROVIDER_WEEKEND',
            'closure_start': int(start), 'review_after': int(end)}


def expected_weekend(asset_class, now, stream):
    if asset_class not in WEEKEND_ASSET_CLASSES or not math.isfinite(now):
        return None
    try:
        zone = ZoneInfo('America/New_York')
    except ZoneInfoNotFoundError:
        return None  # No timezone evidence: preserve the original degraded state.
    local = datetime.fromtimestamp(now, timezone.utc).astimezone(zone)
    friday = local.date()-timedelta(days=(local.weekday()-4)%7)
    start = datetime.combine(friday,time(17),zone).timestamp()
    end = datetime.combine(friday+timedelta(days=2),time(17),zone).timestamp()
    if not start <= now < end:
        return None
    observed = stream.get('last_observed_at')
    if (not isinstance(observed,(int,float)) or not math.isfinite(observed) or
            not start-330 <= observed <= now):
        return None  # A pre-existing feed outage must not become a weekend closure.
    series = stream.get('series') or {}
    if not series or not {'H1','M15'} <= set(series):
        return None
    for tf, rows in series.items():
        if tf not in STEPS or not rows:
            return None
        last = rows[-1].get('close_timestamp')
        expected = int((start-1)//STEPS[tf])*STEPS[tf]
        if (not isinstance(last,(int,float)) or not math.isfinite(last) or
                not expected <= last <= start):
            return None
    return {'policy':POLICY, 'basis':'EXPECTED_PROVIDER_WEEKEND',
            'closure_start':int(start), 'review_after':int(end),
            'last_successful_observation':observed}
