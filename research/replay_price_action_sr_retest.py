"""Run the bounded synthetic Support/Resistance Retest V1 replay.

This is detection evidence only; it intentionally does not invoke the generic
execution or outcome resolver.
"""
from __future__ import annotations

import json

from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_sr_retest import SupportResistanceRetestEvaluator, parameter_set, strategy_version


def bar(index: int, timeframe: str, open_: float, high: float, low: float, close: float, *, tick: float = 0.01) -> MarketEvent:
    step = 3600 if timeframe == "H1" else 900
    return MarketEvent("SYNTH", timeframe, index * step, (index + 1) * step,
                       open_, high, low, close, provenance={"tick_size": tick, "snapshot_id": "sr-retest-synthetic-v1"})


def events() -> tuple[MarketEvent, ...]:
    context = (
        bar(0, "H1", 100, 103, 98, 101), bar(1, "H1", 101, 106, 95, 104),
        bar(2, "H1", 104, 110, 102, 107), bar(3, "H1", 107, 108, 100, 102),
        bar(4, "H1", 102, 105, 97, 100), bar(5, "H1", 100, 101, 90, 93),
        bar(6, "H1", 93, 96, 89, 91), bar(7, "H1", 91, 120, 88, 100),
        bar(8, "H1", 100, 130, 95, 125), bar(9, "H1", 125, 126, 115, 120),
        bar(10, "H1", 120, 121, 110, 115),
    )
    execution = (
        bar(50, "M15", 100, 109, 99, 105),
        bar(51, "M15", 105, 115, 104, 114),
        bar(52, "M15", 114, 115, 109, 110),
        bar(53, "M15", 110, 116, 108, 116),
        bar(54, "M15", 100, 110, 90, 95),  # next-open gap invalidates the proposal
    )
    return context + execution


def main() -> None:
    evaluator = SupportResistanceRetestEvaluator()
    evaluator.initialize(strategy_version(), parameter_set())
    evaluations = [item for event in events() for item in evaluator.consume_market_event(event)]
    print(json.dumps({"strategy": strategy_version().strategy_version_id,
                      "research_only": True, "evaluations": len(evaluations),
                      "decisions": [item.decision.value for item in evaluations],
                      "reasons": [item.provenance["rejection_reason"] for item in evaluations],
                      "diagnostics": evaluator.diagnostics()}, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
