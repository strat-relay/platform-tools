from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterator

from .attention import attention_layer
from .config import DEFAULT_TIMEFRAMES, ResearchTimeframes
from .data import CausalReplay, bar_end, iso
from .normalization import normalization_audit
from .outcomes import future_outcome_labels
from .patterns import detect_patterns
from .replay import feature_snapshot
from .retracement import measure_retracement


def build_phase2_ledger(
    replay: CausalReplay,
    symbol: str,
    start: int,
    end: int,
    execution_timeframe: str | None = None,
    timeframes: ResearchTimeframes = DEFAULT_TIMEFRAMES,
    attention_limit: int = 3,
    window_limits: dict[str, int] | None = None,
    structure_max_points: int = 12,
) -> Iterator[dict[str, Any]]:
    tf = execution_timeframe or timeframes.execution
    bars = replay.bars_by_timeframe.get(tf, [])
    completed_by_index: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        completed_by_index.append(bar)
        as_of = bar_end(bar, tf)
        if not start <= as_of <= end:
            continue
        events = detect_patterns(bars, tf, as_of, symbol, completed_override=completed_by_index)
        if not events:
            continue
        snapshot = feature_snapshot(replay, symbol, as_of, timeframes=timeframes, window_limits=window_limits, structure_max_points=structure_max_points)
        attention = attention_layer(snapshot, attention_limit)
        structure_tf = snapshot["provenance"]["structure_timeframe"]
        structure = snapshot["timeframes"][structure_tf]
        atr_value = structure["ema_context"].get("atr")
        ema_values = structure["ema_context"].get("ema_values_completed", {})
        zones = structure["sr_context"].get("zones", [])
        future_bars = bars[index + 1:index + 25]
        for event in events:
            direction = "UP" if event["direction"] == "LONG" else "DOWN"
            yield {
                "event_id": event["event_id"],
                "symbol": symbol,
                "event_timestamp": event["timestamp"],
                "event_timestamp_iso": iso(event["timestamp"]),
                "event": event,
                "context_snapshot": snapshot,
                "attention_layer": attention,
                "normalization_audit": normalization_audit(snapshot),
                "retracement_measurements": measure_retracement(bar, future_bars, direction, atr_value, ema_values, zones),
                "future_outcome_labels": future_outcome_labels(bar, future_bars, direction, atr_value),
                "ledger_provenance": {"feature_generation_as_of": as_of, "features_and_attention_causal": True, "future_labels_separate": True, "development_only": True, "validation_status": "previously_exposed_periods_are_not_untouched"},
            }


def write_phase2_ledger(rows: Iterator[dict[str, Any]], jsonl_path: str | Path, csv_path: str | Path | None = None, snapshot_path: str | Path | None = None) -> int:
    count = 0
    written_snapshots: set[str] = set()
    with Path(jsonl_path).open("w", encoding="utf-8") as handle:
        csv_handle = Path(csv_path).open("w", newline="", encoding="utf-8") if csv_path else None
        snapshot_handle = Path(snapshot_path).open("w", encoding="utf-8") if snapshot_path else None
        writer = csv.DictWriter(csv_handle, fieldnames=["event_id", "symbol", "timestamp", "pattern", "direction", "structure_timeframe", "raw_sr", "raw_trendlines", "raw_channels", "attention_sr", "attention_trendlines", "attention_channels"]) if csv_handle else None
        if writer:
            writer.writeheader()
        try:
            for row in rows:
                output_row = row
                if snapshot_handle:
                    snapshot_id = f"{row['symbol']}|{row['event_timestamp']}"
                    if snapshot_id not in written_snapshots:
                        snapshot_handle.write(json.dumps({"snapshot_id": snapshot_id, "timestamp": row["event_timestamp"], "context_snapshot": row["context_snapshot"], "attention_layer": row["attention_layer"], "normalization_audit": row["normalization_audit"]}, separators=(",", ":"), sort_keys=True) + "\n")
                        written_snapshots.add(snapshot_id)
                    output_row = {k: v for k, v in row.items() if k not in {"context_snapshot", "attention_layer", "normalization_audit"}}
                    output_row["context_snapshot_ref"] = snapshot_id
                    output_row["attention_layer_summary"] = {"structure_timeframe": row["attention_layer"]["structure_timeframe"], "raw_sr": row["attention_layer"]["sr"]["raw_count"], "raw_trendlines": row["attention_layer"]["trendlines"]["raw_count"], "raw_channels": row["attention_layer"]["channels"]["raw_count"]}
                handle.write(json.dumps(output_row, separators=(",", ":"), sort_keys=True) + "\n")
                if writer:
                    writer.writerow({"event_id": row["event_id"], "symbol": row["symbol"], "timestamp": row["event_timestamp_iso"], "pattern": row["event"]["pattern"], "direction": row["event"]["direction"], "structure_timeframe": row["attention_layer"]["structure_timeframe"], "raw_sr": row["attention_layer"]["sr"]["raw_count"], "raw_trendlines": row["attention_layer"]["trendlines"]["raw_count"], "raw_channels": row["attention_layer"]["channels"]["raw_count"], "attention_sr": len(row["attention_layer"]["sr"]["top"]), "attention_trendlines": len(row["attention_layer"]["trendlines"]["top"]), "attention_channels": len(row["attention_layer"]["channels"]["top"])})
                count += 1
        finally:
            if csv_handle:
                csv_handle.close()
            if snapshot_handle:
                snapshot_handle.close()
    return count
