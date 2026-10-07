"""Deterministic research comparison for Context raw-OHLC evaluators.

This tool never reads or writes production state. The input is an immutable JSONL export of
completed M5 bars; higher timeframes are rebuilt causally by the research adapter.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

# Support both `python -m research.context_structure_retrace_parity` and the documented
# direct invocation from a checkout. This script is research-only and must not import
# production runtime state.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy_backtest.feeds import HistoricalMarketFeed
from strategy_backtest.models import MarketEvent
from strategy_backtest.registry import StrategyEvaluatorRegistry, register_builtin_evaluators
from strategy_backtest.parity import evaluate_sequential
from strategy_backtest.models import ParameterSet
from context_structure_retrace_v2 import research_metadata, research_parameter_set, research_strategy_version


def load(path: Path, symbols: set[str] | None = None) -> tuple[MarketEvent, ...]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if symbols and row.get("canonical_instrument") not in symbols:
            continue
        rows.append(MarketEvent(**row))
    return tuple(sorted(rows, key=lambda event: (event.open_timestamp, event.canonical_instrument, event.timeframe)))


def run(path: Path, symbols: set[str] | None = None) -> dict:
    events = load(path, symbols)
    grouped: dict[str, list[MarketEvent]] = defaultdict(list)
    for event in events:
        grouped[event.canonical_instrument].append(event)
    parameter_set = research_parameter_set()
    v2 = research_strategy_version()
    v1 = type(v2)("CONTEXT_STRUCTURE_RETRACE_V1", "V1", "context_structure_retrace_intraday_v1",
                  v2.parameter_schema, lifecycle="RESEARCH_ONLY")
    v1_parameters = ParameterSet("context-v1-research-parity", v1.strategy_version_id,
                                 v2.parameter_schema.schema_id, dict(parameter_set.values),
                                 {"research_only": True, "broker_writes": False})
    v1_outputs, v2_outputs = [], []
    fingerprints = {}
    for instrument in sorted(grouped):
        feed = HistoricalMarketFeed(tuple(grouped[instrument]), dataset_id=f"{path}:{instrument}", partition="VALIDATION")
        fingerprints[instrument] = feed.dataset_fingerprint
        v1_outputs.extend(evaluate_sequential(v1, v1_parameters, feed,
                                              register_builtin_evaluators(StrategyEvaluatorRegistry())))
        v2_outputs.extend(evaluate_sequential(v2, parameter_set, feed,
                                              register_builtin_evaluators(StrategyEvaluatorRegistry())))
    def signals(outputs):
        return [asdict(row) for row in outputs if row.__class__.__name__ == "EntrySignal"]
    def output_counts(outputs):
        counts = defaultdict(int)
        for row in outputs:
            counts[row.__class__.__name__] += 1
        return dict(sorted(counts.items()))
    left, right = signals(v1_outputs), signals(v2_outputs)
    return {"dataset": str(path), "dataset_fingerprints": fingerprints,
            "instruments": sorted({event.canonical_instrument for event in events}),
            "timeframes": sorted({event.timeframe for event in events}),
            "v1": {"signals": len(left), "output_counts": output_counts(v1_outputs), "records": left},
            "v2": {"signals": len(right), "output_counts": output_counts(v2_outputs), "records": right},
            "v2_metadata": research_metadata(),
            "research_only": True,
            "note": "This compares research adapters; live runner parity requires the exact live snapshot and live state-machine replay."}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.input, set(args.symbols) if args.symbols else None)
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
