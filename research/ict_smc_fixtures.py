"""Synthetic causal sequences, not historical performance samples."""
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from strategy_backtest.models import MarketEvent
from research.strategy_library_fixtures import snapshot as legacy_snapshot
from strategy_backtest.library.catalog import BY_KEY


def snapshot(d,direction=1):
    # A midweek date avoids calendar closures. Session endpoints use IANA time.
    start=int(datetime(2024,1,11,0,tzinfo=ZoneInfo(d.session_tz)).timestamp())
    if d.mode=='order_block_sequence':
        _,context=legacy_snapshot(BY_KEY['fvg_retracement'])
        cutoff=context[-1].close_timestamp
        bars=tuple(MarketEvent('EURUSD','M15',cutoff-(40-i)*900,cutoff-(39-i)*900,100.2,100.5,99.5,100.,source='synthetic') for i in range(40))
        bars=bars[:-2]+(replace(bars[-2],open=100.,high=104.,low=99.9,close=103.),replace(bars[-1],open=101.7,high=102.1,low=99.6,close=102.))
    else:
        end=start+(d.trade_start+45)*60
        bars=tuple(MarketEvent('EURUSD','M15',t,t+900,100.,100.5,99.5,100.2,source='synthetic') for t in range(start-8*3600,end,900))
        if d.mode=='judas_sequence':bars=tuple(replace(b,high=110.) if b.open_timestamp==start+3600 else b for b in bars)
        bars=bars[:-3]+(replace(bars[-3],open=100.,high=100.2,low=98.9,close=99.2),replace(bars[-2],open=99.2,high=100.4,low=99.1,close=100.2),replace(bars[-1],open=100.,high=103.2,low=99.9,close=103.))
        context=()
    if direction==-1:
        def mirror(b):return replace(b,open=200-b.open,high=200-b.low,low=200-b.high,close=200-b.close)
        bars=tuple(map(mirror,bars));context=tuple(map(mirror,context))
    return bars,context
