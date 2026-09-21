import json, sys, statistics
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import liquidity_reclaim_continuation_pair_agnostic_20260917 as h
ROOT=Path(__file__).resolve().parents[1]
src=json.loads((ROOT/'artifacts/audits/liquidity_reclaim_continuation_pair_agnostic_20260917.json').read_text())
specs=[x['spec'] for x in src['finalist_results'][:2]]
prepared={}
for s in h.PAIRS:
    x=h.DATA['symbols'][s]; prepared[s]=(x['M5'],h.atr_series(x['M5']),x['contract'])
all_rows=[h.evaluate(s,prepared) for s in specs]
out=[]
for held in h.PAIRS:
    train_scores=[]
    for i,rows in enumerate(all_rows):
        train=[x for x in rows if x['symbol']!=held and h.period_for(x['timestamp_epoch'])=='SELECTION']
        train_scores.append((h.equal_pair(train),i))
    chosen=max(train_scores)[1]
    held_rows=[x for x in all_rows[chosen] if x['symbol']==held and h.period_for(x['timestamp_epoch'])=='FINAL_UNTOUCHED_TEST']
    out.append({'held_out_symbol':held,'selected_variant_without_pair':chosen,'selection_equal_pair_expectancy_excluding_pair':max(train_scores)[0],'held_out':h.metrics(held_rows)})
payload={'procedure':'select between the two already-frozen finalists using selection-period equal-pair expectancy on the other 14 pairs; no new values introduced','rows':out,'positive_held_out_pairs':sum(x['held_out']['expectancy_R']>0 for x in out),'median_held_out_expectancy_R':statistics.median(x['held_out']['expectancy_R'] for x in out),'worst_held_out_expectancy_R':min(x['held_out']['expectancy_R'] for x in out)}
p=ROOT/'artifacts/audits/LIQUIDITY_RECLAIM_CONTINUATION_CLEAN_LOPO_20260917.json'; p.write_text(json.dumps(payload,indent=2)+'\n'); print(p)
