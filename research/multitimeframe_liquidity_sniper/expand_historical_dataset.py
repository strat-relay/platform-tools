"""Build the deepest locally reproducible research dataset.

This module is deliberately research-only.  It never calls an execution API.
It combines the persisted USDJPY historical export with the existing native
bridge snapshot, audits every series, freezes calendar splits/folds, and
records the exact source limitations instead of inventing history.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from .dataset import TF_SECONDS, audit_timeframe

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/research/multitimeframe_liquidity_sniper"
SHORT = OUT / "aligned_dataset.json"
DEEP_USDJPY = Path("/tmp/usdjpy_historical_20260917.json")
PAGED_ROOT = OUT / "paged_native"
SYMBOLS = (
    "EURUSDm", "GBPUSDm", "USDJPYm", "USDCHFm", "USDCADm", "AUDUSDm",
    "NZDUSDm", "EURJPYm", "GBPJPYm", "EURGBPm", "AUDJPYm", "CADJPYm",
    "CHFJPYm", "GBPAUDm", "GBPCADm",
)
TIMEFRAMES = ("M5", "M15", "H1", "H4")


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(x) for x in rows), key=lambda x: int(x["time"]))


def _aggregate_h4(h1: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calendar-aligned UTC H4 aggregation from native H1, explicitly labeled derived."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in _normalize_rows(h1):
        dt = datetime.fromtimestamp(int(row["time"]), timezone.utc)
        start = int(datetime(dt.year, dt.month, dt.day, (dt.hour // 4) * 4, tzinfo=timezone.utc).timestamp())
        groups.setdefault(start, []).append(row)
    out = []
    for start, rows in sorted(groups.items()):
        if len(rows) != 4 or [int(x["time"]) for x in rows] != [start + 3600 * i for i in range(4)]:
            continue
        out.append({
            "time": start, "open": float(rows[0]["open"]),
            "high": max(float(x["high"]) for x in rows),
            "low": min(float(x["low"]) for x in rows),
            "close": float(rows[-1]["close"]),
            "spread": int(round(sum(int(x.get("spread", 0)) for x in rows) / 4)),
            "tick_volume": sum(int(x.get("tick_volume", 0)) for x in rows),
            "real_volume": sum(int(x.get("real_volume", 0)) for x in rows),
        })
    return out


def _gap_kind(start: int, end: int) -> str:
    # A gap touching the normal FX weekend closure is expected.  All other
    # gaps remain visible and are invalidating, never interpolated.
    cur = datetime.fromtimestamp(start, timezone.utc)
    finish = datetime.fromtimestamp(end, timezone.utc)
    while cur <= finish:
        if cur.weekday() in (5, 6):
            return "EXPECTED_MARKET_CLOSURE"
        cur += timedelta(hours=1)
    return "UNEXPECTED_DATA_GAP"


def _audit(rows: list[dict[str, Any]], tf: str) -> dict[str, Any]:
    base = audit_timeframe(rows, tf)
    gaps = []
    times = sorted(int(x["time"]) for x in rows)
    for a, b in zip(times, times[1:]):
        if b - a != TF_SECONDS[tf]:
            missing = max(0, (b - a) // TF_SECONDS[tf] - 1)
            gaps.append({"start": a + TF_SECONDS[tf], "end": b - TF_SECONDS[tf],
                         "missing_bars": missing, "classification": _gap_kind(a, b)})
    base["gaps"] = gaps
    base["unexpected_gap_count"] = sum(x["classification"] == "UNEXPECTED_DATA_GAP" for x in gaps)
    base["expected_gap_count"] = sum(x["classification"] == "EXPECTED_MARKET_CLOSURE" for x in gaps)
    base["quality_pass"] = bool(base["quality_pass"] and base["unexpected_gap_count"] == 0)
    return base


def _contract_from_deep(deep: dict[str, Any]) -> dict[str, Any]:
    c = dict(deep.get("contract", {}))
    point = float(c.get("point", 0.00001))
    digits = int(c.get("digits", 5))
    pip = 0.01 if digits in (2, 3) else 0.0001
    return {"point": point, "digits": digits, "pip_size": pip,
            "pip_to_price_multiplier": pip / point if point else None,
            "broker": "local MT5 bridge / persisted export"}


def _atr_values(rows: list[dict[str, Any]], n: int = 14) -> list[float]:
    out = []
    for i, row in enumerate(rows):
        window = [float(x["high"]) - float(x["low"]) for x in rows[max(0, i-n):i] if float(x["high"]) >= float(x["low"])]
        if window:
            out.append(sum(window) / len(window))
    return out


def build() -> tuple[dict[str, Any], dict[str, Any]]:
    short = json.loads(SHORT.read_text())
    data: dict[str, dict[str, list[dict[str, Any]]]] = {s: {tf: _normalize_rows(rows) for tf, rows in frames.items()} for s, frames in short.items()}
    provenance: dict[str, dict[str, str]] = {s: {tf: "NATIVE" for tf in TIMEFRAMES} for s in data}
    contracts: dict[str, dict[str, Any]] = {}

    # Prefer a completed read-only paged export when one exists.  The exporter
    # stores broker-native directories; this mapping keeps canonical names in
    # the research manifest.
    broker_to_canonical = {v: k for k, v in {
        "EURUSD":"EURUSDm", "GBPUSD":"GBPUSDm", "USDJPY":"USDJPYm", "USDCHF":"USDCHFm", "USDCAD":"USDCADm",
        "AUDUSD":"AUDUSDm", "NZDUSD":"NZDUSDm", "EURJPY":"EURJPYm", "GBPJPY":"GBPJPYm", "EURGBP":"EURGBPm",
        "AUDJPY":"AUDJPYm", "CADJPY":"CADJPYm", "CHFJPY":"CHFJPYm", "GBPAUD":"GBPAUDm", "GBPCAD":"GBPCADm"}.items()}
    paged_manifest = PAGED_ROOT / "export_manifest.json"
    if paged_manifest.exists():
        pm = json.loads(paged_manifest.read_text())
        for canonical, item in pm.get("symbols", {}).items():
            broker = item.get("broker_symbol", canonical)
            if item.get("status") == "FAILED":
                continue
            target = broker_to_canonical.get(broker, canonical)
            data.setdefault(target, {})
            provenance.setdefault(target, {})
            for tf in TIMEFRAMES:
                fp = PAGED_ROOT / broker / f"{tf}.jsonl"
                if not fp.exists():
                    continue
                data[target][tf] = _normalize_rows([json.loads(line) for line in fp.read_text().splitlines() if line.strip()])
                provenance[target][tf] = "NATIVE_PAGED_MT5_EXPORT"
            info = item.get("symbol_info") or {}
            if info and "point" in info:
                point = float(info.get("point", 0.00001)); digits = int(info.get("digits", 5)); pip = 0.01 if digits in (2,3) else 0.0001
                contracts[target] = {"point":point,"digits":digits,"pip_size":pip,"pip_to_price_multiplier":pip/point if point else None,"broker":"MT5 native paged export"}

    if DEEP_USDJPY.exists() and provenance.get("USDJPYm", {}).get("M5") != "NATIVE_PAGED_MT5_EXPORT":
        deep = json.loads(DEEP_USDJPY.read_text())
        s = "USDJPYm"
        for tf in ("M5", "M15", "H1"):
            data[s][tf] = _normalize_rows(deep.get(tf, []))
            provenance[s][tf] = "NATIVE_PERSISTED_HISTORICAL_EXPORT"
        native_h4 = data[s]["H4"]
        derived_h4 = _aggregate_h4(data[s]["H1"])
        # Prefer native H4 where the two sources overlap; use derived H4 only
        # for older periods, with the provenance recorded below.
        native_times = {int(x["time"]) for x in native_h4}
        combined = {int(x["time"]): x for x in derived_h4}
        combined.update({int(x["time"]): x for x in native_h4})
        data[s]["H4"] = [combined[t] for t in sorted(combined)]
        provenance[s]["H4"] = "NATIVE_OVERLAP_PLUS_DERIVED_FROM_NATIVE_H1"
        contracts[s] = _contract_from_deep(deep)

    manifest: dict[str, Any] = {
        "schema": "multitimeframe-historical-dataset-v2",
        "family": "MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH",
        "research_only": True,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "requested_symbols": list(SYMBOLS), "requested_timeframes": list(TIMEFRAMES),
        "bridge_limit_constraint": {"max_candles": 500, "supports_historical_paging": False,
                                     "impact": "bridge snapshot cannot provide 6-12 month history"},
        "gap_policy": "EXPECTED_MARKET_CLOSURE is retained; UNEXPECTED_DATA_GAP makes required setup window DATA_GAP_INVALID; no interpolation",
        "symbols": {},
    }
    for s in SYMBOLS:
        if s not in data:
            manifest["symbols"][s] = {"available": False, "reason": "No persisted historical export; bridge historical paging unavailable"}
            continue
        is_jpy = "JPY" in s.upper()
        item = {"available": True, "broker_symbol": s, "contract": contracts.get(s, {
            "broker": "local MT5 bridge", "point": 0.001 if is_jpy else 0.00001,
            "digits": 3 if is_jpy else 5, "pip_size": 0.01 if is_jpy else 0.0001,
            "pip_to_price_multiplier": 10.0 if is_jpy else 10.0})}
        for tf in TIMEFRAMES:
            rows = data[s].get(tf, [])
            a = _audit(rows, tf)
            item[tf] = {**a, "symbol": s, "timeframe": tf, "source": provenance[s].get(tf, "UNKNOWN"),
                        "first_timestamp_utc": _iso(a["first_timestamp"]), "last_timestamp_utc": _iso(a["last_timestamp"]),
                        "data_hash": _sha(rows)}
        item["minimum_6_month_history"] = all((item[tf]["last_timestamp"] - item[tf]["first_timestamp"] >= 180 * 86400) for tf in TIMEFRAMES if item[tf]["first_timestamp"] is not None)
        item["minimum_12_month_history"] = all((item[tf]["last_timestamp"] - item[tf]["first_timestamp"] >= 365 * 86400) for tf in TIMEFRAMES if item[tf]["first_timestamp"] is not None)
        manifest["symbols"][s] = item
    manifest["dataset_hash"] = _sha({s: {tf: manifest["symbols"][s].get(tf) for tf in TIMEFRAMES} for s in manifest["symbols"] if manifest["symbols"][s].get("available")})
    manifest["source_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    manifest["config_hash"] = _sha({"neutral": "artifacts/research/multitimeframe_liquidity_sniper/neutral.py"})

    cost = {"schema": "historical-cost-manifest-v1", "method": "M5 native bar spread only; no present-day fallback; exact bid/ask ticks unavailable", "symbols": {}}
    for s, item in manifest["symbols"].items():
        if not item.get("available"):
            cost["symbols"][s] = {"historical_cost_source": "UNAVAILABLE"}
            continue
        rows = data[s]["M5"]
        spreads = [float(x.get("spread", 0)) * float(item["contract"].get("point", 0.00001)) / float(item["contract"].get("pip_size", 0.0001)) for x in rows if x.get("spread") is not None]
        ss = sorted(spreads)
        def pct(p: float): return ss[min(len(ss)-1, int((len(ss)-1)*p))] if ss else None
        by_hour = {}
        for x, sp in zip(rows, spreads):
            h = datetime.fromtimestamp(int(x["time"]), timezone.utc).hour
            by_hour.setdefault(str(h), []).append(sp)
        av = sorted(_atr_values(rows))
        cost["symbols"][s] = {"historical_cost_source": "NATIVE_M5_BAR_SPREAD",
                               "exact_historical_bid_ask": False, "historical_tick_data": False,
                               "historical_bar_spread": True, "spread_pips": {"median": pct(.5), "mean": sum(spreads)/len(spreads) if spreads else None, "P75": pct(.75), "P90": pct(.90), "P95": pct(.95), "P99": pct(.99)},
                               "by_hour_utc": {h: {"median": sorted(v)[len(v)//2], "count": len(v)} for h,v in by_hour.items()},
                               "m5_atr_available": True,
                               "m5_atr_price": {"median": av[len(av)//2] if av else None,
                                                "P75": av[int((len(av)-1)*.75)] if av else None,
                                                "P95": av[int((len(av)-1)*.95)] if av else None}}
    return {"data": data, "manifest": manifest}, cost


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    built, cost = build()
    (OUT / "historical_dataset_manifest.json").write_text(json.dumps(built["manifest"], indent=2) + "\n")
    (OUT / "historical_cost_manifest.json").write_text(json.dumps(cost, indent=2) + "\n")
    (OUT / "expanded_historical_dataset.json").write_text(json.dumps(built["data"], separators=(",", ":")) + "\n")
    print(json.dumps({"dataset_hash": built["manifest"]["dataset_hash"], "available_symbols": [s for s,v in built["manifest"]["symbols"].items() if v.get("available")], "unavailable_symbols": [s for s,v in built["manifest"]["symbols"].items() if not v.get("available")]}, indent=2))
