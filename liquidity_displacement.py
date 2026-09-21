from __future__ import annotations
from dataclasses import dataclass
import hashlib
from typing import Any

from paper_engine import Signal, _candle, atr, ema, rsi, swings

@dataclass
class LiquidityDisplacementConfig:
    symbol: str = "XAUUSDm"
    target_r: float = 1.25
    max_hold_minutes: int = 120
    max_retrace_candles: int = 3
    max_structure_break_candles: int = 5
    min_body_atr: float = 0.50
    min_body_median_multiple: float = 1.00
    min_close_location: float = 0.60
    atr_buffer_fraction: float = 0.10

class LiquidityDisplacementStrategy:
    """Fixed research rule: sweep -> reclaim -> displacement -> micro break -> retrace."""
    def __init__(self, config: LiquidityDisplacementConfig|None=None): self.config=config or LiquidityDisplacementConfig()

    def _levels(self, m5: list[dict[str,Any]], i: int):
        prior=m5[max(0,i-36):i]
        highs,lows=swings(prior,2)
        recent_high=max(highs[-3:]) if highs else None; recent_low=min(lows[-3:]) if lows else None
        # Prior completed session extremes are descriptive liquidity levels.
        day=datetime_utc(int(prior[-1]["time"])).date() if prior else None
        session_bars=[x for x in prior if datetime_utc(int(x["time"])).date()==day and 8<=datetime_utc(int(x["time"])).hour<21]
        session_high=max([_candle(x,"high") for x in session_bars],default=None); session_low=min([_candle(x,"low") for x in session_bars],default=None)
        lows_all=[x for x in [recent_low,session_low] if x is not None]; highs_all=[x for x in [recent_high,session_high] if x is not None]
        return (max(lows_all) if lows_all else None, max(highs_all) if highs_all else None, recent_low, recent_high)

    def find_candidate(self, m15: list[dict[str,Any]], m5: list[dict[str,Any]], i: int, quote: dict[str,float], contract: dict[str,float], timestamp: str):
        empty=Signal(self.config.symbol,"NONE","NO_LIQUIDITY_DISPLACEMENT",None,None,None,0,"M5",[],"no_sequence",timestamp)
        if len(m5)<40 or len(m15)<30:return None
        c=m5[i]; a=atr(m5[max(0,i-119):i+1])[-1]; spread=float(quote["ask"])-float(quote["bid"])
        if a<=0 or spread<=0:return None
        low_level,high_level,recent_low,recent_high=self._levels(m5,i)
        if low_level is None or high_level is None:return None
        prior=m5[max(0,i-12):i]; bodies=[abs(_candle(x,"close")-_candle(x,"open")) for x in prior]; med_body=sorted(bodies)[len(bodies)//2] if bodies else 0
        sweep_long=_candle(c,"low")<low_level and _candle(c,"close")>low_level
        sweep_short=_candle(c,"high")>high_level and _candle(c,"close")<high_level
        direction="LONG" if sweep_long else "SHORT" if sweep_short else None
        if not direction:return None
        sweep_level=low_level if direction=="LONG" else high_level; sweep_extreme=_candle(c,"low") if direction=="LONG" else _candle(c,"high")
        # Search only forward completed candles for displacement and micro shift.
        disp_i=None; break_level=None; disp_features=None
        for j in range(i+1,min(len(m5),i+1+self.config.max_structure_break_candles)):
            d=m5[j]; rng=_candle(d,"high")-_candle(d,"low"); body=abs(_candle(d,"close")-_candle(d,"open")); close_loc=(_candle(d,"close")-_candle(d,"low"))/rng if rng>0 else .5
            bullish=direction=="LONG" and _candle(d,"close")>_candle(d,"open") and close_loc>=self.config.min_close_location
            bearish=direction=="SHORT" and _candle(d,"close")<_candle(d,"open") and close_loc<=1-self.config.min_close_location
            displacement=body>=a*self.config.min_body_atr and body>=med_body*self.config.min_body_median_multiple
            if not (displacement and (bullish or bearish)):continue
            micro= max(_candle(x,"high") for x in m5[max(i-5,i-12):i]) if direction=="LONG" else min(_candle(x,"low") for x in m5[max(i-5,i-12):i])
            shifted=_candle(d,"close")>micro if direction=="LONG" else _candle(d,"close")<micro
            if shifted: disp_i=j; break_level=micro; disp_features=(body/rng if rng else 0,close_loc,rng); break
        if disp_i is None:return None
        d=m5[disp_i]; dlow,dhigh=_candle(d,"low"),_candle(d,"high"); drange=dhigh-dlow; entry=dlow+drange*.50 if direction=="LONG" else dhigh-drange*.50
        broker_min=max(float(contract.get("tick_size",.001)),float(contract.get("stops_level",0))*float(contract.get("point",.001)))
        buffer=max(a*self.config.atr_buffer_fraction,spread*1.25,broker_min)
        stop=sweep_extreme-buffer if direction=="LONG" else sweep_extreme+buffer
        risk=entry-stop if direction=="LONG" else stop-entry
        if risk<=0:return None
        return {"direction":direction,"sweep_level":sweep_level,"sweep_extreme":sweep_extreme,"displacement_index":disp_i,"break_level":break_level,"entry":entry,"stop_loss":stop,"risk":risk,"atr":a,"spread":spread,"body_atr":disp_features[0],"close_location":disp_features[1],"displacement_range":disp_features[2],"sweep_distance":abs(sweep_extreme-sweep_level),"wick_penetration":abs(sweep_extreme-sweep_level),"time_sweep_to_break":disp_i-i,"time_break_to_entry":None,"entry_delay_candles":None,"entry_type":"RETRACE_50_DISPLACEMENT","setup_type":"LIQUIDITY_DISPLACEMENT_SCALP","break_distance":abs(_candle(d,"close")-break_level),"liquidity_type":"RECENT_SWING_OR_SESSION_EXTREME","touch_count":None,"level_age_candles":i-max(0,i-36),"next_opposing_level":high_level if direction=="LONG" else low_level}

    def evaluate(self,m15,m5,quote,contract,timestamp,i):
        candidate=self.find_candidate(m15,m5,i,quote,contract,timestamp)
        if not candidate:return None
        direction=candidate["direction"]; entry=candidate["entry"]; stop=candidate["stop_loss"]; start=candidate["displacement_index"]
        fill_i=None
        for j in range(start+1,min(len(m5),start+1+self.config.max_retrace_candles)):
            c=m5[j]; touched=_candle(c,"low")<=entry if direction=="LONG" else _candle(c,"high")>=entry
            held=_candle(c,"close")>=entry if direction=="LONG" else _candle(c,"close")<=entry
            if touched and held: fill_i=j; candidate["time_break_to_entry"]=j-start; candidate["entry_delay_candles"]=j-i; break
        if fill_i is None:
            candidate["status"]="UNFILLED"; candidate["fill_index"]=None; return candidate
        candidate["status"]="FILLED"; candidate["fill_index"]=fill_i; candidate["entry_time"]=int(m5[fill_i]["time"])+300
        fp=hashlib.sha256(f"{self.config.symbol}|{direction}|{m5[i]['time']}|{fill_i}|{entry:.3f}".encode()).hexdigest()[:16]
        candidate["signal"]=Signal(self.config.symbol,direction,"LIQUIDITY_DISPLACEMENT_SCALP",entry,stop,entry+(entry-stop)*self.config.target_r if direction=="LONG" else entry-(stop-entry)*self.config.target_r,.72,"M5",["liquidity sweep","reclaim","displacement","micro structure shift","50% retracement"],None,timestamp,fp)
        return candidate

def datetime_utc(ts):
    from datetime import datetime,timezone
    return datetime.fromtimestamp(ts,timezone.utc)
