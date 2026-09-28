"""Freeze the Context exploratory split without reading validation bars."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from research.intraday_variants import variants
from strategy_backtest.models import fingerprint
from strategy_backtest.raw_ohlc_adapters import ContextRawOhlcEvaluator

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "artifacts/research/intraday-variants/historical_data_inventory.json"
OUT = ROOT / "artifacts/research/intraday-variants/context-exploratory"
SPLIT = "2026-01-01T00:00:00Z"


def main() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    context, _ = variants()
    coverage = inventory["recovered_existing_data"]["context_native_m5"]["coverage"]
    source_records = []
    for instrument in ("EURUSD", "GBPUSD"):
        source = coverage[instrument]
        source_records.append({
            "instrument": instrument,
            "provider": "MT5 / Exness-MT5Real9",
            "dataset_fingerprint": source["fingerprint"],
            "full_range": [source["start"], source["end"]],
            "discovery_range": [source["start"], SPLIT],
            "reserved_validation_range": [SPLIT, source["end"]],
            "full_bar_count": source["bar_count"],
            "unexplained_gap_count": source["unexplained_gap_count"],
        })
    payload = {
        "schema": "research.context_intraday_exploratory_protocol.v1",
        "status": "FROZEN_BEFORE_PERFORMANCE",
        "evidence_class": "EXPLORATORY_RAW_PIPELINE_PARENT_PARITY_UNPROVEN",
        "strategy_version": context.strategy.strategy_version_id,
        "parameter_set": context.parameter_set.canonical_payload(),
        "parameter_set_fingerprint": context.parameter_set.fingerprint,
        "engine_version": "strategy-backtest-core-v1",
        "engine_fingerprint": fingerprint({"engine": "strategy-backtest-core-v1", "raw_adapter": ContextRawOhlcEvaluator.VERSION}),
        "cost_model": {"model_id": "research-zero-engine-cost", "limitation": "generic engine scalar cost cannot consume per-bar spread; adapter geometry uses source spread"},
        "exit_hypothesis": "STRUCTURE_CAPPED_EXTENSION",
        "max_hold_minutes": 1440,
        "utc_boundary_behavior": "UTC_DAY_BOUNDARY_HYPOTHESIS",
        "split_timestamp": SPLIT,
        "source_records": source_records,
        "validation_outcomes_accessed": False,
        "production_changed": False,
        "broker_writes": 0,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "discovery_protocol.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
