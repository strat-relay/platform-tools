"""Mechanical interpretations, not claims of discretionary methodology fidelity."""
from dataclasses import asdict
from . import primitives as p
from . import sequence_primitives as q
from .compositions import Detection,Proposal

MODES={'judas_sequence','po3_sequence','order_block_sequence'}


def detect(d,bars,context,tick):
    if len(bars)<max(32,d.lookback+1,d.atr_period+2):return Detection('INSUFFICIENT_WARMUP')
    step=bars[-1].close_timestamp-bars[-1].open_timestamp;cutoff=bars[-1].close_timestamp
    if any(a.close_timestamp!=b.open_timestamp for a,b in zip(bars[-max(32,d.setup_bars+d.lookback+2):],bars[-max(32,d.setup_bars+d.lookback+2)+1:])):
        return Detection('EXECUTION_DATA_GAP')
    volatility=p.atr(bars[:-1],d.atr_period)
    if volatility<=0:return Detection('ZERO_VOLATILITY')
    buffer=max(tick,d.buffer_atr*volatility)
    evidence={'prior_atr':volatility,'tick_size':tick,'cutoff':cutoff,'primitive_version':p.VERSION,'sequence_primitive_version':q.VERSION}
    def emit(direction,anchor,stop,target,extra):
        e=evidence|extra
        if not (stop<bars[-1].close<target if direction==1 else target<bars[-1].close<stop):return Detection('INVALID_GEOMETRY',evidence=e)
        rr=abs(target-bars[-1].close)/abs(stop-bars[-1].close)
        if rr<d.min_r:return Detection('RR_BELOW_MINIMUM',evidence=e|{'planned_r':rr})
        return Detection('PROPOSAL',Proposal(direction,anchor,stop,target,e|{'planned_r':rr}))
    if d.mode in {'judas_sequence','po3_sequence'}:
        start,end,date=p.session_bounds(cutoff-1,d.session_tz,d.range_start,d.range_end)
        trade_start,deadline,_=p.session_bounds(cutoff-1,d.session_tz,d.trade_start,d.trade_end)
        if not trade_start<cutoff<deadline:return Detection('OUTSIDE_SESSION')
        levels=p.session_range(bars,start,end,step)
        if levels is None:return Detection('INCOMPLETE_RANGE')
        pre=[b for b in bars if b.close_timestamp<=end]
        if len(pre)<d.atr_period+1:return Detection('INSUFFICIENT_WARMUP')
        if d.mode=='po3_sequence' and levels[1]-levels[0]>3*p.atr(pre,d.atr_period):return Detection('ACCUMULATION_TOO_WIDE')
        window=[b for b in bars if trade_start<=b.open_timestamp and b.close_timestamp<=cutoff]
        # Missing observations anywhere since session open must fail closed.
        if [b.open_timestamp for b in window]!=list(range(trade_start,cutoff,step)):return Detection('EXECUTION_DATA_GAP')
        # Sequence's ATR/BOS context includes completed pre-session warmup while
        # sweep search is strictly limited to the trade window.
        first=q.first_sweep(window,*levels)
        if d.mode=='po3_sequence' and first and window[first[0]].open_timestamp>=trade_start+3*3600:
            return Detection('MANIPULATION_WINDOW_EXPIRED')
        prefix=[b for b in bars if b.close_timestamp<=trade_start]
        # Apply a bounded session sequence with prior ATR supplied via context.
        reason,seq=q.reversal_sequence(prefix,window,levels,atr_period=d.atr_period,multiple=d.atr_multiple,max_bars=d.setup_bars,distribution=d.mode=='po3_sequence')
        if seq is None:return Detection(reason,evidence=evidence|{'session_range':levels,'session_date':date})
        if seq.confirmation_index!=len(window)-1:return Detection('SEQUENCE_ALREADY_CONFIRMED')
        direction=seq.direction;stop=seq.extreme-direction*buffer
        target=levels[1] if direction==1 else levels[0]
        if d.mode=='po3_sequence':target=bars[-1].close+direction*d.target_r*abs(bars[-1].close-stop)
        return emit(direction,f'{date}:{direction}',stop,target,{'session_date':date,'session_range':levels,
            'range_known_at':end,'sweep_at':window[seq.sweep_index].close_timestamp,'reclaim_at':window[seq.reclaim_index].close_timestamp,
            'confirmation_at':cutoff,'entry_deadline':deadline,'sequence':asdict(seq)})
    bias=p.trend(p.swings(tuple(context)))
    if not bias:return Detection('NO_STRUCTURAL_BIAS')
    for i in range(max(d.lookback,d.atr_period+1,len(bars)-d.setup_bars-2),len(bars)-1):
        zone=q.displacement_block(bars,i,bias,lookback=d.lookback,atr_period=d.atr_period,multiple=d.atr_multiple)
        if zone is None:continue
        status,touch=q.first_block_touch(zone,bars[i+1:])
        if touch is None:continue
        j=i+1+touch
        if j!=len(bars)-1:continue
        if j-i>d.setup_bars:return Detection('BLOCK_EXPIRED')
        if status=='BLOCK_INVALIDATED':return Detection(status)
        b=bars[j]
        if not ((b.close>zone.high if bias==1 else b.close<zone.low) and (p.rejection(b,bias) or p.engulfing(bars[j-1],b,bias))):return Detection('BLOCK_RETEST_NOT_CONFIRMED')
        stop=min(zone.low,b.low)-buffer if bias==1 else max(zone.high,b.high)+buffer
        target=b.close+bias*d.target_r*abs(b.close-stop)
        return emit(bias,str(zone.known_at),stop,target,{'zone':asdict(zone),'formation_at':zone.known_at,'first_touch_at':cutoff,'context_bias':bias})
    return Detection('NO_SETUP',evidence=evidence)

