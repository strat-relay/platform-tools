"""Normalize a legitimate MT5 H1 export and run one KOJO discovery baseline.

The input is an offline export produced by ``historical_fetch_symbol.py``. The
script never calls the bridge, never reads a live quote, and never constructs a
validation feed. The final chronological 20 percent is reserved by metadata.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_backtest import (
    BacktestArtifactStore,
    BacktestEngine,
    CostModel,
    HistoricalMarketFeed,
    MarketEvent,
    StrategyEvaluatorRegistry,
    StrategyVersion,
    kojo_wedge_baseline_parameter_set,
    kojo_wedge_parameter_schema,
    write_kojo_wedge_diagnostic_artifact,
    register_builtin_evaluators,
)


UTC = timezone.utc


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def load_source(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def normalize(raw: dict, source_sha256: str) -> tuple[tuple[MarketEvent, ...], list[dict]]:
    if raw.get("symbol") != "XAUUSDm":
        raise ValueError(f"expected XAUUSDm source, got {raw.get('symbol')!r}")
    rows = raw.get("H1")
    if not isinstance(rows, list) or not rows:
        raise ValueError("source has no H1 records")
    quote_time = int((raw.get("quote") or {}).get("time", 0))
    ordered = sorted(rows, key=lambda row: int(row["time"]))
    events: list[MarketEvent] = []
    irregular: list[dict] = []
    previous = None
    for row in ordered:
        start = int(row["time"])
        if previous is not None and start - previous != 3600:
            irregular.append({"previous_open": previous, "current_open": start, "delta_seconds": start - previous, "previous_utc": iso(previous), "current_utc": iso(start)})
        previous = start
        high, low = float(row["high"]), float(row["low"])
        open_, close = float(row["open"]), float(row["close"])
        if high < max(open_, close) or low > min(open_, close) or low > high:
            raise ValueError(f"invalid OHLC at {start}")
        # copy_rates_from_pos includes the currently forming bar. It is not a
        # completed historical observation and is excluded when the snapshot
        # quote proves that the bar had not closed yet.
        if quote_time and start <= quote_time < start + 3600:
            continue
        events.append(MarketEvent(
            canonical_instrument="XAUUSD", timeframe="H1",
            open_timestamp=start, close_timestamp=start + 3600,
            open=open_, high=high, low=low, close=close, completed=True,
            source="MT5_TERMINAL_HISTORY",
            provenance={"provider": "MT5", "provider_symbol": "XAUUSDm", "timezone": "UTC", "source_sha256": source_sha256},
        ))
    if not events:
        raise ValueError("no completed H1 records remain")
    return tuple(events), irregular


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--artifact-root", type=Path, required=True)
    args = parser.parse_args()

    raw, source_sha256 = load_source(args.source)
    events, irregular = normalize(raw, source_sha256)
    split = int(len(events) * 0.8)
    discovery_events = events[:split]
    validation_events = events[split:]
    if len(discovery_events) < 24 * 30 * 12:
        raise ValueError("discovery partition is shorter than the required meaningful history")

    discovery = HistoricalMarketFeed(discovery_events, dataset_id=f"mt5-xauusdm-h1:{source_sha256}", partition="DISCOVERY")
    strategy = StrategyVersion("KOJO_WEDGE", "V1", "KOJO_WEDGE_V1", kojo_wedge_parameter_schema(), "VALIDATED")
    parameters = kojo_wedge_baseline_parameter_set()
    result = BacktestEngine(register_builtin_evaluators(StrategyEvaluatorRegistry())).run(
        strategy, parameters, discovery, CostModel("zero-cost-discovery", spread_price=0.0, commission_r=0.0), run_id="kojo-wedge-v1-discovery-baseline"
    )
    artifact_root = args.artifact_root
    result_path = BacktestArtifactStore(artifact_root).write(result)
    diagnostic_root = artifact_root / "diagnostics"
    for signal in result.signals:
        write_kojo_wedge_diagnostic_artifact(signal, diagnostic_root)
    dataset_path = artifact_root / "datasets" / "kojo_xauusd_h1_discovery.jsonl.gz"
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dataset_path, "wt", encoding="utf-8") as handle:
        for event in discovery_events:
            handle.write(json.dumps(event.identity_payload(), sort_keys=True) + "\n")

    manifest = {
        "dataset_id": discovery.dataset_id,
        "dataset_fingerprint": discovery.dataset_fingerprint,
        "source": "local MT5 terminal historical export",
        "provider": "MT5",
        "provider_symbol": "XAUUSDm",
        "canonical_instrument": "XAUUSD",
        "timezone": "UTC",
        "completed_only": True,
        "source_sha256": source_sha256,
        "total_completed_h1_bars": len(events),
        "irregular_intervals": irregular,
        "discovery_range": {"start": iso(discovery_events[0].open_timestamp), "end": iso(discovery_events[-1].close_timestamp), "bars": len(discovery_events)},
        "validation_range": {"start": iso(validation_events[0].open_timestamp), "end": iso(validation_events[-1].close_timestamp), "bars": len(validation_events)},
        "validation_accessed": False,
        "validation_bars_persisted": False,
        "baseline_parameter_set_id": parameters.parameter_set_id,
        "baseline_parameter_fingerprint": parameters.fingerprint,
        "result_artifact": str(result_path),
        "diagnostic_artifact": str(diagnostic_root / "kojo_wedge_v1"),
        "dataset_artifact": str(dataset_path),
    }
    manifest_path = artifact_root / "kojo_xauusd_h1_discovery_manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(manifest_path), "result": str(result_path), "dataset": str(dataset_path), "signals": len(result.signals), "trades": len(result.outcomes), "metrics": result.metrics, "diagnostics": result.diagnostics}, sort_keys=True))


if __name__ == "__main__":
    main()
