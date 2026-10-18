"""Versioned causal ingredients for sequenced research hypotheses."""
from dataclasses import dataclass
from . import primitives as p

VERSION='sequence-primitives.v1'

@dataclass(frozen=True)
class SweepSequence:
    direction: int
    sweep_index: int
    reclaim_index: int
    confirmation_index: int
    extreme: float


def first_sweep(bars, low, high):
    """First range violation; a two-sided candle is explicitly ambiguous."""
    for i,b in enumerate(bars):
        down,up=b.low<low,b.high>high
        if down or up:return i,0 if down and up else 1 if down else -1
    return None


def reversal_sequence(prefix,window,levels,*,atr_period,multiple,max_bars,distribution=False):
    """One reusable session wrapper: session-only sweep search, full prior indicators."""
    first=first_sweep(window,*levels)
    if first is None:return 'NO_SESSION_SWEEP',None
    i,direction=first
    if not direction:return 'AMBIGUOUS_SESSION_SWEEP',None
    reclaim=None;low,high=levels
    for j in range(i+1,min(len(window),i+max_bars+1)):
        b=window[j]
        if reclaim is None:
            if (b.high>high if direction==1 else b.low<low):return 'SEQUENCE_INVALIDATED',None
            if low<b.close<high:reclaim=j
            continue
        prior=prefix+window[:j]
        if len(prior)<max(5,atr_period+1):continue
        vol=p.atr(prior,atr_period);lo,hi=p.channel(prior,5)
        beyond=b.close>hi if direction==1 else b.close<lo
        if distribution:beyond=beyond and (b.close>high if direction==1 else b.close<low)
        if beyond and p.displacement(b,vol,direction,multiple):
            extreme=min(x.low for x in window[i:j+1]) if direction==1 else max(x.high for x in window[i:j+1])
            return 'SEQUENCE_CONFIRMED',SweepSequence(direction,i,reclaim,j,extreme)
    return ('SEQUENCE_EXPIRED' if len(window)>i+max_bars+1 else 'AWAITING_SEQUENCE_CONFIRMATION'),None

def displacement_block(bars,index,direction,*,lookback,atr_period,multiple,opposing_lookback=5):
    """Wick-range of last opposing candle; known only at BOS displacement close."""
    if index<max(lookback,atr_period+1):return None
    prior=bars[:index];b=bars[index];vol=p.atr(prior,atr_period);lo,hi=p.channel(prior,lookback)
    if not p.displacement(b,vol,direction,multiple) or not (b.close>hi if direction==1 else b.close<lo):return None
    origin=next((x for x in reversed(prior[-opposing_lookback:]) if direction*(x.close-x.open)<0),None)
    return p.Zone(origin.low,origin.high,direction,origin.close_timestamp,b.close_timestamp,'DISPLACEMENT_ORDER_BLOCK') if origin else None


def first_block_touch(zone,bars):
    """First post-formation intersection consumes the block, including failed retests.

    A close beyond the distal edge invalidates it even without an intersection
    (e.g. a gap). Equality is a touch; distal invalidation is strict.
    """
    for i,b in enumerate(bars):
        if b.close_timestamp<=zone.known_at:continue
        if (b.close<zone.low if zone.direction==1 else b.close>zone.high):return 'BLOCK_INVALIDATED',i
        if zone.intersects(b):return 'BLOCK_TOUCHED',i
    return 'BLOCK_UNTOUCHED',None
