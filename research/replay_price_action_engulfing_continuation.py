"""Bounded synthetic replay for Engulfing Continuation V1."""
from __future__ import annotations

import json

from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_engulfing_continuation import EngulfingContinuationEvaluator, parameter_set, strategy_version


def bar(index: int, timeframe: str, open_: float, high: float, low: float, close: float, *, tick: float = 0.01) -> MarketEvent:
    step = 3600 if timeframe == "H1" else 900
    return MarketEvent("SYNTH", timeframe, index * step, (index + 1) * step,
                       open_, high, low, close, provenance={"tick_size": tick, "snapshot_id": "engulfing-continuation-synthetic-v1"})


def context() -> tuple[MarketEvent, ...]:
    return (
        bar(0, "H1", 100, 103, 98, 101), bar(1, "H1", 101, 106, 96, 104),
        bar(2, "H1", 104, 110, 99, 107), bar(3, "H1", 107, 108, 90, 98),
        bar(4, "H1", 98, 108, 95, 104), bar(5, "H1", 104, 115, 101, 114),
        bar(6, "H1", 114, 116, 100, 105), bar(7, "H1", 103, 118, 102, 112),
        bar(8, "H1", 112, 130, 105, 125), bar(9, "H1", 125, 125, 100, 110),
        bar(10, "H1", 110, 135, 110, 130), bar(11, "H1", 130, 145, 115, 140),
        bar(12, "H1", 140, 180, 120, 170), bar(13, "H1", 170, 170, 125, 150),
        bar(14, "H1", 150, 175, 130, 160), bar(15, "H1", 160, 165, 135, 155),
    )


def main() -> None:
    def run(events):
        evaluator = EngulfingContinuationEvaluator()
        evaluator.initialize(strategy_version(), parameter_set())
        return [item for event in events for item in evaluator.consume_market_event(event)]

    base = context() + (bar(70, "M15", 105, 106, 99.95, 100.0), bar(71, "M15", 100.0, 111, 99.9, 110))
    scenarios = {
        "valid_continuation": base + (bar(72, "M15", 110, 112, 109, 111),),
        "countertrend_engulfing_rejection": context() + (bar(70, "M15", 105, 106, 99.95, 100.0), bar(71, "M15", 100, 101, 89, 90)),
        "missing_context": (bar(0, "M15", 100, 101, 99, 100),),
        "next_open_gap_cancelled": base + (bar(72, "M15", 90, 110, 89, 95),),
    }
    scenario_results = {name: [item.provenance["rejection_reason"] for item in run(events)]
                        for name, events in scenarios.items()}
    evaluations = run(scenarios["next_open_gap_cancelled"])
    print(json.dumps({"strategy": strategy_version().strategy_version_id, "research_only": True,
                      "scenarios": scenario_results, "evaluations": len(evaluations),
                      "reasons": [item.provenance["rejection_reason"] for item in evaluations],
                      "note": "synthetic causal evidence only"}, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
