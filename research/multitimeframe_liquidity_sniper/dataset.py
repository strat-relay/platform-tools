"""Historical dataset acquisition and strict quality checks for research only."""
from __future__ import annotations
import hashlib, json
from datetime import datetime, timezone
from typing import Any, Mapping

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}

def _valid_bar(x: Mapping[str, Any]) -> bool:
    try:
        o,h,l,c = map(float, (x["open"], x["high"], x["low"], x["close"]))
        return h >= max(o,c) and l <= min(o,c) and h >= l
    except (KeyError, TypeError, ValueError): return False

def audit_timeframe(rows: list[Mapping[str, Any]], timeframe: str) -> dict[str, Any]:
    times = [int(x["time"]) for x in rows]
    duplicates = len(times) - len(set(times)); sorted_ok = times == sorted(times)
    gaps = []
    for a,b in zip(sorted(set(times)), sorted(set(times))[1:]):
        if b-a != TF_SECONDS[timeframe]: gaps.append({"from": a, "to": b, "missing_bars": max(0, (b-a)//TF_SECONDS[timeframe]-1)})
    invalid = sum(not _valid_bar(x) for x in rows)
    return {"first_timestamp": times[0] if times else None, "last_timestamp": times[-1] if times else None,
            "bar_count": len(rows), "missing_bar_count": sum(x["missing_bars"] for x in gaps),
            "duplicate_bar_count": duplicates, "invalid_ohlc_count": invalid, "sorted": sorted_ok,
            "gaps": gaps, "quality_pass": bool(rows) and sorted_ok and duplicates == 0 and invalid == 0}

def align_common_window(symbol_data: Mapping[str, Mapping[str, list[Mapping[str, Any]]]]) -> dict[str, Any]:
    starts = [int(rows[0]["time"]) for d in symbol_data.values() for rows in d.values() if rows]
    ends = [int(rows[-1]["time"]) for d in symbol_data.values() for rows in d.values() if rows]
    start, end = max(starts), min(ends)
    out = {}
    for symbol, frames in symbol_data.items():
        out[symbol] = {tf: [x for x in rows if start <= int(x["time"]) <= end] for tf, rows in frames.items()}
    return {"start": start, "end": end, "symbols": out}

def dataset_manifest(dataset: Mapping[str, Mapping[str, list[Mapping[str, Any]]]], *, source: str) -> dict[str, Any]:
    symbols = {}
    for symbol, frames in dataset.items():
        symbols[symbol] = {tf: {"symbol": symbol, "timeframe": tf, "source": source, "derivation": "NATIVE",
                                **audit_timeframe(list(rows), tf)} for tf, rows in frames.items()}
    digest = hashlib.sha256(json.dumps(symbols, sort_keys=True, default=str).encode()).hexdigest()
    return {"schema": "multitimeframe-research-dataset-v1", "source": source,
            "gap_handling": "DATA_GAP_INVALID_SKIP_SETUP_WINDOW_NO_INTERPOLATION",
            "symbols": symbols, "dataset_hash": digest}
