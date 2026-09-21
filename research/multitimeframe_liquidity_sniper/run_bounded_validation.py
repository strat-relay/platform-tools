from __future__ import annotations
import json
from pathlib import Path
from .dataset import dataset_manifest
from .replay import replay

def main():
    root=Path("artifacts/research/multitimeframe_liquidity_sniper")
    data=json.loads((root/"aligned_dataset.json").read_text())
    manifest=json.loads((root/"dataset_manifest.json").read_text())
    report=replay(data,output_dir=root)
    report["dataset_manifest_hash"]=manifest["dataset_hash"]
    (root/"bounded_replay_report.json").write_text(json.dumps(report,indent=2,default=str)+"\n")
    print(json.dumps(report,indent=2,default=str))

if __name__ == "__main__": main()
