"""Read-only bridge fetch for the bounded five-pair dataset."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from paper_runner import call_bridge
from .dataset import align_common_window, dataset_manifest

PAIRS = ("EURUSDm", "GBPUSDm", "USDJPYm", "EURJPYm", "GBPJPYm")
TIMEFRAMES = ("M5", "M15", "H1", "H4")

def fetch(limit: int = 500):
    raw = {}
    for symbol in PAIRS:
        snap = call_bridge("http://127.0.0.1:22350/mcp", "mt5_symbol_snapshot", {"symbol": symbol, "timeframes": list(TIMEFRAMES), "limit": limit})
        rates = snap["rates"]
        raw[symbol] = {tf: list(rates[tf]["rates"]) for tf in TIMEFRAMES}
    aligned = align_common_window(raw)
    manifest = dataset_manifest(aligned["symbols"], source="LOCAL_MT5_BRIDGE_NATIVE_RATES")
    manifest["acquired_at"] = datetime.now(timezone.utc).isoformat()
    return aligned["symbols"], manifest

if __name__ == "__main__":
    out_dir = Path("artifacts/research/multitimeframe_liquidity_sniper")
    out_dir.mkdir(parents=True, exist_ok=True)
    data, manifest = fetch()
    (out_dir / "aligned_dataset.json").write_text(json.dumps(data, indent=2) + "\n")
    (out_dir / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"symbols": list(data), "dataset_hash": manifest["dataset_hash"], "start": manifest["symbols"][next(iter(data))]["M5"]["first_timestamp"], "end": manifest["symbols"][next(iter(data))]["M5"]["last_timestamp"]}, indent=2))
