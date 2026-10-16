"""Hand-authored synthetic setup scenarios; never market/performance evidence."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timezone
import math

from strategy_backtest.models import MarketEvent
from strategy_backtest.library import primitives as p
from strategy_backtest.library.runtime import STEPS

BASE=1704067200


def makebar(start,step,tf,symbol,o,h,l,c):
    return MarketEvent(symbol,tf,start,start+step,o,h,l,c,source='synthetic',provenance={'tick_volume':10})


def snapshot(d):
    step=STEPS[d.execution];symbol=d.symbols[0] if d.symbols else 'EURUSD'
    # Midweek gives day/week fixtures preceding complete synthetic calendars.
    cutoff=BASE+10*86400+16*3600
    if d.mode in {'range_break','range_fade','range_retest','session_drive','session_pullback'} or d.key in {'silver_bullet','killzone_liquidity_sweep'}:
        local=datetime(2024,1,11,d.trade_start//60,d.trade_start%60,tzinfo=__import__('zoneinfo').ZoneInfo(d.session_tz))
        cutoff=int(local.timestamp())+3*step
    cutoff=cutoff//step*step
    count=max(260,15*86400//step+32 if d.mode in {'week_sweep','weekly_retest'} else 260)
    bars=[]
    for i in range(count):
        close=100+.15*math.sin(i*.7)
        bars.append(makebar(cutoff-(count-i)*step,step,d.execution,symbol,close-.03,close+.8,close-.8,close))
    def setbar(index,o,h,l,c):
        old=bars[index];bars[index]=replace(old,open=o,high=h,low=l,close=c)
    def tail(values):
        for index,values in enumerate(values,start=len(bars)-len(values)):setbar(index,*values)
    cstep=STEPS[d.context];boundary=cutoff//cstep*cstep
    context=[]
    for i in range(80):
        close=92+i*.1+5*math.sin(i*.7)
        context.append(makebar(boundary-(80-i)*cstep,cstep,d.context,symbol,close-.1,close+.6,close-.6,close))
    mode=d.mode
    if mode in {'channel_break','expansion','momentum','session_drive'}:
        # Establish positive EMA trend without breaking the last-20 envelope yet.
        for i in range(len(bars)-61,len(bars)-1):
            close=95+(i-(len(bars)-61))*.08
            setbar(i,close-.05,close+.7,close-.7,close)
        tail([(100,105,99.8,104.8)])
    elif mode=='ema_cross':
        for i in range(len(bars)-1):
            close=100 if i<len(bars)-11 else 99.9
            setbar(i,close,close+.2,close-.2,close)
        tail([(99.9,110.2,99.8,110)])
    elif mode in {'ema_pullback','session_pullback','ema_momentum'}:
        for i in range(len(bars)-1):
            close=90+i*.04
            setbar(i,close-.05,close+.4,close-.4,close)
        fast=p.ema([b.close for b in bars[:-1]],20)
        if mode=='ema_momentum':
            tail([(bars[-2].close, bars[-2].close+3.1,bars[-2].close-.1,bars[-2].close+3)])
        else:
            tail([(fast+.15,fast+.5,fast-1.2,fast+.45)])
    elif mode in {'bollinger','rsi_reversion','vwap_fade','vwap_reclaim'}:
        if mode=='rsi_reversion':
            for i in range(len(bars)-22,len(bars)-15):setbar(i,120,120.2,119.8,120)
            for i in range(len(bars)-15,len(bars)-1):
                close=99-(i-(len(bars)-15))*.1
                setbar(i,close+.05,close+.1,close-.1,close)
            tail([(97.7,98.7,97.6,98.6)])
        elif mode=='bollinger':
            for i in range(len(bars)-22,len(bars)-2):
                close=99.2 if i%2 else 100.8
                setbar(i,close,close+.1,close-.1,close)
            tail([(100,100.1,97.4,97.5),(97.5,98.3,97.45,98.2)])
        elif mode=='vwap_fade':tail([(100,100.1,94.8,95),(95,96.6,94.9,96.5)])
        else:tail([(99.8,100,99,99.5),(99.5,101.5,99.4,101.2)])
    elif mode in {'range_break','range_retest','range_fade','day_sweep','week_sweep','weekly_retest'}:
        if mode in {'range_fade','day_sweep','week_sweep'}:
            tail([(99,99.25,98.5,99.2)])
        else:
            tail([(100,104,99.8,103),(103,103.2,102,102.5),(101.3,101.6,100,101.5)] if mode in {'range_retest','weekly_retest'} else [(100,104,99.9,103)])
    elif mode in {'sweep','equal_sweep'}:
        if mode=='equal_sweep':
            tail([(100,101,97,100),(100,101,99,100),(100,101,99,100),
                  (100,101,97.05,100),(100,101,99,100),(100,101,99,100),(98,98.3,96,98.2)])
        else:tail([(100,100.3,97.5,100.2)])
    elif mode in {'structure_pullback','ote'}:
        levels=p.swings(tuple(context));lows=[l for l in levels if l.kind=='LOW']
        price=lows[-1].price
        if mode=='ote':
            pairs=[(a,b) for a,b in zip(levels,levels[1:]) if a.kind=='LOW' and b.kind=='HIGH' and b.price>a.price]
            a,b=pairs[-1];price=b.price-.62*(b.price-a.price)
        tail([(price+.1,price+.35,price-.8,price+.3)])
    elif mode in {'fvg','ict_fvg','order_block','liquidity_run','volatility_continuation'}:
        if mode=='order_block':
            tail([(99.8,100.2,99.4,99.5),(99.5,104.2,99.4,104),
                  (104,104.2,100,100.2),(100.3,100.6,99.5,100.5)])
        elif mode=='volatility_continuation':
            tail([(100,104.2,99.9,104),(104,104.1,102,103),(103,105.2,102.9,105)])
        elif mode=='liquidity_run':
            tail([(100,100.3,97.5,100.2),(100.2,104.2,100,104)])
        else:
            tail([(100,100.3,97.5,100.2),(100.2,101,99.8,100.5),
                  (100.5,105.2,100.4,105),(105,106,104,105.5),
                  (105.5,105.6,103.5,103.7),(104.1,104.4,103,104.3)])
    elif mode in {'bos_retest','choch','trendline_retest'}:
        # Oscillating lower structure, then upward close break and retest.
        for i in range(len(bars)-40,len(bars)-3):
            close=105-(i-(len(bars)-40))*.1+2*math.sin(i*.9)
            setbar(i,close-.05,close+.5,close-.5,close)
        known=p.swings(tuple(bars[:-3]));highs=[l.price for l in known if l.kind=='HIGH']
        level=highs[-1] if highs else 104
        if mode=='trendline_retest':
            level=p.trendline(known,-1,bars[-3].close_timestamp) or level
        tail([(level-.2,level+2.1,level-.3,level+2),
              (level+2,level+2.2,level+.1,level+.2),
              (level+.1,level+.5,level-.8,level+.4)])
    return tuple(bars),tuple(context)
