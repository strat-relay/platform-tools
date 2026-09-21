"""Read-only native MT5 historical exporter.

The exporter only calls ``mt5_rates_range`` and ``mt5_symbol_info``.  It never
has access to order tools and writes each series atomically under a versioned
research dataset directory.
"""
from __future__ import annotations

import argparse, hashlib, json, os, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

from paper_runner import call_bridge
from .dataset import TF_SECONDS, audit_timeframe
from .paging import merge_pages, normalize_page, page_ranges
from .symbol_mapping import candidate_symbols, resolve_from_read_probes

DEFAULT_SYMBOLS = (
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURJPY",
    "GBPJPY", "EURGBP", "AUDJPY", "CADJPY", "CHFJPY", "GBPAUD", "GBPCAD",
)
TIMEFRAMES = ("M5", "M15", "H1", "H4")


def parse_ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def atomic_write(path: Path, content: str):
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(content)
    os.replace(tmp, path)


def fetch_page(mcp_url, symbol, tf, start, end, page_size, completed_only, retries):
    args = {"symbol": symbol, "timeframe": tf, "start_timestamp": int(start), "end_timestamp": int(end), "page_size": int(page_size), "completed_only": bool(completed_only)}
    last = None
    for attempt in range(retries + 1):
        try:
            response = call_bridge(mcp_url, "mt5_rates_range", args)
            rows = response.get("rates", response.get("data", {}).get("rates", []))
            return normalize_page(rows, start, end, tf)
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"paged read failed for {symbol} {tf} [{start},{end}): {last}")


def export_series(mcp_url, canonical, broker_symbol, tf, start, end, out_dir, page_size, retries, completed_only, incremental):
    series_dir = out_dir / broker_symbol
    series_dir.mkdir(parents=True, exist_ok=True)
    data_path = series_dir / f"{tf}.jsonl"
    meta_path = series_dir / f"{tf}.metadata.json"
    old = []
    old_meta = json.loads(meta_path.read_text()) if incremental and meta_path.exists() else {}
    if incremental and data_path.exists():
        old = [json.loads(line) for line in data_path.read_text().splitlines() if line.strip()]
        if old_meta.get("last_timestamp") is not None:
            start = max(start, int(old_meta["last_timestamp"]) - TF_SECONDS[tf] * 2)
    pages = []
    ranges = page_ranges(start, end, tf, page_size)
    for a, b in ranges:
        pages.append(fetch_page(mcp_url, broker_symbol, tf, a, b, page_size, completed_only, retries))
    rows = merge_pages([old, *pages])
    rows = [x for x in rows if start <= int(x["time"]) < end or (old and int(x["time"]) < start)]
    rows.sort(key=lambda x: int(x["time"]))
    # Defensive final deduplication, with a stable last-write-wins rule.
    by_time = {int(x["time"]): x for x in rows}; rows = [by_time[t] for t in sorted(by_time)]
    body = "".join(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n" for x in rows)
    atomic_write(data_path, body)
    audit = audit_timeframe(rows, tf)
    digest = hashlib.sha256(body.encode()).hexdigest()
    metadata = {"schema":"native-mt5-paged-series-v1", "canonical_symbol":canonical, "broker_symbol":broker_symbol,
                "timeframe":tf, "boundary_semantics":"start <= candle_timestamp < end", "completed_only":completed_only,
                "requested_start_timestamp":start, "requested_end_timestamp":end, "first_timestamp":audit["first_timestamp"],
                "last_timestamp":audit["last_timestamp"], "bar_count":len(rows), "expected_page_count":len(ranges),
                "actual_page_count":len(pages), "duplicate_count":audit["duplicate_bar_count"],
                "unexpected_gap_count":sum(1 for g in audit["gaps"] if g.get("classification") == "UNEXPECTED_DATA_GAP"),
                "gap_ledger":audit["gaps"], "source":"NATIVE_MT5", "retrieved_at":datetime.now(timezone.utc).isoformat(),
                "data_hash":digest, "source_commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
                "atomic_write":True, "incremental":incremental, "audit":audit}
    if old_meta.get("data_hash") and old_meta.get("data_hash") != digest:
        metadata["previous_data_hash"] = old_meta["data_hash"]
        metadata["previous_retrieved_at"] = old_meta.get("retrieved_at")
    metadata["dataset_version"] = digest[:16]
    atomic_write(meta_path, json.dumps(metadata, indent=2) + "\n")
    return metadata


def main():
    parser = argparse.ArgumentParser(description="READ-ONLY paged MT5 historical export")
    parser.add_argument("--start", required=True, help="UTC ISO timestamp")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp, exclusive")
    parser.add_argument("--output", default="artifacts/research/multitimeframe_liquidity_sniper/paged_native")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:22350/mcp")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    start, end = parse_ts(args.start), parse_ts(args.end)
    if start >= end or not 1 <= args.page_size <= 500: raise SystemExit("invalid range or page size")
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "export_manifest.json"
    prior_manifest_hash = None
    if args.incremental and manifest_path.exists():
        try:
            prior_manifest_hash = json.loads(manifest_path.read_text()).get("manifest_hash")
        except (OSError, ValueError):
            prior_manifest_hash = None
    manifest = {"schema":"native-mt5-paged-export-v1", "read_only":True, "broker_writes":0,
                "requested_start_timestamp":start, "requested_end_timestamp":end, "page_size":args.page_size,
                "symbols":{}, "retrieved_at":datetime.now(timezone.utc).isoformat()}
    if prior_manifest_hash:
        manifest["previous_manifest_hash"] = prior_manifest_hash
    for canonical in args.symbols:
        try:
            responses = {}
            quotes = {}
            for candidate in candidate_symbols(canonical):
                try:
                    responses[candidate] = call_bridge(args.mcp_url, "mt5_symbol_info", {"symbol": candidate})
                except Exception:
                    responses[candidate] = None
                try:
                    quotes[candidate] = call_bridge(args.mcp_url, "mt5_quote", {"symbol": candidate})
                except Exception:
                    quotes[candidate] = None
            broker_symbol, info = resolve_from_read_probes(canonical, responses, quotes)
            manifest["symbols"].setdefault(canonical, {"broker_symbol":broker_symbol, "symbol_info":info, "timeframes":{}})
            for tf in TIMEFRAMES:
                manifest["symbols"][canonical]["timeframes"][tf] = export_series(args.mcp_url, canonical, broker_symbol, tf, start, end, out, args.page_size, args.retries, True, args.incremental)
        except Exception as exc:
            manifest["symbols"][canonical] = {"broker_symbol":broker_symbol, "status":"FAILED", "error":str(exc)}
    manifest["source_commit"] = subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
    manifest["manifest_hash"] = hashlib.sha256(json.dumps(manifest, sort_keys=True, default=str).encode()).hexdigest()
    atomic_write(manifest_path, json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"output":str(out), "symbols":list(manifest["symbols"]), "failed":[s for s,v in manifest["symbols"].items() if v.get("status")=="FAILED"], "broker_writes":0}, indent=2))


if __name__ == "__main__": main()
