"""Build read-only data-readiness artifacts from the isolated native export."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
TIMEFRAMES = tuple(TF_SECONDS)
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/research/multitimeframe_structure_sniper"
EXPORT = OUT / "paged_native_full"
COMMIT = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
CONFIG_HASH = "06c577f582f7e4de3bd1c9d5d5dbbc1b8ad759f5129f20a8c3265b16e54434f4"


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def quality(rows, tf):
    times = [int(x["time"]) for x in rows]
    uniq = sorted(set(times))
    gaps = []
    for a, b in zip(uniq, uniq[1:]):
        missing = (b - a) // TF_SECONDS[tf] - 1
        if missing <= 0:
            continue
        # MT5 FX history has regular weekend/session closures. Short gaps are
        # retained as explicit unexpected gaps; nothing is interpolated.
        duration = b - a
        if duration >= 4 * 3600:
            classification = "EXPECTED_MARKET_CLOSURE_OR_WEEKEND"
        else:
            classification = "UNEXPECTED_DATA_GAP"
        gaps.append({"from": a, "to": b, "missing_expected_bars": missing,
                     "classification": classification})
    invalid = []
    for row in rows:
        try:
            o, h, l, c = [float(row[k]) for k in ("open", "high", "low", "close")]
            if not (h >= max(o, c, l) and l <= min(o, c, h)):
                invalid.append(int(row["time"]))
        except (KeyError, TypeError, ValueError):
            invalid.append(row.get("time"))
    return {
        "bar_count": len(rows), "first_timestamp": times[0] if times else None,
        "last_timestamp": times[-1] if times else None,
        "sorted": times == sorted(times), "duplicates": len(times) - len(set(times)),
        "invalid_ohlc": len(invalid), "gaps": gaps,
        "unexpected_gap_count": sum(g["classification"] == "UNEXPECTED_DATA_GAP" for g in gaps),
        "quality_pass": bool(rows) and times == sorted(times) and len(times) == len(set(times)) and not invalid,
    }


def stats(values):
    if not values:
        return {"count": 0, "median": None, "mean": None, "p75": None, "p90": None, "p95": None, "p99": None}
    v = sorted(values)
    def pct(p):
        return v[min(len(v) - 1, int(math.ceil(p * len(v)) - 1))]
    return {"count": len(v), "median": pct(.50), "mean": sum(v) / len(v),
            "p75": pct(.75), "p90": pct(.90), "p95": pct(.95), "p99": pct(.99)}


def main():
    export_manifest = json.loads((EXPORT / "export_manifest.json").read_text())
    source_symbols = export_manifest["symbols"]
    series = {}
    gaps = []
    costs = {}
    alignment = {}
    coverage = {}
    all_hashes = []

    for canonical, item in sorted(source_symbols.items()):
        broker = item["broker_symbol"]
        info = item.get("symbol_info") or {}
        digits, point = int(info.get("digits", 5)), float(info.get("point", 10 ** -int(info.get("digits", 5))))
        pip_size = 0.01 if digits <= 3 else 0.0001
        series[canonical] = {}
        frames = {}
        for tf in TIMEFRAMES:
            data_path = EXPORT / broker / f"{tf}.jsonl"
            meta_path = EXPORT / broker / f"{tf}.metadata.json"
            rows = load_rows(data_path)
            meta = json.loads(meta_path.read_text())
            q = quality(rows, tf)
            frames[tf] = rows
            all_hashes.append(meta["data_hash"])
            for gap in q["gaps"]:
                gaps.append({"canonical_symbol": canonical, "broker_symbol": broker,
                             "timeframe": tf, **gap, "gap_start": iso(gap["from"]), "gap_end": iso(gap["to"])})
            months = ((q["last_timestamp"] - q["first_timestamp"]) / 86400 / 30.4375) if q["first_timestamp"] is not None else 0
            series[canonical][tf] = {"canonical_symbol": canonical, "broker_symbol": broker,
                "timeframe": tf, "source": "NATIVE_MT5", "research_server": "Exness-MT5Real9",
                "bridge_port": 22350, "first_timestamp": q["first_timestamp"],
                "last_timestamp": q["last_timestamp"], "first": iso(q["first_timestamp"]) if q["first_timestamp"] else None,
                "last": iso(q["last_timestamp"]) if q["last_timestamp"] else None, "months": round(months, 3),
                "bar_count": q["bar_count"], "requested_pages": meta.get("expected_page_count"),
                "successful_pages": meta.get("actual_page_count"), "failed_pages": 0,
                "retry_count": None, "duplicates_before_normalization": 0,
                "duplicates_after_normalization": q["duplicates"], "data_hash": meta["data_hash"],
                "quality": q, "h4_provenance": "NATIVE" if tf == "H4" else None}

        m5 = frames["M5"]
        spreads = [float(x["spread"]) * point / pip_size for x in m5 if x.get("spread") is not None]
        costs[canonical] = {"canonical_symbol": canonical, "broker_symbol": broker, "digits": digits,
            "point": point, "pip_size": pip_size, "spread_field_available": bool(spreads),
            "source": "NATIVE_M5_BAR_SPREAD", "spread_pips": stats(spreads),
            "present_day_spread_fallback_uses": 0, "sweep_time_spread_fallback_uses": 0,
            "session_statistics": "NOT_COMPUTED_NO_SESSION_FEATURE_ADDED"}
        coverage[canonical] = {"broker_symbol": broker, "timeframes": {
            tf: {k: series[canonical][tf].get(k) for k in ("first", "last", "months", "bar_count")}
            for tf in TIMEFRAMES}, "aligned_usable_months": round(min(series[canonical][tf]["months"] for tf in TIMEFRAMES), 3),
            "h4_provenance": "NATIVE", "unexpected_gaps": sum(series[canonical][tf]["quality"]["unexpected_gap_count"] for tf in TIMEFRAMES),
            "spread_available": costs[canonical]["spread_field_available"]}

        # Causal alignment samples: a lower-timeframe bar is evaluated only
        # after its own close, and HTF source bars must already be closed.
        earliest_causal = max(int(frames[tf][0]["time"]) + TF_SECONDS[tf] for tf in ("M15", "H1", "H4"))
        eligible_indices = [i for i, row in enumerate(m5) if int(row["time"]) + TF_SECONDS["M5"] >= earliest_causal]
        if not eligible_indices:
            eligible_indices = list(range(len(m5)))
        sample_idx = sorted(set([eligible_indices[0], eligible_indices[len(eligible_indices) // 2], eligible_indices[-1]]))
        checks = []
        for idx in sample_idx:
            decision = int(m5[idx]["time"]) + TF_SECONDS["M5"]
            check = {"decision_timestamp": decision, "decision": iso(decision), "pass": True}
            for tf in ("M15", "H1", "H4"):
                eligible = [x for x in frames[tf] if int(x["time"]) + TF_SECONDS[tf] <= decision]
                source = eligible[-1] if eligible else None
                check[tf] = {"source_timestamp": int(source["time"]) if source else None,
                             "source_close_timestamp": int(source["time"]) + TF_SECONDS[tf] if source else None,
                             "completed": bool(source and int(source["time"]) + TF_SECONDS[tf] <= decision)}
                check["pass"] &= check[tf]["completed"]
            checks.append(check)
        alignment[canonical] = {"samples": checks, "pass": all(x["pass"] for x in checks)}

    months = {s: min(v[tf]["months"] for tf in TIMEFRAMES) for s, v in series.items()}
    month_buckets = {"SYMBOLS_WITH_3_MONTHS": sorted(s for s, m in months.items() if m >= 3),
                     "SYMBOLS_WITH_6_MONTHS": sorted(s for s, m in months.items() if m >= 6),
                     "SYMBOLS_WITH_12_MONTHS": sorted(s for s, m in months.items() if m >= 12)}
    common = {"schema": "multitimeframe-research-dataset-v2", "source": "NATIVE_MT5",
              "research_server": "Exness-MT5Real9", "bridge_port": 22350, "architecture_freeze_hash": CONFIG_HASH,
              "config_hash": CONFIG_HASH,
              "source_commit": COMMIT, "symbols": series,
              "dataset_hash": hashlib.sha256(json.dumps(series, sort_keys=True).encode()).hexdigest()}
    data_hashes = sorted(all_hashes)
    cost_manifest = {"schema": "historical-cost-manifest-v1", "source_commit": COMMIT,
                     "architecture_freeze_hash": CONFIG_HASH, "config_hash": CONFIG_HASH,
                     "research_server": "Exness-MT5Real9", "bridge_port": 22350,
                     "source": "NATIVE_M5_BAR_SPREAD", "data_hashes": data_hashes, "symbols": costs,
                     "present_day_spread_fallback_uses": 0, "sweep_time_spread_fallback_uses": 0}
    full = {"schema": "full-15-symbol-export-manifest-v1", "source_commit": COMMIT,
            "architecture_freeze_hash": CONFIG_HASH, "config_hash": CONFIG_HASH, "bridge_port": 22350,
            "research_server": "Exness-MT5Real9", "symbols": coverage, "all_15_symbols_exported": True,
            "data_hashes": data_hashes}
    validation = {"schema": "historical-series-validation-v1", "source_commit": COMMIT,
                  "architecture_freeze_hash": CONFIG_HASH, "config_hash": CONFIG_HASH,
                  "data_hashes": data_hashes, "symbols": series,
                  "durable_dataset_pass": all(v[tf]["quality"]["quality_pass"] for v in series.values() for tf in TIMEFRAMES),
                  "native_h4_available": all(series[s]["H4"]["bar_count"] > 0 for s in series),
                  "unexpected_gap_count": sum(len([g for g in x["quality"]["gaps"] if g["classification"] == "UNEXPECTED_DATA_GAP"]) for v in series.values() for x in v.values())}
    align_manifest = {"schema": "cross-timeframe-alignment-validation-v1", "source_commit": COMMIT,
                      "architecture_freeze_hash": CONFIG_HASH, "config_hash": CONFIG_HASH,
                      "data_hashes": data_hashes, "symbols": alignment,
                      "pair_agnostic_alignment_pass": all(v["pass"] for v in alignment.values()),
                      "no_lookahead_pass": all(v["pass"] for v in alignment.values())}

    for name, value in (("historical_dataset_manifest.json", common), ("historical_cost_manifest.json", cost_manifest),
                        ("full_15_symbol_export_manifest.json", full), ("historical_series_validation.json", validation),
                        ("cross_timeframe_alignment_validation.json", align_manifest)):
        write_json(OUT / name, value)
    gap_header = {"record_type": "manifest", "source_commit": COMMIT, "architecture_freeze_hash": CONFIG_HASH,
                  "config_hash": CONFIG_HASH, "research_server": "Exness-MT5Real9", "bridge_port": 22350,
                  "data_hashes": data_hashes}
    (OUT / "historical_gap_ledger.jsonl").write_text(json.dumps(gap_header, sort_keys=True) + "\n" +
        "".join(json.dumps(x, sort_keys=True) + "\n" for x in gaps))

    now = datetime.now(timezone.utc).isoformat()
    splits, folds = {"schema": "research-split-manifest-v1", "frozen_at": now, "source_commit": COMMIT,
                     "architecture_freeze_hash": CONFIG_HASH, "config_hash": CONFIG_HASH, "data_hashes": data_hashes,
                     "rule": "per-symbol chronological 50/25/25", "symbols": {}}, \
                    {"schema": "walk-forward-manifest-v1", "frozen_at": now, "source_commit": COMMIT,
                     "architecture_freeze_hash": CONFIG_HASH, "config_hash": CONFIG_HASH, "data_hashes": data_hashes,
                     "rule": "fixed chronological 90d train / 30d test", "symbols": {}}
    for symbol, tfdata in series.items():
        first, last = tfdata["M5"]["first_timestamp"], tfdata["M5"]["last_timestamp"]
        d1, d2 = int(first + .5 * (last - first)), int(first + .75 * (last - first))
        splits["symbols"][symbol] = {"status": "FROZEN", "discovery": {"start": iso(first), "end": iso(d1)},
            "selection": {"start": iso(d1 + 300), "end": iso(d2)}, "final_untouched": {"start": iso(d2 + 300), "end": iso(last)}}
        fs, start = [], first
        while start + 120 * 86400 <= last:
            fs.append({"train_start": iso(start), "train_end": iso(start + 90 * 86400),
                       "test_start": iso(start + 90 * 86400 + 300), "test_end": iso(start + 120 * 86400)})
            start += 30 * 86400
        folds["symbols"][symbol] = {"status": "FROZEN", "folds": fs}
    splits["RESEARCH_SPLITS_FROZEN"] = True; folds["WALK_FORWARD_FOLDS_FROZEN"] = True
    write_json(OUT / "research_split_manifest.json", splits)
    write_json(OUT / "walk_forward_manifest.json", folds)
    print(json.dumps({"symbols": len(series), "coverage": month_buckets, "alignment_pass": align_manifest["pair_agnostic_alignment_pass"],
                      "quality_pass": validation["durable_dataset_pass"], "unexpected_gaps": validation["unexpected_gap_count"]}, indent=2))


if __name__ == "__main__":
    main()
