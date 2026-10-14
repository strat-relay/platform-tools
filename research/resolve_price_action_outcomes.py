"""Resolve the committed accepted Price Action research entries in memory."""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Any

from strategy_backtest.models import fingerprint
from strategy_backtest.research_outcomes import (
    POLICY_VERSION, RESOLVER_VERSION, ResearchOutcomeEntry, resolve_research_entry,
)

DEFAULT_SOURCE = Path(os.environ.get("PRICE_ACTION_OUTCOME_SOURCE_ROOT", "artifacts/research/multitimeframe_structure_sniper/paged_native_full"))
DEFAULT_INPUT = Path("docs/research/price_action_historical/outcome_audit_v1/accepted_research_entries.jsonl")
DEFAULT_OUTPUT = Path("docs/research/price_action_historical/outcome_resolution_v1")


def _metrics(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    known = [row for row in outcomes if row["status"] in {"TP", "SL"}]
    rs = [float(row["realized_r"]) for row in known if row["realized_r"] is not None]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    equity = peak = drawdown = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    holds = [int(row["holding_seconds"]) / 60 for row in known if row.get("holding_seconds") is not None]
    return {"known_outcome_count": len(known), "tp_count": sum(row["status"] == "TP" for row in known),
            "sl_count": sum(row["status"] == "SL" for row in known),
            "win_rate_numerator": sum(row["status"] == "TP" for row in known),
            "win_rate_denominator": len(known),
            "win_rate": sum(row["status"] == "TP" for row in known) / len(known) if known else None,
            "gross_r_denominator": len(rs), "expectancy_r": mean(rs) if rs else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "maximum_drawdown_r": drawdown if rs else None,
            "holding_time_minutes_mean": mean(holds) if holds else None,
            "holding_time_minutes_median": median(holds) if holds else None,
            "valuation_uncertain_count": sum(row["valuation_status"] == "UNCERTAIN_GAP_OPEN_REFERENCE" for row in known),
            "drawdown_order": "entry-chronological resolved-only R sequence; overlapping episodes are not netted",
            "drawdown_scope": "independent theoretical episode diagnostic, not account equity"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    entries = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    cache: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}
    outcomes: list[dict[str, Any]] = []
    for raw in entries:
        instrument = raw["instrument"]
        if instrument not in cache:
            data_path = args.source_root / instrument / "M15.jsonl"
            metadata_path = args.source_root / instrument / "M15.metadata.json"
            bars = [json.loads(line) for line in data_path.read_text().splitlines() if line.strip()]
            metadata = json.loads(metadata_path.read_text())
            cache[instrument] = (bars, metadata.get("gap_ledger", []))
        bars, gaps = cache[instrument]
        provenance = raw.get("provenance", {})
        entry = ResearchOutcomeEntry(raw["episode_id"], raw["strategy_version"], instrument,
                                     raw["direction"], int(raw["entry_timestamp"]), float(raw["entry_price"]),
                                     float(raw["stop_price"]), float(raw["target_price"]),
                                     provenance.get("parameter_hash"), provenance.get("dependency_manifest_hash"),
                                     provenance.get("data_manifest_hash"),
                                     {"data_snapshot_id": provenance.get("data_snapshot_id"),
                                      "outcome_policy_hash": provenance.get("outcome_policy_hash")})
        outcomes.append(resolve_research_entry(entry, bars, expected_gaps=gaps).to_dict())
    outcomes.sort(key=lambda row: (row["entry_timestamp"], row["episode_id"]))
    by_strategy = {}
    by_instrument = {}
    for key in sorted({row["strategy_version"] for row in outcomes}):
        rows = [row for row in outcomes if row["strategy_version"] == key]
        by_strategy[key] = {"entry_count": len(rows), "status_counts": dict(Counter(row["status"] for row in rows)),
                            "metrics_resolved_known_outcomes": _metrics(rows)}
    for key in sorted({(row["strategy_version"], row["instrument"]) for row in outcomes}):
        rows = [row for row in outcomes if (row["strategy_version"], row["instrument"]) == key]
        by_instrument[f"{key[0]}|{key[1]}"] = {"entry_count": len(rows),
            "status_counts": dict(Counter(row["status"] for row in rows)), "metrics_resolved_known_outcomes": _metrics(rows)}
    report = {"schema": "price-action-outcome-resolution-report-v1", "policy_version": POLICY_VERSION,
              "resolver_version": RESOLVER_VERSION, "adapter_version": "price-action-research-entry-adapter-v1",
              "input_entry_count": len(entries), "output_outcome_count": len(outcomes),
              "status_counts": dict(Counter(row["status"] for row in outcomes)),
              "by_strategy": by_strategy, "by_strategy_instrument": by_instrument,
              "protocol": {"entry_price_basis": "accepted next-open reference price",
                           "cost_basis": "gross reference-price; no bid/ask/commission/slippage",
                           "max_hold": "none; OPEN_AT_DATASET_END is explicit",
                           "same_candle": "AMBIGUOUS_INTRABAR without lower-timeframe evidence",
                           "gap_policy": "barrier at reopening open can resolve with uncertain open valuation; otherwise DATA_GAP_UNRESOLVED",
                           "dataset_end": "no future price inferred", "optimization": False},
              "resolution_fingerprint": fingerprint(outcomes)}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "resolution_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    with (args.output_dir / "resolved_outcome_ledger.jsonl").open("w") as handle:
        for row in outcomes:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps({"output_dir": str(args.output_dir), "input_entry_count": len(entries),
                      "status_counts": report["status_counts"], "resolution_fingerprint": report["resolution_fingerprint"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
