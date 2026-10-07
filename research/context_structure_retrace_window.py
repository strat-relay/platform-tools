"""Research-only append-only decision ledger and V1/V2 comparison report.

The forward runner or an operator export can write one record per evaluation. This module
does not evaluate a strategy, publish signals, create execution intents, or contact a broker.
It only makes the evidence contract and deterministic aggregation explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "context-v1-v2-decision-ledger-v1"
REQUIRED_FIELDS = ("strategy_id", "strategy_version", "strategy_instance", "config_hash",
                   "instrument", "decision_timestamp", "market_cutoff_timestamp",
                   "market_snapshot_hash", "candidate_id", "signal_id", "direction", "entry",
                   "stop", "target", "risk_distance", "reward_distance", "planned_r",
                   "target_source", "decision", "rejection_reason", "evaluation_id")
OUTCOME_FIELDS = ("strategy_outcome", "strategy_r", "execution_intent", "execution_decision",
                  "broker_position_state", "broker_realized_pnl")
BUCKETS = (("1.00-1.24", 1.0, 1.25), ("1.25-1.49", 1.25, 1.5),
           ("1.50-1.99", 1.5, 2.0), (">=2.00", 2.0, None))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import datetime
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


class DecisionLedger:
    """Write hash-chained JSONL records; an existing ledger cannot be truncated."""

    def __init__(self, path: Path, *, capture_hash: str, t0: str | int | float | None = None):
        self.path = path
        self.capture_hash = capture_hash
        self.t0 = t0 if t0 is not None else os.environ.get("CONTEXT_T0")
        self.previous_hash = self._last_hash()

    def _last_hash(self) -> str | None:
        if not self.path.exists():
            return None
        last = None
        with self.path.open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    last = json.loads(line)["record_hash"]
        return last

    def append(self, record: dict[str, Any]) -> dict[str, Any]:
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            raise ValueError(f"missing decision fields: {', '.join(missing)}")
        payload = {**record, "schema": SCHEMA, "capture_hash": self.capture_hash,
                   "capture_t0": self.t0, "previous_record_hash": self.previous_hash}
        payload["record_hash"] = hashlib.sha256(_canonical(payload)).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        self.previous_hash = payload["record_hash"]
        return payload


def _records(path: Path) -> list[dict[str, Any]]:
    rows = []
    previous = None
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("schema") != SCHEMA or row.get("previous_record_hash") != previous:
                raise ValueError(f"invalid ledger chain at line {line_number}")
            expected = row.pop("record_hash", None)
            if expected != hashlib.sha256(_canonical(row)).hexdigest():
                raise ValueError(f"record hash mismatch at line {line_number}")
            row["record_hash"] = expected
            previous = expected
            rows.append(row)
    return rows


def _max_drawdown(values: Iterable[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = [row for row in rows if row.get("strategy_outcome")]
    rs = [float(row["strategy_r"]) for row in outcomes if row.get("strategy_r") is not None]
    wins = [value for value in rs if value > 0]
    losses = [value for value in rs if value < 0]
    target_hits = sum(row.get("strategy_outcome") == "TARGET_HIT" for row in outcomes)
    stops = sum(row.get("strategy_outcome") == "STOPPED" for row in outcomes)
    timeouts = sum(row.get("strategy_outcome") == "TIME_EXIT" for row in outcomes)
    ambiguous = sum(row.get("strategy_outcome") == "AMBIGUOUS_INTRABAR" for row in outcomes)
    planned = [float(row["planned_r"]) for row in rows if row.get("planned_r") is not None]
    return {"evaluations": len(rows), "setups": sum(row.get("candidate_id") is not None for row in rows),
            "signals": sum(row.get("signal_id") is not None for row in rows),
            "rr_rejected": sum(row.get("rejection_reason") == "RR_BELOW_MINIMUM" for row in rows),
            "target_hits": target_hits, "stops": stops, "time_exits": timeouts,
            "ambiguous": ambiguous, "win_rate": len(wins) / len(rs) if rs else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "average_r": statistics.fmean(rs) if rs else None, "total_r": sum(rs),
            "median_planned_r": statistics.median(planned) if planned else None,
            "max_drawdown_r": _max_drawdown(rs), "signals_per_day": None}


def report(path: Path, *, t0: str | int | float | None = None) -> dict[str, Any]:
    rows = _records(path)
    boundary = t0 if t0 is not None else os.environ.get("CONTEXT_T0")
    if boundary is None:
        boundary = next((row.get("capture_t0") for row in rows if row.get("capture_t0") is not None), None)
    if boundary is None:
        raise ValueError("T0 boundary is required for a V1/V2 report")
    boundary_epoch = _epoch(boundary)
    historical_count = sum(_epoch(row["decision_timestamp"]) < boundary_epoch for row in rows)
    rows = [row for row in rows if _epoch(row["decision_timestamp"]) >= boundary_epoch]
    by_strategy = defaultdict(list)
    for row in rows:
        by_strategy[row["strategy_id"]].append(row)
    v1 = by_strategy.get("CONTEXT_STRUCTURE_RETRACE_V1", [])
    v2 = by_strategy.get("CONTEXT_STRUCTURE_RETRACE_V2", [])
    removed_ids = {row.get("candidate_id") for row in v2
                   if row.get("candidate_id") and row.get("rejection_reason") == "RR_BELOW_MINIMUM"}
    removed = [row for row in v1 if row.get("candidate_id") in removed_ids]
    breakdown = {}
    for instrument in sorted({row["instrument"] for row in v2}):
        for direction in sorted({row["direction"] for row in v2 if row["instrument"] == instrument}):
            subset = [row for row in v2 if row["instrument"] == instrument and row["direction"] == direction]
            breakdown[f"{instrument}:{direction}"] = _metrics(subset)
    buckets = {}
    for label, lower, upper in BUCKETS:
        buckets[label] = _metrics([row for row in v2 if row.get("planned_r") is not None and
                                   float(row["planned_r"]) >= lower and (upper is None or float(row["planned_r"]) < upper)])
    return {"schema": "context-v1-v2-comparison-report-v1", "ledger": str(path),
            "t0": boundary, "historical_records_excluded": historical_count,
            "experiment_records": len(rows),
            "v1": _metrics(v1), "v2": _metrics(v2), "v1_removed_by_v2": _metrics(removed),
            "v2_by_instrument_direction": breakdown, "v2_by_planned_r_bucket": buckets,
            "research_only": True, "broker_writes": 0}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--t0", default=None)
    args = parser.parse_args()
    payload = json.dumps(report(args.ledger, t0=args.t0), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
