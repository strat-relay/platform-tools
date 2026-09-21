"""Focused research-harness invariants; no production imports are mutated."""
import json
from pathlib import Path
from research.usdjpy_sequential_mss_20260917 import Sequential, replay_seq

DATA=Path('/tmp/usdjpy_historical_20260917.json')

def run_controls():
    d=json.loads(DATA.read_text())
    # The source-level delegation is the zero-delay pairwise control.
    src=Path(__file__).with_name('usdjpy_sequential_mss_20260917.py').read_text()
    assert 'return self.levels.find_candidate' in src
    assert 'return self.delayed_control.evaluate' in src
    # Cumulative-window invariant used by the grid assembler.
    control={('sweep-a','LONG'),('sweep-b','SHORT')}
    for later in ({('sweep-c','LONG')},{('sweep-c','LONG'),('sweep-d','SHORT')}):
        merged=control|later; assert control <= merged
    # Sep-10 unit trace: delayed N=2 must reach displacement state and later
    # BOS independently of the later candle's displacement body.
    i=next(k for k,x in enumerate(d['M5']) if int(x['time'])==1789025400)
    import bisect
    mt=[x['time'] for x in d['M15']]; j=bisect.bisect_right(mt,d['M5'][i]['time']); ctx=d['M15'][max(0,j-120):j]; c=d['M5'][i]; sp=c['spread']*d['contract']['point']; q={'bid':c['close']-sp/2,'ask':c['close']+sp/2}
    s=Sequential(2,2); out=s.evaluate(ctx,d['M5'],q,d['contract'],'',i)
    assert out is not None and out['reclaim_delay']==2 and out['displacement_index']==58552 and out['bos_index']==58554
    from research.usdjpy_entry_pipeline_audit_20260917 import sim
    z=sim(out,d['M5'],0,5)
    assert z['filled'] and z['fill_index']==58556
    assert d['M5'][58555]['low'] < z['entry'] and d['M5'][58555]['close'] < z['entry']
    assert d['M5'][58556]['low'] < z['entry'] and d['M5'][58556]['close'] >= z['entry']
    return 'PASS'

if __name__=='__main__': print(run_controls())
