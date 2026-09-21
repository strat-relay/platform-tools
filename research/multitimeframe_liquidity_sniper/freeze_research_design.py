"""Freeze chronological research splits and walk-forward folds before search."""
from __future__ import annotations
import json, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/research/multitimeframe_liquidity_sniper"


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def boundary(a, b, fraction):
    return int(a + (b-a)*fraction)


def main():
    manifest = json.loads((OUT/"historical_dataset_manifest.json").read_text())
    splits = {"schema":"research-split-manifest-v1", "frozen_at":datetime.now(timezone.utc).isoformat(),
              "rule":"per-symbol chronological 50/25/25 where sufficient; no final-period inspection for selection", "symbols":{}}
    folds = {"schema":"walk-forward-manifest-v1", "frozen_at":splits["frozen_at"],
             "rule":"calendar rolling train 90-180 days, test 30 days; only created where history supports the full fold", "symbols":{}}
    for symbol, item in manifest["symbols"].items():
        if not item.get("available"):
            splits["symbols"][symbol] = {"status":"UNAVAILABLE"}; folds["symbols"][symbol] = {"status":"UNAVAILABLE"}; continue
        first = item["M5"]["first_timestamp"]; last = item["M5"]["last_timestamp"]
        if not first or not last or last <= first:
            splits["symbols"][symbol] = {"status":"INSUFFICIENT"}; folds["symbols"][symbol] = {"status":"INSUFFICIENT"}; continue
        d1, d2 = boundary(first,last,.50), boundary(first,last,.75)
        splits["symbols"][symbol] = {"status":"FROZEN", "discovery":{"start":iso(first),"end":iso(d1)},
            "selection":{"start":iso(d1+300),"end":iso(d2)}, "final_untouched":{"start":iso(d2+300),"end":iso(last)},
            "note":"one M5 candle separation prevents boundary reuse"}
        duration = last-first
        fs=[]
        if duration >= 150*86400:
            # Fixed calendar starts relative to the first available timestamp;
            # these folds are defined before any result is inspected.
            start = first
            train = 90*86400; test = 30*86400
            while start + train + test <= last:
                fs.append({"train_start":iso(start),"train_end":iso(start+train),"test_start":iso(start+train+300),"test_end":iso(start+train+test)})
                start += test
        folds["symbols"][symbol] = {"status":"FROZEN" if fs else "INSUFFICIENT_FOR_90D_30D_FOLD", "folds":fs}
    splits["source_commit"] = subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    folds["source_commit"] = splits["source_commit"]
    (OUT/"research_split_manifest.json").write_text(json.dumps(splits,indent=2)+"\n")
    (OUT/"walk_forward_manifest.json").write_text(json.dumps(folds,indent=2)+"\n")
    print(json.dumps({"split_manifest":str(OUT/"research_split_manifest.json"),"walk_forward_manifest":str(OUT/"walk_forward_manifest.json"),"frozen_symbols":sum(v.get("status")=="FROZEN" for v in splits["symbols"].values())},indent=2))

if __name__ == "__main__": main()
