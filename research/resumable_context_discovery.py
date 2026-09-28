"""Research-only resumable Context discovery runner.

The immutable EURUSD/GBPUSD dataset is loaded from its canonical path and is
referenced by fingerprint in checkpoints.  Checkpoints contain evaluator and
execution state, but never duplicate the immutable dataset.  This module does
not alter strategy semantics or production runtime code.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.context_replay_optimized import OptimizedContextRawOhlcEvaluator
from research.intraday_variants import variants
from research.run_context_intraday_exploratory import DATA_ROOT, SPLIT, _fast_replay_factory, _load_discovery
from strategy_backtest.execution import SimulatedExecution
from strategy_backtest.metrics import calculate_metrics
from strategy_backtest.models import CostModel, EntrySignal, EntrySignalOutcome, MarketEvent, SetupLifecycleEvent, fingerprint
import strategy_backtest.raw_ohlc_adapters as raw_adapters


OUT = ROOT / "artifacts/research/intraday-variants/context-exploratory"
RUNNER_VERSION = "context-indexed-resumable-discovery-v2"
DEFAULT_CADENCE_BARS = 900
DEFAULT_MAX_RSS_MIB = 1024


def _rss_mib() -> float:
    """Return current RSS when the host exposes ps; fallback to high-water RSS."""
    try:
        value = subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip()
        if value:
            return int(value) / 1024.0
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0 / 1024.0


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _event_from_payload(payload: dict[str, Any]) -> MarketEvent:
    return MarketEvent(**payload)


def _signal_from_payload(payload: dict[str, Any]) -> EntrySignal:
    return EntrySignal(**payload)


def _outcome_from_payload(payload: dict[str, Any]) -> EntrySignalOutcome:
    return EntrySignalOutcome(**payload)


def _execution_snapshot(execution: SimulatedExecution) -> dict[str, Any]:
    return {
        "outcomes": [asdict(item) for item in execution.outcomes],
        "open": {
            signal_id: {
                "signal": asdict(trade.signal),
                "entry_timestamp": trade.entry_timestamp,
                "entry_price": trade.entry_price,
            }
            for signal_id, trade in execution._open.items()
        },
        "pending": [asdict(signal) for signal in execution._pending],
    }


def _restore_execution(execution: SimulatedExecution, payload: dict[str, Any]) -> None:
    from strategy_backtest.execution import _OpenTrade

    execution.outcomes = [_outcome_from_payload(item) for item in payload.get("outcomes", [])]
    execution._open = {
        signal_id: _OpenTrade(_signal_from_payload(item["signal"]), int(item["entry_timestamp"]), float(item["entry_price"]))
        for signal_id, item in payload.get("open", {}).items()
    }
    execution._pending = [_signal_from_payload(item) for item in payload.get("pending", [])]


def _identity(instrument: str, events: tuple[MarketEvent, ...], strategy: Any, parameter_set: Any) -> dict[str, Any]:
    dataset_path = DATA_ROOT / instrument / "M5.jsonl"
    dataset_fingerprint = fingerprint([event.identity_payload() for event in events])
    return {
        "runner_version": RUNNER_VERSION,
        "instrument": instrument,
        "dataset_path": str(dataset_path),
        "dataset_fingerprint": dataset_fingerprint,
        "dataset_bar_count": len(events),
        "discovery_split": "2026-01-01T00:00:00Z",
        "strategy_version": strategy.strategy_version_id,
        "parameter_set_fingerprint": parameter_set.fingerprint,
        "evaluator_version": OptimizedContextRawOhlcEvaluator.VERSION,
        "partition": "DISCOVERY",
        "cost_model": CostModel("research-zero-engine-cost").fingerprint,
    }


def _compact_evaluator_checkpoint(evaluator: OptimizedContextRawOhlcEvaluator) -> dict[str, Any]:
    snapshot = evaluator.snapshot_state()
    # The immutable M5 dataset is referenced by checkpoint identity and rebuilt
    # from the canonical source on resume.  Keep only adapter state here.
    return {
        "seen_events": snapshot.get("seen_events", []),
        "adapter": snapshot.get("adapter", {}),
    }


def _restore_evaluator(evaluator: OptimizedContextRawOhlcEvaluator, events: tuple[MarketEvent, ...], last_index: int, payload: dict[str, Any]) -> None:
    evaluator.state.events = list(events[:last_index])
    evaluator._seen_events = {int(value) for value in payload.get("seen_events", [])}
    evaluator._restore_adapter_state(payload.get("adapter", {}))


def _checkpoint_payload(identity: dict[str, Any], last_index: int, events: tuple[MarketEvent, ...], evaluator: OptimizedContextRawOhlcEvaluator, execution: SimulatedExecution, setups: list[SetupLifecycleEvent], signals: list[EntrySignal], progress: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "research.context_resumable_checkpoint.v1",
        "identity": identity,
        "last_consumed_index": last_index,
        "last_consumed_timestamp": events[last_index - 1].close_timestamp if last_index else None,
        "evaluator": _compact_evaluator_checkpoint(evaluator),
        "execution": _execution_snapshot(execution),
        "setups": [asdict(item) for item in setups],
        "signals": [asdict(item) for item in signals],
        "progress": progress,
    }


def _restore_payload(path: Path, identity: dict[str, Any]) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("identity") != identity:
        raise RuntimeError("checkpoint identity mismatch; refusing resume")
    return payload


def _write_result(instrument: str, identity: dict[str, Any], setups: list[SetupLifecycleEvent], signals: list[EntrySignal], outcomes: list[EntrySignalOutcome], runtime_seconds: float, peak_rss_mib: float) -> dict[str, Any]:
    metrics = calculate_metrics(signals, outcomes)
    result_fingerprint = fingerprint({"signals": [asdict(item) for item in signals], "outcomes": [asdict(item) for item in outcomes], "metrics": metrics})
    result = {
        "schema": "research.context_resumable_discovery_result.v1",
        "identity": identity,
        "status": "COMPLETED",
        "runtime_seconds": runtime_seconds,
        "peak_rss_mib": peak_rss_mib,
        "setups": [asdict(item) for item in setups],
        "signals": [asdict(item) for item in signals],
        "outcomes": [asdict(item) for item in outcomes],
        "metrics": metrics,
        "result_fingerprint": result_fingerprint,
        "validation_outcomes_accessed": False,
        "parent_parity_status": "UNPROVEN",
        "evidence_class": "EXPLORATORY_RAW_PIPELINE_PARENT_PARITY_UNPROVEN",
        "production_changed": False,
        "broker_writes": 0,
    }
    _atomic_json(OUT / f"{instrument.lower()}_discovery_results.json", result)
    _atomic_json(OUT / f"{instrument.lower()}_discovery_funnel.json", {"identity": identity, "metrics": metrics, "setups": len(setups), "signals": len(signals), "outcomes": len(outcomes), "result_fingerprint": result_fingerprint})
    return result


def run(instrument: str, *, checkpoint_path: Path | None = None, cadence_bars: int = DEFAULT_CADENCE_BARS, max_rss_mib: int = DEFAULT_MAX_RSS_MIB, stop_after: int | None = None, profile_path: Path | None = None) -> dict[str, Any]:
    events = tuple(_load_discovery(instrument))
    context, _ = variants()
    identity = _identity(instrument, events, context.strategy, context.parameter_set)
    checkpoint_path = checkpoint_path or OUT / f"{instrument.lower()}_discovery_checkpoint.json"
    checkpoint = _restore_payload(checkpoint_path, identity)
    evaluator = OptimizedContextRawOhlcEvaluator()
    evaluator.initialize(context.strategy, context.parameter_set)
    execution = SimulatedExecution(CostModel("research-zero-engine-cost"))
    setups: list[SetupLifecycleEvent] = []
    signals: list[EntrySignal] = []
    start_index = 0
    started_at = time.perf_counter()
    if checkpoint:
        start_index = int(checkpoint["last_consumed_index"])
        _restore_evaluator(evaluator, events, start_index, checkpoint["evaluator"])
        _restore_execution(execution, checkpoint["execution"])
        setups = [SetupLifecycleEvent(**item) for item in checkpoint.get("setups", [])]
        signals = [_signal_from_payload(item) for item in checkpoint.get("signals", [])]
    original_replay = raw_adapters._replay
    raw_adapters._replay = _fast_replay_factory(events)
    profile: list[dict[str, Any]] = []
    if profile_path and profile_path.exists():
        prior_profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if prior_profile.get("identity") != identity:
            raise RuntimeError("memory profile identity mismatch; refusing append")
        profile = list(prior_profile.get("checkpoints", []))
    try:
        for index in range(start_index, len(events)):
            event = events[index]
            for output in evaluator.consume_market_event(event):
                if isinstance(output, SetupLifecycleEvent):
                    setups.append(output)
                elif isinstance(output, EntrySignal):
                    signals.append(output)
                    execution.submit(output)
            execution.consume(event)
            processed = index + 1
            if processed % cadence_bars == 0 or processed == len(events) or (stop_after is not None and processed >= stop_after):
                current_rss = _rss_mib()
                row = {
                    "bars_processed": processed,
                    "m15_evaluations": sum(1 for item in events[:processed] if item.close_timestamp % 900 == 0),
                    "candidate_count": len(evaluator.setups),
                    "feature_snapshots": sum("context_snapshot" in item for item in evaluator.setups.values()),
                    "retained_snapshot_count": sum("context_snapshot" in item for item in evaluator.setups.values()),
                    "active_trade_count": len(execution._open),
                    "completed_trade_count": len(execution.outcomes),
                    "index_size": sum(index.source_count for index in raw_adapters._replay(evaluator.state, event.close_timestamp)._indexes.values()),
                    "elapsed_seconds": time.perf_counter() - started_at,
                    "rss_mib": current_rss,
                    "gc_objects": len(gc.get_objects()),
                }
                profile.append(row)
                _atomic_json(checkpoint_path, _checkpoint_payload(identity, processed, events, evaluator, execution, setups, signals, row))
                if profile_path:
                    _atomic_json(profile_path, {"schema": "research.context_memory_growth_profile.v1", "identity": identity, "checkpoints": profile, "validation_outcomes_accessed": False, "broker_writes": 0})
                if stop_after is not None and processed >= stop_after:
                    return {"status": "CHECKPOINTED", "profile": profile, "checkpoint": str(checkpoint_path), "identity": identity, "setup_count": len(setups), "signal_count": len(signals), "outcome_count": len(execution.outcomes)}
                if current_rss >= max_rss_mib:
                    return {"status": "RSS_GUARD_STOPPED", "profile": profile, "checkpoint": str(checkpoint_path), "identity": identity, "setup_count": len(setups), "signal_count": len(signals), "outcome_count": len(execution.outcomes)}
        execution.finalize(events[-1] if events else None)
    finally:
        raw_adapters._replay = original_replay
    result = _write_result(instrument, identity, setups, signals, execution.outcomes, time.perf_counter() - started_at, max([row["rss_mib"] for row in profile], default=_rss_mib()))
    return {"status": "COMPLETED", "result": result, "profile": profile, "checkpoint": str(checkpoint_path), "identity": identity, "setup_count": len(setups), "signal_count": len(signals), "outcome_count": len(execution.outcomes)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("instrument", choices=("EURUSD", "GBPUSD"))
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--cadence-bars", type=int, default=DEFAULT_CADENCE_BARS)
    parser.add_argument("--max-rss-mib", type=int, default=DEFAULT_MAX_RSS_MIB)
    parser.add_argument("--stop-after", type=int)
    args = parser.parse_args()
    print(json.dumps(run(args.instrument, checkpoint_path=args.checkpoint, profile_path=args.profile, cadence_bars=args.cadence_bars, max_rss_mib=args.max_rss_mib, stop_after=args.stop_after), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
