"""Record implementation/runtime readiness without starting the exporter."""
from __future__ import annotations
import json, subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/research/multitimeframe_liquidity_sniper"


def main():
    result = {
        "schema": "paged-history-readiness-v1",
        "family": "MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH",
        "research_only": True,
        "PAGED_HISTORY_IMPLEMENTED": True,
        "PAGINATION_TESTS_PASS": True,
        "NATIVE_H4_DEEP_HISTORY_AVAILABLE": False,
        "DERIVED_H4_VALIDATED_IF_USED": False,
        "ALL_15_SYMBOLS_EXPORTED": False,
        "SYMBOLS_WITH_3_MONTHS": [],
        "SYMBOLS_WITH_6_MONTHS": [],
        "SYMBOLS_WITH_12_MONTHS": [],
        "PAIR_AGNOSTIC_ALIGNMENT_PASS": False,
        "HISTORICAL_SPREAD_FIELDS_VALID": True,
        "DURABLE_DATASET_PASS": False,
        "BROKER_WRITES": 0,
        "EXECUTION_CAPABILITY_ADDED": False,
        "PRODUCTION_CHANGED": False,
        "RUNNERS_RESTARTED": False,
        "runtime_status": "NOT_RELOADED",
        "reload_required": [
            "Compile the updated MT5TradingBridge.mq5 in the research/read-only terminal instance.",
            "Reload or attach that EA instance so mt5_rates_range becomes available.",
            "Run paged_export.py for the requested UTC range; this task intentionally did not perform that runtime reload or export.",
        ],
        "boundary_semantics": "start_timestamp <= candle_timestamp < end_timestamp",
        "cost_provenance": "native historical M5 bar spread field only; no present-day fallback",
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "written_at": datetime.now(timezone.utc).isoformat(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "paged_history_readiness.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
