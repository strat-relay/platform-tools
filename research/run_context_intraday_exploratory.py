"""Run the frozen Context intraday raw-OHLC exploratory discovery study.

This is deliberately research-only.  It reads the already qualified native
MT5 exports, freezes a chronological boundary before running the engine, and
never opens or evaluates the reserved validation suffix.  The small replay
cache is an execution optimization for the existing raw adapter: it produces
the same completed-candle aggregation as the adapter's causal replay, without
changing any parent predicate or parameter.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.intraday_variants import variants  # noqa: E402
from strategy_backtest.engine import BacktestEngine  # noqa: E402
from strategy_backtest.feeds import HistoricalMarketFeed  # noqa: E402
from strategy_backtest.intraday_adapters import aggregate_completed_events  # noqa: E402
from strategy_backtest.metrics import calculate_metrics  # noqa: E402
from strategy_backtest.models import CostModel, MarketEvent, fingerprint  # noqa: E402
from strategy_backtest.registry import StrategyEvaluatorRegistry  # noqa: E402
import strategy_backtest.raw_ohlc_adapters as raw_adapters  # noqa: E402
from context_structure_retrace.data import CausalReplay  # noqa: E402


DATA_ROOT = ROOT.parent / "artifacts/research/multitimeframe_structure_sniper/paged_native_full"
OUT = ROOT / "artifacts/research/intraday-variants/context-exploratory"
INVENTORY = ROOT / "artifacts/research/intraday-variants/historical_data_inventory.json"
# Freeze a short, contiguous first discovery cohort and reserve the later
# majority of the qualified export untouched.  The exact boundary is part of
# the protocol artifact and is never selected from performance.
SPLIT_ISO = "2026-01-01T00:00:00Z"
SPLIT = int(datetime.fromisoformat(SPLIT_ISO.replace("Z", "+00:00")).timestamp())
INSTRUMENTS = ("EURUSD", "GBPUSD")


def _load_discovery(instrument: str) -> list[MarketEvent]:
    path = DATA_ROOT / instrument / "M5.jsonl"
    events: list[MarketEvent] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            timestamp = int(row["time"])
            if timestamp >= SPLIT:
                break
            events.append(MarketEvent(
                instrument, "M5", timestamp, timestamp + 300,
                float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                True, "MT5 / Exness-MT5Real9",
                {
                    "spread_points": row.get("spread", 0),
                    "tick_volume": row.get("tick_volume", 0),
                    "point": 0.00001,
                    "spread_price": float(row.get("spread", 0)) * 0.00001,
                    "provider_symbol": instrument,
                },
            ))
    return events


def _fast_replay_factory(all_events: tuple[MarketEvent, ...]):
    """Return the same causal replay view with precomputed completed buckets."""
    higher = {
        timeframe: aggregate_completed_events(all_events, timeframe)
        for timeframe in ("M15", "H1", "H4")
    }

    def replay(state: Any, as_of: int) -> CausalReplay:
        bars = {
            "M5": [event for event in state.events if event.close_timestamp <= as_of],
            **{
                timeframe: [event for event in events if event.close_timestamp <= as_of]
                for timeframe, events in higher.items()
            },
        }
        return CausalReplay({timeframe: [
            {
                "time": event.open_timestamp,
                "open": event.open,
                "high": event.high,
                "low": event.low,
                "close": event.close,
                "spread": event.provenance.get("spread_points", 0),
                "tick_volume": event.provenance.get("tick_volume", 0),
            }
            for event in events
        ] for timeframe, events in bars.items()})

    return replay


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _funnel(result: Any) -> dict[str, Any]:
    statuses = Counter(event.status for event in result.setups)
    detected = {event.setup_id for event in result.setups if event.status == "SETUP_DETECTED"}
    entered = {event.setup_id for event in result.setups if event.status == "ENTERED"}
    outcomes_by_signal = {outcome.signal_id: outcome for outcome in result.outcomes}
    closed = [outcome for outcome in result.outcomes if outcome.reason != "END_OF_DATA"]
    censored = [outcome for outcome in result.outcomes if outcome.reason == "END_OF_DATA"]
    return {
        "h4_context_opportunities": None,
        "h1_retracement_candidates": None,
        "unobservable_funnel_stages": ["H4_CONTEXT", "H1_RETRACEMENT"],
        "unobservable_reason": "The committed generic event contract exposes setup lifecycle events only; it does not emit independent H4/H1 stage events.",
        "m15_confirmations": len(detected),
        "entry_candidates": len(detected),
        "filled_or_entered_trades": len(entered),
        "closed_trades": len(closed),
        "open_or_censored_trades": len(censored),
        "setup_status_counts": dict(sorted(statuses.items())),
        "signal_count": len(result.signals),
        "outcome_status_counts": dict(sorted(Counter(outcome.status for outcome in result.outcomes).items())),
        "outcome_reason_counts": dict(sorted(Counter(outcome.reason for outcome in result.outcomes).items())),
        "signals_without_outcome": sorted(set(signal.signal_id for signal in result.signals) - set(outcomes_by_signal)),
    }


def _performance(result: Any) -> dict[str, Any]:
    closed = [outcome for outcome in result.outcomes if outcome.reason != "END_OF_DATA"]
    metrics = calculate_metrics(result.signals, closed)
    rs = [outcome.realized_r for outcome in closed]
    holds = [outcome.exit_timestamp - int(outcome.provenance["entry_timestamp"]) for outcome in closed if outcome.provenance.get("entry_timestamp") is not None]
    return {
        **metrics,
        "other_or_time_exits": sum(outcome.status == "TIME_EXIT" for outcome in closed),
        "censored_open_count": sum(outcome.reason == "END_OF_DATA" for outcome in result.outcomes),
        "exit_reason_distribution": dict(sorted(Counter(outcome.reason for outcome in closed).items())),
        "median_hold_duration": median(holds) if holds else 0.0,
        "mean_hold_duration": mean(holds) if holds else 0.0,
        "realized_r_values": rs,
    }


def _monthly(result: Any) -> dict[str, Any]:
    signal_by_id = {signal.signal_id: signal for signal in result.signals}
    grouped: dict[str, list[Any]] = defaultdict(list)
    for outcome in result.outcomes:
        if outcome.reason == "END_OF_DATA":
            continue
        timestamp = int(outcome.provenance.get("entry_timestamp", outcome.exit_timestamp))
        grouped[datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m")].append(outcome)
    rows = {}
    for month, outcomes in sorted(grouped.items()):
        rows[month] = calculate_metrics([signal_by_id[outcome.signal_id] for outcome in outcomes], outcomes)
    return rows


def _run_instrument(strategy: Any, parameter_set: Any, instrument: str) -> Any:
    events = tuple(_load_discovery(instrument))
    original_replay = raw_adapters._replay
    raw_adapters._replay = _fast_replay_factory(events)
    try:
        registry = StrategyEvaluatorRegistry()
        registry.register(strategy.evaluator_key, raw_adapters.ContextRawOhlcEvaluator)
        feed = HistoricalMarketFeed(events, f"context-qualified-native-m5-{instrument}", partition="DISCOVERY")
        # The generic cost model cannot apply a per-bar spread.  Spread is still
        # present in the adapter quote/geometry; the engine cost is explicitly
        # zero and is reported as a limitation, not hidden as broker truth.
        return BacktestEngine(registry).run(strategy, parameter_set, feed, CostModel("research-zero-engine-cost"))
    finally:
        raw_adapters._replay = original_replay


def _run_instrument_worker(args: tuple[Any, Any, str]) -> Any:
    return _run_instrument(*args)


def main() -> None:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    context, _ = variants()
    # The instruments are independent and the parent feature calculations are
    # CPU-bound.  Running the same frozen evaluator in separate worker
    # processes shortens wall time without sharing mutable evaluator state.
    with ProcessPoolExecutor(max_workers=len(INSTRUMENTS)) as pool:
        completed = pool.map(_run_instrument_worker, ((context.strategy, context.parameter_set, instrument) for instrument in INSTRUMENTS))
        results = dict(zip(INSTRUMENTS, completed))
    all_signals = tuple(signal for result in results.values() for signal in result.signals)
    all_outcomes = tuple(outcome for result in results.values() for outcome in result.outcomes)
    all_setups = tuple(event for result in results.values() for event in result.setups)
    combined = type("Combined", (), {"signals": all_signals, "outcomes": all_outcomes, "setups": all_setups})()

    source_records = []
    for instrument in INSTRUMENTS:
        source = inventory["recovered_existing_data"]["context_native_m5"]["coverage"][instrument]
        source_records.append({
            "instrument": instrument,
            "provider": "MT5 / Exness-MT5Real9",
            "dataset_fingerprint": source["fingerprint"],
            "full_range": [source["start"], source["end"]],
            "discovery_range": [source["start"], SPLIT_ISO],
            "reserved_validation_range": [SPLIT_ISO, source["end"]],
            "full_bar_count": source["bar_count"],
            "unexplained_gap_count": source["unexplained_gap_count"],
        })
    protocol = {
        "schema": "research.context_intraday_exploratory_protocol.v1",
        "status": "FROZEN_BEFORE_PERFORMANCE",
        "evidence_class": "EXPLORATORY_RAW_PIPELINE_PARENT_PARITY_UNPROVEN",
        "strategy_version": context.strategy.strategy_version_id,
        "parameter_set": context.parameter_set.canonical_payload(),
        "parameter_set_fingerprint": context.parameter_set.fingerprint,
        "engine_version": "strategy-backtest-core-v1",
        "engine_fingerprint": fingerprint({"engine": "strategy-backtest-core-v1", "raw_adapter": raw_adapters.ContextRawOhlcEvaluator.VERSION}),
        "cost_model": {"model_id": "research-zero-engine-cost", "limitation": "generic engine scalar cost cannot consume per-bar spread; adapter geometry uses source spread"},
        "exit_hypothesis": "STRUCTURE_CAPPED_EXTENSION",
        "max_hold_minutes": 1440,
        "utc_boundary_behavior": "UTC_DAY_BOUNDARY_HYPOTHESIS",
        "split_timestamp": SPLIT_ISO,
        "source_records": source_records,
        "validation_outcomes_accessed": False,
        "production_changed": False,
        "broker_writes": 0,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "discovery_protocol.json").write_text(json.dumps(protocol, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report = {
        "schema": "research.context_intraday_exploratory_result.v1",
        "status": "EXPLORATORY_RAW_PIPELINE_VALIDATED_PARENT_PARITY_UNPROVEN",
        "protocol": "discovery_protocol.json",
        "strategy_version": context.strategy.strategy_version_id,
        "parameter_set_id": context.parameter_set.parameter_set_id,
        "parameter_set_fingerprint": context.parameter_set.fingerprint,
        "split_timestamp": SPLIT_ISO,
        "source_records": source_records,
        "by_instrument": {
            instrument: {"funnel": _funnel(result), "performance": _performance(result), "monthly": _monthly(result), "result_fingerprint": fingerprint({"signals": [signal.identity_payload() for signal in result.signals], "outcomes": [outcome.__dict__ for outcome in result.outcomes]})}
            for instrument, result in results.items()
        },
        "combined": {"funnel": _funnel(combined), "performance": _performance(combined), "monthly": _monthly(combined)},
        "validation_outcomes_accessed": False,
        "parent_parity_status": "UNPROVEN",
        "production_changed": False,
        "broker_writes": 0,
    }
    (OUT / "exploratory_result.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "protocol": str(OUT / "discovery_protocol.json"),
        "result": str(OUT / "exploratory_result.json"),
        "combined_funnel": report["combined"]["funnel"],
        "combined_performance": {key: value for key, value in report["combined"]["performance"].items() if key != "realized_r_values"},
        "parent_parity_status": "UNPROVEN",
        "validation_outcomes_accessed": False,
        "broker_writes": 0,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
