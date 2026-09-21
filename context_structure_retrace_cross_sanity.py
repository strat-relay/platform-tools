from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from context_structure_retrace.data import CausalReplay, bar_end, floor_timestamp
from context_structure_retrace.replay import feature_snapshot
from context_structure_retrace.attention import attention_layer


def aggregate_h4_from_h1(bars: list[dict]) -> list[dict]:
    grouped = {}
    for bar in bars:
        start = floor_timestamp(int(bar["time"]), "H4")
        grouped.setdefault(start, []).append(bar)
    out = []
    for start, group in sorted(grouped.items()):
        out.append({"time": start, "open": group[0]["open"], "high": max(x["high"] for x in group), "low": min(x["low"] for x in group), "close": group[-1]["close"], "spread": group[-1].get("spread", 0), "tick_volume": sum(x.get("tick_volume", 0) for x in group), "forming": False, "source_timeframe": "H1"})
    return out


def load(path: Path) -> dict:
    data = json.loads(path.read_text())
    if "H4" not in data and "H1" in data:
        data["H4"] = aggregate_h4_from_h1(data["H1"])
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="/tmp/context-structure-retrace")
    parser.add_argument("--out", default="context_structure_retrace_cross_instrument_results.json")
    args = parser.parse_args()
    root = Path(args.data_dir)
    paths = {p.stem: p for p in (root / "xau.json", root / "btc_basic.json", root / "USDJPYm.json", root / "EURUSDm.json") if p.exists() and p.stat().st_size}
    results = {}
    for label, path in paths.items():
        data = load(path)
        symbol = data.get("symbol", label)
        replay = CausalReplay({k: v for k, v in data.items() if k in {"M1", "M5", "M15", "H1", "H4"}})
        m5 = replay.bars_by_timeframe["M5"]
        # Use a common historical calendar anchor where available; no outcome
        # information is involved in this sanity snapshot.
        target = int(datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc).timestamp())
        eligible = [x for x in m5 if bar_end(x, "M5") <= target]
        if not eligible:
            eligible = m5[:-1]
        as_of = bar_end(eligible[-1], "M5")
        snapshot = feature_snapshot(replay, symbol, as_of, contract=data.get("contract"), window_limits={"M1": 1000, "M5": 600, "M15": 300, "H1": 100, "H4": 50}, structure_max_points=12)
        attention = attention_layer(snapshot)
        structure = snapshot["timeframes"][snapshot["provenance"]["structure_timeframe"]]
        results[symbol] = {
            "source_file": str(path),
            "as_of": snapshot["timestamp_iso"],
            "contract": data.get("contract"),
            "completed_bars": {tf: len(replay.history(tf, as_of)) for tf in snapshot["timeframes"]},
            "atr": structure["ema_context"].get("atr"),
            "ema_ordering": structure["ema_context"].get("ordering"),
            "sr_raw_count": attention["sr"]["raw_count"],
            "sr_top_count": len(attention["sr"]["top"]),
            "trendline_raw_count": attention["trendlines"]["raw_count"],
            "trendline_top_count": len(attention["trendlines"]["top"]),
            "channel_raw_count": attention["channels"]["raw_count"],
            "channel_cluster_count": attention["channels"]["cluster_count"],
            "channel_top_count": len(attention["channels"]["top"]),
            "boundary_distances_atr": {"support": attention["boundaries"].get("distance_to_support_atr"), "resistance": attention["boundaries"].get("distance_to_resistance_atr")},
            "provenance": {"future_outcomes_not_used": True, "same_pipeline": True, "missing_h4_derived_from_h1": "H4" not in json.loads(path.read_text())},
        }
    Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
