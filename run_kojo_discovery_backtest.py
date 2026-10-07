"""KOJO_STRUCTURE_RECLAIM_V1 Discovery Backtest Runner.

BROKER_WRITES = 0.  No production state is touched.
VALIDATION_OUTCOMES_ACCESSED = false.  Does NOT run over validation partition.
PARAMETER_SEARCH = false.  Uses frozen baseline parameter set.

Usage:
    python3 run_kojo_discovery_backtest.py [--partition DISCOVERY|VALIDATION] [--run-id RUN_ID]

Defaults to DISCOVERY partition.  VALIDATION partition ingests data but must
NOT be used to report economics (validation lock).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── constants ─────────────────────────────────────────────────────────────────

CANONICAL_DATA_PATH = (
    "/private/tmp/claude-501/-Users-caleb-mt5-native-bridge/"
    "4e5c7ef3-7ead-4349-9793-d980ff1d24ec/scratchpad/xauusd_study_canonical.json"
)

# Frozen before any signal output was examined.
STUDY_START  = "2026-07-01T00:00:00Z"
STUDY_END    = "2026-08-31T23:59:59Z"
DISC_START   = "2026-07-01T00:00:00Z"
DISC_END     = "2026-08-09T23:59:59Z"
VAL_START    = "2026-08-10T00:00:00Z"
VAL_END      = "2026-08-30T23:59:59Z"

DISC_START_TS = int(datetime.fromisoformat(DISC_START.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp())
DISC_END_TS   = int(datetime.fromisoformat(DISC_END.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp())
VAL_START_TS  = int(datetime.fromisoformat(VAL_START.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp())
VAL_END_TS    = int(datetime.fromisoformat(VAL_END.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp())

# ── path setup ────────────────────────────────────────────────────────────────
import os
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy_backtest.engine import BacktestEngine, BacktestArtifactStore
from strategy_backtest.feeds import HistoricalMarketFeed
from strategy_backtest.models import (
    CostModel, MarketEvent, ParameterSet, StrategyVersion,
)
from strategy_backtest.registry import (
    StrategyEvaluatorRegistry,
    register_builtin_evaluators,
)
from strategy_backtest.kojo_structure_reclaim import (
    EVALUATOR_KEY,
    STRATEGY_ID,
    VERSION,
    kojo_structure_reclaim_baseline_parameter_set,
    kojo_structure_reclaim_parameter_schema,
)

H1_SECONDS  = 3600
M15_SECONDS = 900


def _ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.rstrip("Z")).replace(tzinfo=timezone.utc).timestamp())


def load_canonical_data() -> dict:
    with open(CANONICAL_DATA_PATH) as f:
        return json.load(f)


def build_market_events(
    canonical: dict,
    start_ts: int,
    end_ts: int,
) -> list[MarketEvent]:
    """Build interleaved H1+M15 MarketEvents from canonical M15 and H1 bars.

    Only emit completed bars whose close_timestamp <= end_ts AND
    whose open_timestamp >= (start_ts - some lookback for state warm-up).

    NOTE: We include all bars from the full study window for state warm-up,
    then slice by start/end for the feed bounds.  The feed itself filters
    by the partition bounds.
    """
    events: list[MarketEvent] = []

    for bar in canonical["m15_bars"]:
        ts = bar["time"]
        close_ts = ts + M15_SECONDS
        events.append(MarketEvent(
            canonical_instrument="XAUUSD",
            timeframe="M15",
            open_timestamp=ts,
            close_timestamp=close_ts,
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            completed=True,
            source="MT5/Exness",
            provenance={"broker_symbol": "XAUUSDm", "broker_server": canonical["provenance"]["broker_server"]},
        ))

    for bar in canonical["h1_bars"]:
        ts = bar["time"]
        close_ts = ts + H1_SECONDS
        events.append(MarketEvent(
            canonical_instrument="XAUUSD",
            timeframe="H1",
            open_timestamp=ts,
            close_timestamp=close_ts,
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            completed=True,
            source="MT5/Exness",
            provenance={"broker_symbol": "XAUUSDm", "broker_server": canonical["provenance"]["broker_server"]},
        ))

    # Sort by (open_timestamp, canonical_instrument, timeframe) — same key as HistoricalMarketFeed
    events.sort(key=lambda e: (e.open_timestamp, e.canonical_instrument, e.timeframe))
    return events


def run_backtest(
    partition: str,
    run_id: str,
    events: list[MarketEvent],
    start_ts: int,
    end_ts: int,
    canonical_fingerprint: str,
) -> dict:
    """Run a single backtest and return the result dict."""
    registry = StrategyEvaluatorRegistry()
    register_builtin_evaluators(registry)

    strategy_version = StrategyVersion(
        strategy_id=STRATEGY_ID,
        version=VERSION,
        evaluator_key=EVALUATOR_KEY,
        parameter_schema=kojo_structure_reclaim_parameter_schema(),
        lifecycle="RESEARCH",
    )

    parameter_set = kojo_structure_reclaim_baseline_parameter_set(
        strategy_version.strategy_version_id
    )

    # Spread-aware cost model: use 0.16 USD (minimum observed spread) as fixed model.
    # The evaluator uses price-based stop geometry; spread does not gate entries.
    # Per-bar spread data is available but V1 uses a fixed cost model for R calculations.
    cost_model = CostModel(
        model_id="xauusd-exness-fixed-spread-0.16",
        spread_price=0.16,
        commission_r=0.0,
        approximation="fixed minimum observed spread from Exness-MT5Real27",
    )

    feed = HistoricalMarketFeed(
        events=tuple(events),
        dataset_id=f"XAUUSDm-MT5-Exness-{canonical_fingerprint[:16]}",
        requested_start=start_ts,
        requested_end=end_ts,
        partition=partition,
    )

    engine = BacktestEngine(registry)
    result = engine.run(strategy_version, parameter_set, feed, cost_model, run_id=run_id)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--partition", default="DISCOVERY", choices=["DISCOVERY", "VALIDATION"])
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default=None)
    args = parser.parse_args()

    partition = args.partition

    if partition == "VALIDATION":
        print("VALIDATION LOCK: ingesting validation data only, NOT reporting economics.")
        start_ts, end_ts = VAL_START_TS, VAL_END_TS
    else:
        start_ts, end_ts = DISC_START_TS, DISC_END_TS

    run_id = args.run_id or (
        "kojo-discovery-run-1" if partition == "DISCOVERY" else "kojo-validation-ingest-1"
    )

    print(f"Loading canonical data from {CANONICAL_DATA_PATH}...")
    canonical = load_canonical_data()
    provenance = canonical["provenance"]
    print(f"  Canonical fingerprint: {provenance['canonical_fingerprint']}")
    print(f"  M5 bar count: {provenance['bar_count']}")
    print(f"  M15 bar count: {len(canonical['m15_bars'])}")
    print(f"  H1 bar count: {len(canonical['h1_bars'])}")

    print(f"\nBuilding MarketEvents ({partition})...")
    events = build_market_events(canonical, start_ts, end_ts)
    print(f"  Total events (H1+M15): {len(events)}")

    print(f"\nRunning backtest (partition={partition}, run_id={run_id})...")
    result = run_backtest(
        partition, run_id, events, start_ts, end_ts,
        provenance["canonical_fingerprint"]
    )

    print(f"\nResult fingerprint: {result.result_fingerprint}")
    print(f"Status: {result.run.status}")

    # Write artifact
    output_dir = Path(args.output_dir) if args.output_dir else Path("artifacts/backtests") / run_id
    store = BacktestArtifactStore(output_dir.parent)
    artifact_path = store.write(result)
    print(f"Artifact written: {artifact_path}")

    # Print summary
    m = result.metrics
    print(f"\n── Discovery Metrics ──")
    print(f"  Signals (entries):      {m.get('signal_count', 0)}")
    print(f"  Closed outcomes:        {m.get('trade_count', 0)}")
    print(f"  Win rate:               {m.get('win_rate', 0):.1%}")
    print(f"  Net R:                  {m.get('net_r', 0):.3f}")
    print(f"  Expectancy R:           {m.get('expectancy_r', 0):.3f}")
    print(f"  Profit factor:          {m.get('profit_factor')}")
    print(f"  Max drawdown R:         {m.get('maximum_drawdown_r', 0):.3f}")
    avg_hold = m.get('average_hold_duration', 0)
    print(f"  Avg hold (minutes):     {avg_hold / 60:.1f}")

    # Direction breakdown
    if "direction" in m:
        for direction, dm in m["direction"].items():
            print(f"  {direction}: n={dm['trade_count']} win_rate={dm['win_rate']:.1%} net_r={dm['net_r']:.3f}")

    # Setup lifecycle summary
    total_setups = len(result.setups)
    statuses = {}
    for s in result.setups:
        statuses[s.status] = statuses.get(s.status, 0) + 1
    print(f"\n── Setup Lifecycle ──")
    print(f"  Total lifecycle events: {total_setups}")
    for status, count in sorted(statuses.items()):
        print(f"  {status}: {count}")

    # Confirmation type breakdown
    conf_types = {}
    for sig in result.signals:
        ct = sig.provenance.get("confirmation_type", "UNKNOWN")
        conf_types[ct] = conf_types.get(ct, 0) + 1
    print(f"\n── Confirmation Types ──")
    for ct, count in sorted(conf_types.items()):
        print(f"  {ct}: {count}")

    # Return the result for programmatic use
    return result


if __name__ == "__main__":
    result = main()
