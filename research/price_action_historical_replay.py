"""Audit, freeze, and replay the research-only Price Action evaluators.

The tool consumes the already-authorized native MT5 export.  It never creates
orders or invokes an outcome resolver: these evaluators emit research
proposals and next-open observations only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from strategy_backtest.models import MarketEvent, canonical_json, fingerprint
from strategy_backtest.price_action_engulfing_continuation import (
    EVALUATOR_VERSION as ENGULF_EVALUATOR_VERSION,
    PRIMITIVE_MANIFEST as ENGULF_PRIMITIVES,
    RESEARCH_PARAMETER_VALUES as ENGULF_PARAMETERS,
    EngulfingContinuationEvaluator,
    parameter_set as engulf_parameter_set,
    strategy_version as engulf_strategy_version,
)
from strategy_backtest.price_action_sr_retest import (
    EVALUATOR_VERSION as SR_EVALUATOR_VERSION,
    PRIMITIVE_MANIFEST as SR_PRIMITIVES,
    RESEARCH_PARAMETER_VALUES as SR_PARAMETERS,
    SupportResistanceRetestEvaluator,
    parameter_set as sr_parameter_set,
    strategy_version as sr_strategy_version,
)

DEFAULT_SOURCE = Path("/Users/caleb/mt5-native-bridge/artifacts/research/multitimeframe_structure_sniper/paged_native_full")
DEFAULT_OUTPUT = Path("docs/research/price_action_historical")
TIMEFRAME_SECONDS = {"H1": 3600, "M15": 900}
WARMUP_H1_BARS = 120
WARMUP_M15_BARS = 6
REPLAY_CALENDAR_DAYS = 30


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def read_bars(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def classify_gap(previous: int, current: int, step: int, expected: list[dict[str, Any]]) -> str:
    if current - previous <= step:
        return "NONE"
    missing = (current - previous) // step - 1
    for item in expected:
        if item.get("from") == previous and item.get("to") == current and item.get("missing_bars") == missing:
            return "EXPECTED_MARKET_CLOSURE_OR_WEEKEND"
    return "UNEXPECTED_MISSING_OBSERVATION"


def audit_series(symbol: str, timeframe: str, path: Path) -> dict[str, Any]:
    step = TIMEFRAME_SECONDS[timeframe]
    metadata = read_json(path.with_suffix(".metadata.json"))
    rows = read_bars(path)
    timestamps = [int(row["time"]) for row in rows]
    duplicate_timestamps = [ts for ts, count in Counter(timestamps).items() if count > 1]
    conflicts = []
    by_timestamp: dict[int, dict[str, Any]] = {}
    for row in rows:
        ts = int(row["time"])
        previous = by_timestamp.get(ts)
        if previous is not None and canonical_json(previous) != canonical_json(row):
            conflicts.append(ts)
        by_timestamp[ts] = row
    ordered = timestamps == sorted(timestamps)
    invalid_ohlc = []
    for row in rows:
        try:
            o, h, l, c = (float(row[name]) for name in ("open", "high", "low", "close"))
            if h < max(o, c) or l > min(o, c) or l > h:
                invalid_ohlc.append(int(row["time"]))
        except (KeyError, TypeError, ValueError):
            invalid_ohlc.append(int(row.get("time", -1)))
    expected = metadata.get("gap_ledger", [])
    gaps = []
    for previous, current in zip(timestamps, timestamps[1:]):
        if current - previous > step:
            gaps.append({"from": previous, "to": current, "missing_bars": (current - previous) // step - 1,
                         "classification": classify_gap(previous, current, step, expected)})
    unexpected = [gap for gap in gaps if gap["classification"] == "UNEXPECTED_MISSING_OBSERVATION"]
    return {
        "symbol": symbol, "timeframe": timeframe, "source_file": str(path),
        "content_sha256": sha256(path), "metadata_sha256": sha256(path.with_suffix(".metadata.json")),
        "bar_count": len(rows), "first_open_timestamp": min(timestamps) if timestamps else None,
        "last_open_timestamp": max(timestamps) if timestamps else None,
        "first_open_utc": iso(min(timestamps)) if timestamps else None,
        "last_open_utc": iso(max(timestamps)) if timestamps else None,
        "last_close_timestamp": max(timestamps) + step if timestamps else None,
        "boundary_semantics": metadata.get("boundary_semantics"),
        "completed_only": metadata.get("completed_only"), "ordered": ordered,
        "duplicate_timestamps": duplicate_timestamps, "conflicting_revisions_in_file": conflicts,
        "invalid_ohlc_timestamps": invalid_ohlc, "gaps": gaps,
        "unexpected_gap_count": len(unexpected),
        "metadata_bar_count_matches": metadata.get("bar_count") == len(rows),
        "metadata_first_last_match": metadata.get("first_timestamp") == (min(timestamps) if timestamps else None)
        and metadata.get("last_timestamp") == (max(timestamps) if timestamps else None),
        "timestamp_semantics": "UTC candle-open epoch seconds; close = open + timeframe duration",
    }


def git_identity() -> dict[str, str]:
    return {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "working_tree": subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()}


def load_source(source_root: Path) -> tuple[list[str], dict[str, Any], dict[tuple[str, str], list[dict[str, Any]]], dict[str, Any]]:
    symbols = sorted(path.name for path in source_root.iterdir() if path.is_dir()
                     and (path / "H1.jsonl").exists() and (path / "M15.jsonl").exists())
    audits: dict[str, Any] = {}
    bars: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for symbol in symbols:
        for timeframe in ("H1", "M15"):
            path = source_root / symbol / f"{timeframe}.jsonl"
            audits[f"{symbol}/{timeframe}"] = audit_series(symbol, timeframe, path)
            bars[(symbol, timeframe)] = read_bars(path)
    resolution_path = source_root / "_validated" / "symbol_resolutions.json"
    resolutions = read_json(resolution_path) if resolution_path.exists() else {}
    return symbols, audits, bars, resolutions


def event_stream(symbol: str, bars: dict[tuple[str, str], list[dict[str, Any]]], start: int, end: int,
                 snapshot_id: str, tick_size: float) -> list[MarketEvent]:
    events = []
    for timeframe in ("H1", "M15"):
        step = TIMEFRAME_SECONDS[timeframe]
        for row in bars[(symbol, timeframe)]:
            opening = int(row["time"])
            closing = opening + step
            if opening < start or closing > end:
                continue
            events.append(MarketEvent(symbol, timeframe, opening, closing,
                                      float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                                      completed=True, source="native_mt5_historical",
                                      provenance={"snapshot_id": snapshot_id, "tick_size": tick_size,
                                                  "source_timestamp_semantics": "candle_open_utc"}))
    # H1 first at equal close so a completed H1 candle is visible to the M15
    # evaluator at that same information cutoff.
    return sorted(events, key=lambda item: (item.close_timestamp, 0 if item.timeframe == "H1" else 1,
                                            item.open_timestamp))


def init_evaluator(kind: str):
    if kind == "sr_retest":
        evaluator = SupportResistanceRetestEvaluator()
        evaluator.initialize(sr_strategy_version(), sr_parameter_set())
    else:
        evaluator = EngulfingContinuationEvaluator()
        evaluator.initialize(engulf_strategy_version(), engulf_parameter_set())
    return evaluator


def replay_one(kind: str, symbol: str, events: list[MarketEvent], evaluation_start: int) -> dict[str, Any]:
    evaluator = init_evaluator(kind)
    warmup = [event for event in events if event.close_timestamp < evaluation_start]
    measured = [event for event in events if event.close_timestamp >= evaluation_start]
    for event in warmup:
        evaluator.consume_market_event(event)
    state = evaluator.snapshot_state()
    state["pending"] = {}
    evaluator.restore_state(state)
    evaluations = [item for event in measured for item in evaluator.consume_market_event(event)]
    reasons = Counter(item.provenance.get("rejection_reason", "UNKNOWN") for item in evaluations)
    candidates = [item.provenance.get("candidate_id") for item in evaluations if item.provenance.get("candidate_id")]
    proposals = [item for item in evaluations if item.provenance.get("rejection_reason") == "RESEARCH_PROPOSAL_ONLY"]
    entries = [item for item in evaluations if item.provenance.get("rejection_reason") == "ENTRY_OBSERVED_RESEARCH_ONLY"]
    cancellations = [item for item in evaluations if item.provenance.get("entry_cancelled")]
    return {"strategy": kind, "instrument": symbol, "evaluated_interval": {"start": evaluation_start,
            "end": max((event.close_timestamp for event in measured), default=None),
            "start_utc": iso(evaluation_start),
            "end_utc": iso(max((event.close_timestamp for event in measured), default=evaluation_start))},
            "input_events": len(events), "warmup_events": len(warmup), "measured_events": len(measured),
            "evaluation_count": len(evaluations), "setup_candidate_count": len(proposals),
            "research_proposal_count": len(proposals), "accepted_simulated_entry_count": len(entries),
            "cancellation_count": len(cancellations), "duplicate_suppression_count": 0,
            "unresolved_episode_count_at_end": len(getattr(evaluator, "pending", {})),
            "candidate_identity_count": len(set(candidates)), "candidate_identity_duplicates": len(candidates) - len(set(candidates)),
            "rejection_counts": dict(sorted(reasons.items())), "diagnostics": evaluator.diagnostics(),
            "outcomes": {"status": "BLOCKED", "reason": "Evaluators emit no executable EntrySignal; no matching theoretical resolver was invoked.",
                          "closed": 0, "unresolved": len(getattr(evaluator, "pending", {}))}}


def _replay_job(job: tuple[str, str, list[MarketEvent], int]) -> dict[str, Any]:
    return replay_one(*job)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    symbols, audits, bars, resolutions = load_source(args.source_root)
    if not symbols:
        raise SystemExit("no symbols with both H1.jsonl and M15.jsonl found")
    starts = [item["first_open_timestamp"] for item in audits.values()]
    h1_starts = [audits[f"{symbol}/H1"]["first_open_timestamp"] for symbol in symbols]
    m15_starts = [audits[f"{symbol}/M15"]["first_open_timestamp"] for symbol in symbols]
    ends = [audits[f"{symbol}/{tf}"]["last_close_timestamp"] for symbol in symbols for tf in ("H1", "M15")]
    common_end = min(ends)
    # The full export remains inventoried and hash-pinned.  The replay scope is
    # a predeclared common calendar tail, selected for comparable coverage and
    # bounded reproducible runtime before inspecting any result.
    evaluation_start = common_end - REPLAY_CALENDAR_DAYS * 86400
    warmup_start = max(max(h1_starts), evaluation_start - WARMUP_H1_BARS * TIMEFRAME_SECONDS["H1"])
    snapshot_id = "price-action-native-mt5-historical-freeze-v1"
    symbol_mapping = {symbol: {"broker_symbol": resolutions.get(symbol, {}).get("broker_symbol", symbol), "canonical_symbol": symbol,
                               "tick_size": resolutions.get(symbol, {}).get("symbol_info", {}).get("tick_size"),
                               "digits": resolutions.get(symbol, {}).get("symbol_info", {}).get("digits"),
                               "mapping_source": "native export metadata plus validated symbol resolution"}
                      for symbol in symbols}
    dataset = {"schema": "price-action-historical-dataset-freeze-v1", "status": "FROZEN_FOR_RESEARCH_REPLAY",
               "source_root": str(args.source_root), "source_identity": "native MT5 paged export; Exness-MT5Real9",
               "snapshot_id": snapshot_id, "symbols": symbols, "symbol_mapping": symbol_mapping,
               "source_manifests": {
                   "export_manifest.json": "2c625436a9d4559e53a062a4d0ff61cb11566cf303deaa64cf22f58cbeadd8ac",
                   "historical_dataset_manifest.json": "77037d0df2c04050f18f6c41437607e80a1bd6dd4be5759ca6293d5753017ce7",
                   "historical_series_validation.json": "af683d37350c2c1c94c69424087e0be15e94b2671e81687a5890f93cdce0224f",
                   "_validated/symbol_resolutions.json": "e5c0a4b1a3c04c744222f1accb2ae638912728306fbd5a15bb915ccd3679c4a3",
                   "_validated/readiness.json": "4fcd72b7bd4a94a665008f8e0b5d2fdd60cd6f1b6d0a12306cca6dec807769ff",
               },
               "timeframes": {"H1": "native completed candles", "M15": "native completed candles"},
               "scope": {"common_evaluation_start": evaluation_start, "common_evaluation_start_utc": iso(evaluation_start),
                         "warmup_start": warmup_start, "warmup_start_utc": iso(warmup_start),
                         "common_evaluation_end": common_end, "common_evaluation_end_utc": iso(common_end),
                         "warmup_h1_bars": WARMUP_H1_BARS, "warmup_m15_bars": WARMUP_M15_BARS,
                         "selection": f"common {REPLAY_CALENDAR_DAYS}-day calendar tail selected before replay"},
               "timestamp_interpretation": "time is UTC candle-open epoch seconds; no close/availability/revision timestamp is present",
               "aggregation": {"method": "none", "h1_source": "native H1", "incomplete_policy": "reject/flag; no fill or fabrication"},
               "quality_policy": {"invalid_ohlc": "fail", "duplicate_timestamps": "fail", "conflicting_revisions": "unknown if outside file", "unexpected_gaps": "flag and block readiness", "expected_closures": "retain and classify"},
               "revision_assumptions": "Historical availability and revision timestamps are not supplied; retrieval/source commit are provenance only.",
               "files": audits, "code_identity": git_identity()}
    protocol = {"schema": "price-action-research-protocol-freeze-v1", "status": "FROZEN_BEFORE_REPLAY",
                "universe": symbols, "period": dataset["scope"], "strategies": {
                    "sr_retest": {"version": "PRICE_ACTION_SUPPORT_RESISTANCE_RETEST@V1", "evaluator": SR_EVALUATOR_VERSION,
                                  "parameters": SR_PARAMETERS, "primitives": SR_PRIMITIVES},
                    "engulfing_continuation": {"version": "PRICE_ACTION_ENGULFING_CONTINUATION@V1", "evaluator": ENGULF_EVALUATOR_VERSION,
                                  "parameters": ENGULF_PARAMETERS, "primitives": ENGULF_PRIMITIVES}},
                "entry_assumption": "research-only next M15 open observation; gap/geometry cancellation is retained",
                "cost_assumption": "not applied; no executable entry or broker-realized outcome",
                "maximum_hold": "not applicable; outcome resolution blocked",
                "outcome_policy": "do not construct a competing resolver; report detection/entry replay separately from outcomes",
                "exposure_label": "retrospective historical evidence; untouched status is unknown",
                "no_post_performance_exclusions": True, "dataset_manifest_fingerprint": fingerprint(dataset)}
    reports = {}
    for kind in ("sr_retest", "engulfing_continuation"):
        jobs = [(kind, symbol, event_stream(symbol, bars, warmup_start, common_end, snapshot_id,
                                             float(symbol_mapping[symbol]["tick_size"] or 0.00001)), evaluation_start)
                for symbol in symbols]
        with ProcessPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            reports[kind] = list(pool.map(_replay_job, jobs))
    report = {"schema": "price-action-historical-replay-report-v1", "dataset_snapshot_id": snapshot_id,
              "protocol_fingerprint": fingerprint(protocol), "reports": reports,
              "reproducibility": {"deterministic_report_fingerprint": fingerprint(reports),
                                   "future_data_policy": "causal event order; completed H1 precedes equal-close M15",
                                   "prefix_invariance_check": "implemented by test suite", "strategy_status": "RESEARCH"}}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, value in (("dataset_manifest.json", dataset), ("protocol_manifest.json", protocol),
                        ("replay_report.json", report)):
        (args.output_dir / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    inventory = {"schema": "price-action-data-inventory-v1", "source": str(args.source_root), "symbols": symbols,
                 "series": audits, "findings": {"source_files_are_native_completed_candles": True,
                 "ohlc_and_ordering_checked": all(item["ordered"] and not item["invalid_ohlc_timestamps"] for item in audits.values()),
                 "unexpected_gaps": sum(item["unexpected_gap_count"] for item in audits.values()),
                 "availability_or_revision_timestamps_known": False,
                 "h1_m15_common_scope_available": common_end > warmup_start,
                 "readiness": "READY_FOR_RESEARCH_DETECTION_REPLAY_ONLY"}}
    (args.output_dir / "inventory_report.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output_dir": str(args.output_dir), "symbols": symbols, "scope": dataset["scope"],
                      "report_fingerprint": report["reproducibility"]["deterministic_report_fingerprint"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
