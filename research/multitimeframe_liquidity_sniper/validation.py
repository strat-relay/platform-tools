"""Bounded, offline validation harness for the sniper research family.

The caller supplies a mapping of pair -> timeframe candles.  This module never
fetches data and never writes unless the caller explicitly asks it to persist a
ledger/report path.  It is intentionally neutral: no parameter search occurs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .config import CONTROL_GROUPS, RESEARCH_MANIFEST
from .engine import (TF_SECONDS, _ts, align_completed_timeframes,
                     chronological_splits, inspect_candidate, ledger_row,
                     summarize_controls)


def source_configuration_hash() -> str:
    return hashlib.sha256(json.dumps(RESEARCH_MANIFEST, sort_keys=True, default=list).encode()).hexdigest()


def validate_alignment(data: Mapping[str, list[Mapping[str, Any]]], decision_timestamp: Any) -> dict[str, Any]:
    aligned = align_completed_timeframes(data, decision_timestamp)
    ts = _ts(decision_timestamp)
    checks = {}
    for tf, row in aligned.items():
        checks[tf] = {"source_timestamp": _ts(row["time"]) if row else None,
                      "source_close_timestamp": _ts(row["time"]) + TF_SECONDS[tf] if row else None,
                      "completed": bool(row and _ts(row["time"]) + TF_SECONDS[tf] <= ts)}
    return {"decision_timestamp": ts, "timeframes": checks,
            "pass": all(not row or row["completed"] for row in checks.values())}


def bounded_replay(dataset: Mapping[str, Mapping[str, list[Mapping[str, Any]]]],
                   decision_timestamps: Mapping[str, list[Any]],
                   neutral_params: Mapping[str, Any], *, ledger_path: str | Path | None = None,
                   report_path: str | Path | None = None) -> dict[str, Any]:
    """Replay one neutral configuration, preserving rows and split boundaries."""
    rows = []
    traces = []
    for symbol, data in dataset.items():
        for decision_timestamp in decision_timestamps.get(symbol, []):
            candidate = inspect_candidate(symbol, data, decision_timestamp, neutral_params)
            candidate["h4_context"] = candidate.get("h4_context")
            candidate["h1_context"] = candidate.get("h1_context")
            candidate["alignment"] = validate_alignment(data, decision_timestamp)
            candidate["strategy_family"] = RESEARCH_MANIFEST["family"]
            candidate["configuration_hash"] = source_configuration_hash()
            rows.append(ledger_row(symbol, candidate, control_level="A"))
            traces.append(candidate)
    result = {
        "strategy_family": RESEARCH_MANIFEST["family"],
        "mode": "RESEARCH_ONLY",
        "configuration_hash": source_configuration_hash(),
        "pairs": sorted(dataset),
        "row_count": len(rows),
        "controls": summarize_controls(rows),
        "chronological_splits": {k: len(v) for k, v in chronological_splits(rows).items()} if rows else {},
        "continuation_count": sum(x.get("continuation_or_reversal") == "HTF_CONTINUATION" for x in rows),
        "reversal_count": sum(x.get("continuation_or_reversal") == "HTF_REVERSAL_TRANSITION" for x in rows),
        "alignment_pass": all(x["alignment"]["pass"] for x in traces),
        "broker_writes": 0,
    }
    if ledger_path is not None:
        path = Path(ledger_path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        result["ledger_path"] = str(path)
    if report_path is not None:
        path = Path(report_path); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        result["report_path"] = str(path)
    return result
