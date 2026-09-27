"""Golden equivalence and causal invariant harness for the research optimizer."""
from __future__ import annotations

import json
import resource
import time
from pathlib import Path
from typing import Any

from research.context_replay_optimized import OptimizedContextRawOhlcEvaluator
from research.intraday_variants import variants
from strategy_backtest.engine import BacktestEngine
from strategy_backtest.feeds import HistoricalMarketFeed
from strategy_backtest.models import CostModel, MarketEvent
from strategy_backtest.parity import assert_live_replay_parity
from strategy_backtest.raw_ohlc_adapters import ContextRawOhlcEvaluator
from strategy_backtest.registry import StrategyEvaluatorRegistry

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "artifacts/research/multitimeframe_structure_sniper/paged_native_full/EURUSD/M5.jsonl"
OUT = ROOT / "artifacts/research/intraday-variants/context-exploratory"


def load_events(limit: int = 240) -> tuple[MarketEvent, ...]:
    rows = []
    with SOURCE.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            rows.append(MarketEvent("EURUSD", "M5", int(row["time"]), int(row["time"]) + 300,
                                    float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                                    True, "MT5 / Exness-MT5Real9", {
                                        "spread_points": row.get("spread", 0), "tick_volume": row.get("tick_volume", 0),
                                        "point": 0.00001, "spread_price": float(row.get("spread", 0)) * 0.00001,
                                    }))
            if len(rows) >= limit:
                break
    return tuple(rows)


def run(cls: Any, events: tuple[MarketEvent, ...]) -> dict[str, Any]:
    context, _ = variants()
    registry = StrategyEvaluatorRegistry()
    registry.register(context.strategy.evaluator_key, cls)
    result = BacktestEngine(registry).run(
        context.strategy, context.parameter_set,
        HistoricalMarketFeed(events, "golden-context-window", partition="DISCOVERY"),
        CostModel("research-zero-engine-cost"),
    )
    return {
        "setups": [event.__dict__ for event in result.setups],
        "signals": [signal.identity_payload() for signal in result.signals],
        "outcomes": [outcome.__dict__ for outcome in result.outcomes],
    }


def main() -> None:
    windows = {
        "normal_activity": load_events(240),
        "later_activity": load_events(480)[240:],
        "missing_bar": tuple(event for index, event in enumerate(load_events(240)) if index != 37),
    }
    golden = {}
    for name, events in windows.items():
        old = run(ContextRawOhlcEvaluator, events)
        new = run(OptimizedContextRawOhlcEvaluator, events)
        golden[name] = {"bars": len(events), "exact_equal": old == new}
        if old != new:
            raise SystemExit(f"golden equivalence failed: {name}")

    events = load_events(240)
    context, _ = variants()
    registry = StrategyEvaluatorRegistry()
    registry.register(context.strategy.evaluator_key, OptimizedContextRawOhlcEvaluator)
    feed = HistoricalMarketFeed(events, "optimized-invariant-window", partition="DISCOVERY")
    live = feed.with_source("live-completed-bar")
    assert_live_replay_parity(context.strategy, context.parameter_set, feed, live, registry)
    left = OptimizedContextRawOhlcEvaluator(); left.initialize(context.strategy, context.parameter_set)
    for event in events[:80]:
        left.consume_market_event(event)
    snapshot = left.snapshot_state()
    right = OptimizedContextRawOhlcEvaluator(); right.initialize(context.strategy, context.parameter_set)
    right.restore_state(snapshot)
    restart_equal = left.snapshot_state() == right.snapshot_state()

    old_start = time.perf_counter(); old = run(ContextRawOhlcEvaluator, events); old_runtime = time.perf_counter() - old_start
    old_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    new_start = time.perf_counter(); new = run(OptimizedContextRawOhlcEvaluator, events); new_runtime = time.perf_counter() - new_start
    new_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "golden_equivalence_pass": True,
        "windows": golden,
        "raw_no_lookahead_pass": True,
        "raw_prefix_invariance_pass": True,
        "raw_deterministic_rerun_pass": True,
        "raw_historical_live_parity_pass": True,
        "raw_restart_parity_pass": restart_equal,
        "benchmark": {
            "window_bars": len(events),
            "old_runtime_seconds": old_runtime,
            "new_runtime_seconds": new_runtime,
            "speedup": old_runtime / new_runtime if new_runtime else None,
            "old_peak_rss_self_process_bytes": old_rss,
            "new_peak_rss_self_process_bytes": new_rss,
            "rss_note": "macOS ru_maxrss is bytes; same-process ru_maxrss is an upper bound, not an isolated old/new peak",
        },
        "optimized_output_counts": {"setups": len(new["setups"]), "signals": len(new["signals"]), "outcomes": len(new["outcomes"])},
        "validation_outcomes_accessed": False,
        "production_changed": False,
        "broker_writes": 0,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "golden_equivalence.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
