import copy
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import liquidity_reclaim_continuation_pair_agnostic_20260917 as h

def bars(direction='LONG', touches=()):
    out=[]
    for i in range(140):
        c={'time':1700000000+i*300,'open':101.0,'high':101.2,'low':100.8,'close':101.0,'spread':2}
        if i in touches:
            c['low' if direction=='LONG' else 'high']=99.0 if direction=='LONG' else 103.0
        out.append(c)
    return out

def row(direction='LONG', bos=120):
    return {'symbol':'USDJPYm','setup_id':'test','setup_key':'test','timestamp':h.iso(1700000000),'timestamp_epoch':1700000000,'signal_timestamp':h.iso(1700000000),'direction':direction,'bos_index':bos,'bos_timestamp':h.iso(1700000000+bos*300),'entry':100.0,'stop':98.0 if direction=='LONG' else 102.0,'risk':2.0,'sweep_timestamp':h.iso(1700000000),'reclaim_timestamp':h.iso(1700000000),'displacement_timestamp':h.iso(1700000000)}

def run(direction,touch_idxs,closes):
    b=bars(direction,touch_idxs)
    for i,c in closes.items(): c0=b[i]; c0['close']=c0['open']=c; c0['high']=max(c0['high'],c); c0['low']=min(c0['low'],c)
    s=h.base_spec(confirmation='TOUCH_NEXT_CANDLE_HOLD',expiration=5)
    return h.simulate([row(direction)],b,s)[0]

def test_next_candle():
    x=run('LONG',{121},{122:100.5}); assert x['filled'] and x['fill_index']==122 and x['confirmation_delay_candles']==1
    x=run('LONG',{121},{121:99.5,122:99.5,123:100.5}); assert not (x['filled'] and x['touch_timestamp']==h.iso(1700000000+121*300))
    x=run('LONG',{121},{121:100.5,122:99.5}); assert not (x['filled'] and x['touch_timestamp']==h.iso(1700000000+121*300))
    x=run('LONG',{121,123},{121:99.5,122:99.5,123:99.0,124:100.5}); assert x['filled'] and x['touch_timestamp']==h.iso(1700000000+123*300)
    x=run('SHORT',{121},{122:99.5}); assert x['filled'] and x['confirmation_delay_candles']==1

if __name__=='__main__':
    test_next_candle(); print('PASS: frozen entry semantics focused tests')
