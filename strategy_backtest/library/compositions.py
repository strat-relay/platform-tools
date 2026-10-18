"""Explicit research hypotheses composed from causal ingredients.

Detection never submits orders, fetches data or resolves future outcomes.
Historical sequence searches are bounded and record their anchor/known-at times.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from strategy_backtest.models import MarketEvent
from .catalog import Definition
from . import primitives as p


@dataclass(frozen=True)
class Proposal:
    direction: int
    anchor: str
    stop: float
    target: float
    evidence: Mapping


@dataclass(frozen=True)
class Detection:
    reason: str
    proposal: Proposal | None = None
    evidence: Mapping | None = None


def _confirm(bars, direction):
    return p.engulfing(bars[-2], bars[-1], direction) or p.rejection(bars[-1], direction)


def _sweep(bar, low, high, direction):
    return bar.low < low and bar.close > low if direction == 1 else bar.high > high and bar.close < high


def _break(bar, low, high, direction):
    return bar.close > high if direction == 1 else bar.close < low


def _range_for_definition(d, bars, cutoff, step):
    start, end, date = p.session_bounds(cutoff-1, d.session_tz, d.range_start, d.range_end)
    trade_start, trade_end, _ = p.session_bounds(cutoff-1, d.session_tz, d.trade_start, d.trade_end)
    if not (trade_start < cutoff < trade_end and cutoff > end):
        return None, None, 'OUTSIDE_SESSION'
    levels = p.session_range(bars, start, end, step)
    return levels, (start, end, trade_start, trade_end, date), 'INCOMPLETE_RANGE' if levels is None else None


def _prior_calendar_range(bars, cutoff, step, weekly=False):
    now = datetime.fromtimestamp(cutoff-1, timezone.utc)
    boundary = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if weekly:
        boundary -= timedelta(days=boundary.weekday())
    end = int(boundary.timestamp())
    start = end-(7 if weekly else 1)*86400
    return p.session_range(bars, start, end, step), start, end


def detect(d: Definition, bars: Sequence[MarketEvent], context: Sequence[MarketEvent], tick: float) -> Detection:
    """Inputs are already validated, completed and cutoff-bounded by the SDK."""
    if d.mode in {'judas_sequence','po3_sequence','order_block_sequence'}:
        from .sequence_compositions import detect as sequence_detect
        return sequence_detect(d,bars,context,tick)
    if len(bars) < max(32, d.slow+2 if d.mode in {'ema_cross','ema_pullback','session_pullback','momentum','ema_momentum'} else 32):
        return Detection('INSUFFICIENT_WARMUP')
    current, previous = bars[-1], bars[-2]
    prior = bars[:-1]
    volatility = p.atr(prior, d.atr_period)
    if volatility <= 0:
        return Detection('ZERO_VOLATILITY')
    tol, buffer = d.tolerance_atr*volatility, max(tick, d.buffer_atr*volatility)
    cutoff = current.close_timestamp
    step = current.close_timestamp-current.open_timestamp
    # Sequence gates require contiguous observations. Long day/week/session range
    # coverage is checked separately, with no guessed market calendar.
    recent = bars[-max(32, d.lookback+2, d.setup_bars+22):]
    if any(a.close_timestamp != b.open_timestamp for a,b in zip(recent,recent[1:])):
        return Detection('EXECUTION_DATA_GAP')
    levels = p.swings(tuple(prior))
    context_levels = p.swings(tuple(context))
    bias = p.trend(context_levels)
    low, high = p.channel(prior, d.lookback)
    evidence = {'prior_atr': volatility, 'tick_size':tick, 'channel': [low, high], 'cutoff': cutoff,
                'context_bias': bias, 'primitive_version': p.VERSION}

    def emit(direction, anchor, stop=None, target=None, extra=None):
        stop = (min(b.low for b in bars[-5:])-buffer if direction == 1 else max(b.high for b in bars[-5:])+buffer) if stop is None else stop
        target = current.close+direction*d.target_r*abs(current.close-stop) if target is None else target
        if not (stop < current.close < target if direction == 1 else target < current.close < stop):
            return Detection('INVALID_GEOMETRY', evidence=evidence | (extra or {}))
        rr = abs(target-current.close)/abs(current.close-stop)
        if rr < d.min_r:
            return Detection('RR_BELOW_MINIMUM', evidence=evidence | {'planned_r':rr} | (extra or {}))
        return Detection('PROPOSAL', Proposal(direction, str(anchor), stop, target,
                         evidence | {'planned_r':rr} | (extra or {})))

    def zone_retest(zone, direction, anchor, extra=None):
        if not (zone.intersects(current) and (current.close > zone.high if direction == 1 else current.close < zone.low)
                and _confirm(bars, direction) and p.active_zone(zone, prior, direction)):
            return None
        stop = min(zone.low,current.low)-buffer if direction == 1 else max(zone.high,current.high)+buffer
        return emit(direction, anchor, stop, extra={'zone':asdict(zone)} | (extra or {}))

    mode = d.mode
    if mode in {'channel_break','expansion','momentum','session_drive'}:
        if mode == 'session_drive':
            start,end,date = p.session_bounds(cutoff-1,d.session_tz,d.trade_start,d.trade_end)
            if not start < cutoff < end:
                return Detection('OUTSIDE_SESSION')
        for direction in (1,-1):
            if not _break(current,low,high,direction):
                continue
            if mode == 'expansion' and not (current.high-current.low >= d.atr_multiple*volatility
                    and direction*(current.close-current.open) >= .65*(current.high-current.low)):
                continue
            if mode in {'momentum','session_drive'}:
                if not p.displacement(current,volatility,direction,d.atr_multiple):
                    continue
                if mode == 'momentum':
                    closes=[b.close for b in prior]
                    if direction*(p.ema(closes,d.fast)-p.ema(closes,d.slow)) <= 0:
                        continue
            anchor = f'{date}:{direction}' if mode=='session_drive' else current.close_timestamp
            return emit(direction,anchor)
    elif mode in {'ema_cross','ema_pullback','session_pullback','ema_momentum'}:
        if mode == 'session_pullback':
            start,end,date = p.session_bounds(cutoff-1,d.session_tz,d.trade_start,d.trade_end)
            if not start < cutoff < end:
                return Detection('OUTSIDE_SESSION')
        closes=[b.close for b in bars]
        fast,slow=p.ema(closes[:-1],d.fast),p.ema(closes[:-1],d.slow)
        for direction in (1,-1):
            if mode=='ema_cross':
                if direction*(fast-slow) <= 0 and direction*(p.ema(closes,d.fast)-p.ema(closes,d.slow)) > 0:
                    return emit(direction,cutoff)
            elif direction*(fast-slow)>0:
                if mode=='ema_momentum':
                    moves=[b.close for b in bars[-3:]]
                    if all(direction*(b-a)>0 for a,b in zip(moves,moves[1:])) and p.displacement(current,volatility,direction,d.atr_multiple) and not p.displacement(previous,p.atr(bars[:-2]),direction,d.atr_multiple):
                        return emit(direction,cutoff)
                elif current.low <= fast <= current.high and direction*(current.close-fast)>0 and _confirm(bars,direction):
                    return emit(direction, f'{date}:{direction}' if mode=='session_pullback' else cutoff, extra={'ema_fast':fast,'ema_slow':slow})
    elif mode in {'bollinger','rsi_reversion','vwap_fade','vwap_reclaim'}:
        closes=[b.close for b in bars]
        center=p.mean([b.close for b in prior[-20:]])
        lower,_,upper=p.bands([b.close for b in prior],20)
        if mode.startswith('vwap'):
            day=datetime.fromtimestamp(cutoff-1,timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
            start=int(day.timestamp())
            daybars=[b for b in prior if b.open_timestamp>=start]
            if not daybars or daybars[0].open_timestamp!=start:
                return Detection('INCOMPLETE_SESSION')
            center=p.vwap(daybars)
            if center is None:
                return Detection('VOLUME_UNAVAILABLE')
        for direction in (1,-1):
            matched=False
            if mode=='bollinger':
                boundary=lower if direction==1 else upper
                matched=direction*(previous.close-boundary)<0 and direction*(current.close-boundary)>0
            elif mode=='rsi_reversion':
                old,new=p.rsi(closes[:-1]),p.rsi(closes)
                matched=(old<30<=new) if direction==1 else (old>70>=new)
            elif mode=='vwap_fade':
                matched=direction*(previous.close-center)<-1.5*volatility and direction*(current.close-previous.close)>0
            elif mode=='vwap_reclaim':
                matched=bias==direction and direction*(previous.close-center)<=0<direction*(current.close-center)
            if matched:
                return emit(direction,cutoff,target=None if mode=='vwap_reclaim' else center,extra={'frozen_mean_or_vwap':center})
    elif mode in {'range_break','range_fade','range_retest'}:
        bounds, session, reason=_range_for_definition(d,bars,cutoff,step)
        if reason:
            return Detection(reason)
        lo,hi=bounds
        _,range_end,_,trade_end,date=session
        for direction in (1,-1):
            anchor=f'{date}:{direction}'
            extra={'range':bounds,'session':session,'entry_deadline':trade_end}
            if mode=='range_break':
                boundary=hi if direction==1 else lo
                if direction*(previous.close-boundary)<=0<direction*(current.close-boundary):
                    return emit(direction,anchor,lo-buffer if direction==1 else hi+buffer,extra=extra)
            elif mode=='range_fade':
                if _sweep(current,lo,hi,direction) and _confirm(bars,direction):
                    return emit(direction,anchor,current.low-buffer if direction==1 else current.high+buffer,
                                hi if direction==1 else lo,extra)
            else:
                boundary=hi if direction==1 else lo
                for a,b in zip(bars[-d.setup_bars-2:-2],bars[-d.setup_bars-1:-1]):
                    if b.close_timestamp<=range_end or not direction*(a.close-boundary)<=0<direction*(b.close-boundary):
                        continue
                    z=p.Zone(boundary-tol,boundary+tol,direction,b.close_timestamp,b.close_timestamp,'ROLE_FLIP')
                    result=zone_retest(z,direction,anchor,extra)
                    if result:return result
    elif mode in {'day_sweep','week_sweep','weekly_retest'}:
        bounds,start,end=_prior_calendar_range(bars,cutoff,step,mode!='day_sweep')
        if bounds is None:return Detection('INCOMPLETE_CALENDAR_RANGE')
        lo,hi=bounds
        for direction in (1,-1):
            if mode!='weekly_retest':
                if _sweep(current,lo,hi,direction) and _confirm(bars,direction):
                    return emit(direction,f'{start}:{direction}',current.low-buffer if direction==1 else current.high+buffer,
                                extra={'calendar_range':[start,end,lo,hi]})
            else:
                boundary=hi if direction==1 else lo
                for a,b in zip(bars[-d.setup_bars-2:-2],bars[-d.setup_bars-1:-1]):
                    if b.close_timestamp<=end or not direction*(a.close-boundary)<=0<direction*(b.close-boundary):continue
                    result=zone_retest(p.Zone(boundary-tol,boundary+tol,direction,b.close_timestamp,b.close_timestamp,'WEEKLY_ROLE_FLIP'),direction,f'{start}:{direction}')
                    if result:return result
    elif mode in {'sweep','equal_sweep'}:
        if d.session_tz!='UTC' or d.trade_start!=60 or d.trade_end!=240:
            start,end,_=p.session_bounds(cutoff-1,d.session_tz,d.trade_start,d.trade_end)
            if not start<cutoff<end:return Detection('OUTSIDE_SESSION')
        for direction in (1,-1):
            lo,hi=low,high
            if mode=='equal_sweep':
                kind='LOW' if direction==1 else 'HIGH'
                pair=[l for l in levels if l.kind==kind][-2:]
                if len(pair)<2 or abs(pair[0].price-pair[1].price)>tol:continue
                lo=min(l.price for l in pair);hi=max(l.price for l in pair)
            if _sweep(current,lo,hi,direction) and _confirm(bars,direction):
                return emit(direction,cutoff,current.low-buffer if direction==1 else current.high+buffer)
    elif mode=='structure_pullback':
        if not bias:return Detection('NO_STRUCTURAL_BIAS')
        same=[l for l in context_levels if l.kind==('LOW' if bias==1 else 'HIGH')]
        if same:
            l=same[-1]
            result=zone_retest(p.Zone(l.price-tol,l.price+tol,bias,l.event_at,l.known_at,'STRUCTURAL_PULLBACK'),bias,l.event_at)
            if result:return result
    elif mode=='ote':
        if not bias:return Detection('NO_STRUCTURAL_BIAS')
        # Latest opposite-kind consecutive pivots form a confirmed impulse.
        pairs=[(a,b) for a,b in zip(context_levels,context_levels[1:])
               if a.kind==('LOW' if bias==1 else 'HIGH') and b.kind==('HIGH' if bias==1 else 'LOW') and bias*(b.price-a.price)>0]
        if pairs:
            a,b=pairs[-1]; distance=abs(b.price-a.price)
            endpoints=[b.price-bias*f*distance for f in (.62,.79)]
            z=p.Zone(min(endpoints),max(endpoints),bias,a.event_at,b.known_at,'OTE_62_79')
            result=zone_retest(z,bias,b.event_at,{'impulse':[asdict(a),asdict(b)]})
            if result:return result
    elif mode in {'bos_retest','choch','trendline_retest'}:
        for direction in (1,-1):
            for idx in range(max(22,len(bars)-d.setup_bars-1),len(bars)-1):
                before=bars[:idx];breakbar=bars[idx]
                known=p.swings(tuple(before));oldtrend=p.trend(known)
                if mode=='bos_retest' and bias!=direction:continue
                if mode=='choch' and oldtrend!=-direction:continue
                if mode=='trendline_retest':
                    level=p.trendline(known,-direction,breakbar.close_timestamp)
                    previous_level=p.trendline(known,-direction,before[-1].close_timestamp)
                else:
                    anchors=[l for l in known if l.kind==('HIGH' if direction==1 else 'LOW')]
                    if not anchors:continue
                    level=previous_level=anchors[-1].price
                if level is None or previous_level is None or not direction*(before[-1].close-previous_level)<=0<direction*(breakbar.close-level):continue
                z=p.Zone(level-tol,level+tol,direction,breakbar.close_timestamp,breakbar.close_timestamp,'BREAK_RETEST')
                result=zone_retest(z,direction,breakbar.close_timestamp,{'structural_event':'CHOCH' if mode=='choch' else 'BOS' if mode=='bos_retest' else 'TRENDLINE_BREAK'})
                if result:return result
    elif mode in {'fvg','order_block','ict_fvg','liquidity_run','volatility_continuation'}:
        if mode=='ict_fvg' and d.key=='silver_bullet':
            start,end,_=p.session_bounds(cutoff-1,d.session_tz,d.trade_start,d.trade_end)
            if not start<cutoff<end:return Detection('OUTSIDE_SESSION')
        for direction in (1,-1):
            if mode in {'fvg','ict_fvg','liquidity_run'} and bias!=direction:continue
            for idx in range(max(22,len(bars)-d.setup_bars-1),len(bars)-1):
                before=bars[:idx]; impulse=bars[idx]
                vol=p.atr(before)
                if not p.displacement(impulse,vol,direction,d.atr_multiple):continue
                sweep_event=None
                if mode in {'ict_fvg','liquidity_run'}:
                    for j in range(max(21,idx-5),idx):
                        a,b=p.channel(bars[:j],20)
                        if _sweep(bars[j],a,b,direction) and not any((x.close<bars[j].low if direction==1 else x.close>bars[j].high) for x in bars[j+1:idx]):sweep_event=bars[j]
                    if sweep_event is None:continue
                    a,b=p.channel(before,5)
                    if not _break(impulse,a,b,direction):continue
                if mode=='liquidity_run':
                    # Only current displacement emits; prior sweep is its anchor.
                    continue
                if mode=='volatility_continuation':
                    edge=impulse.high if direction==1 else impulse.low
                    if direction*(previous.close-edge)<=0<direction*(current.close-edge):
                        return emit(direction,impulse.close_timestamp,impulse.low-buffer if direction==1 else impulse.high+buffer)
                    continue
                if mode=='order_block':
                    z=p.order_block(bars[:idx+1],direction,vol)
                    zones=[] if z is None else [z]
                else:
                    # FVG must involve this displacement as middle candle.
                    if idx+1>=len(bars)-1:continue
                    zones=[z for z in p.fvgs(bars[idx-1:idx+2]) if z.direction==direction]
                for z in zones:
                    if current.close_timestamp<=z.known_at:continue
                    extra={'displacement_at':impulse.close_timestamp}
                    if sweep_event:extra['sweep_at']=sweep_event.close_timestamp
                    result=zone_retest(z,direction,z.known_at,extra)
                    if result:return result
        if mode=='liquidity_run' and bias:
            for j in range(max(21,len(bars)-d.setup_bars-1),len(bars)-1):
                a,b=p.channel(bars[:j],20)
                if (_sweep(bars[j],a,b,bias) and p.displacement(current,volatility,bias,d.atr_multiple)
                        and not any((x.close<bars[j].low if bias==1 else x.close>bars[j].high) for x in bars[j+1:-1])):
                    a,b=p.channel(prior,5)
                    if _break(current,a,b,bias):
                        return emit(bias,bars[j].close_timestamp,bars[j].low-buffer if bias==1 else bars[j].high+buffer)
    else:
        raise ValueError(f'unsupported composition: {mode}')
    return Detection('NO_SETUP', evidence=evidence)
