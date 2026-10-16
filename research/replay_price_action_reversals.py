"""Deterministic synthetic replay for the three new reversal definitions.

This is deliberately a fixture replay, not a historical backtest. It exercises
the pure detector contracts and emits episode-linked traces so CI can reconcile
accepted and rejected synthetic episodes while the audited raw dataset is
unavailable.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from collections import Counter

from strategy_backtest.models import MarketEvent
from strategy_backtest.price_action_engulfing_reversal import EngulfingReversalEvaluator, parameter_set as engulfing_parameters, strategy_version as engulfing_version
from strategy_backtest.price_action_pin_bar_reversal import PinBarReversalEvaluator, parameter_set as pin_parameters, strategy_version as pin_version
from strategy_backtest.price_action_primitives import Candle
from strategy_backtest.price_action_star_reversal import StarReversalEvaluator, parameter_set as star_parameters, strategy_version as star_version


def _candle(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(index, index * 900, (index + 1) * 900, open_, high, low, close)


def _bar(index: int, timeframe: str, open_: float, high: float, low: float, close: float) -> MarketEvent:
    step = 3600 if timeframe == "H1" else 900
    provenance = {"tick_size": 0.01, "snapshot_id": "price-action-reversal-synthetic-v1"}
    if timeframe == "H1":
        provenance["context_watermark_timestamp"] = 10_000_000 if index == 10 else (index + 1) * step
    return MarketEvent("TEST", timeframe, index * step, (index + 1) * step,
                       open_, high, low, close, provenance=provenance)


def _context() -> list[MarketEvent]:
    return [_bar(i, "H1", *values) for i, values in enumerate((
        (100, 103, 98, 101), (101, 105, 96, 103), (103, 108, 94, 106),
        (106, 108, 92, 98), (98, 104, 95, 101), (101, 110, 93, 104),
        (104, 104, 89, 96), (96, 108, 94, 103), (103, 107, 95, 101),
        (101, 110, 96, 105), (105, 106, 98, 102)))]


def _lifecycle(evaluator, version, parameters, execution: list[MarketEvent]) -> dict:
    evaluator.initialize(version(), parameters())
    outputs = []
    for event in _context() + execution:
        outputs.extend(evaluator.consume_market_event(event))
    reasons = Counter(item.provenance["rejection_reason"] for item in outputs)
    return {
        "evaluations": len(outputs),
        "proposals": reasons["RESEARCH_PROPOSAL_ONLY"],
        "accepted_reconstructed_entries": reasons["ENTRY_OBSERVED_RESEARCH_ONLY"],
        "cancellations": sum(count for reason, count in reasons.items() if reason in {
            "ENTRY_GAP_CANCELLED", "STRUCTURAL_INVALIDATION", "INVALID_ACTUAL_ENTRY_GEOMETRY",
            "INSUFFICIENT_ACTUAL_ENTRY_R", "MISSING_EXPECTED_NEXT_CANDLE"}),
        "pending_episodes": len(evaluator.pending),
        "rejection_counts": dict(sorted(reasons.items())),
        "episode_ids": [item.candidate_id for item in outputs if item.candidate_id],
    }


def _fixture(evaluator, version, parameters, accepted: tuple[Candle, ...], rejected: tuple[Candle, ...]):
    evaluator.initialize(version(), parameters())
    traces = []
    accepted_result = evaluator.detect_pattern(accepted, 0.01)
    rejected_result = evaluator.detect_pattern(rejected, 0.01)
    traces.append({"episode_id": f"{evaluator.STRATEGY_ID}:accepted", "classification": "accepted_pattern", "pattern": accepted_result})
    traces.append({"episode_id": f"{evaluator.STRATEGY_ID}:rejected", "classification": "rejected_pattern", "pattern": rejected_result})
    return {"strategy_version_id": evaluator.strategy_version_id, "episodes": traces,
            "accepted_pattern_count": int(accepted_result is not None and "rejection_reason" not in accepted_result),
            "rejected_pattern_count": int(rejected_result is None or "rejection_reason" in rejected_result)}


def replay() -> dict:
    strategies = [
        _fixture(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters,
                 (_candle(0, 91, 91.2, 88.8, 89), _candle(1, 88.9, 91.2, 88.8, 91)),
                 (_candle(0, 91, 91.2, 90.8, 91), _candle(1, 90.9, 91.1, 90.7, 91))),
        _fixture(StarReversalEvaluator(), star_version, star_parameters,
                 (_candle(0, 91, 91.2, 88.9, 89.5), _candle(1, 89.5, 90, 89.2, 89.7), _candle(2, 89.5, 91.5, 88.8, 91.2)),
                 (_candle(0, 91, 91.2, 90.8, 91), _candle(1, 90.9, 91, 90.8, 90.95), _candle(2, 90.9, 91.1, 90.7, 90.8))),
        _fixture(PinBarReversalEvaluator(), pin_version, pin_parameters,
                 (_candle(0, 89.1, 89.35, 88.6, 89.3),),
                 (_candle(0, 89, 89.4, 88.9, 89.2),)),
    ]
    lifecycle = {
        "PRICE_ACTION_ENGULFING_REVERSAL@V1": _lifecycle(
            EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters,
            [_bar(50, "M15", 96, 96.2, 94.8, 95), _bar(51, "M15", 95, 95.2, 92.8, 93),
             _bar(52, "M15", 93, 93.2, 90.8, 91), _bar(53, "M15", 91, 91.2, 89.8, 90),
             _bar(54, "M15", 90, 90.2, 88.8, 89), _bar(55, "M15", 88.9, 91.2, 88.8, 91.1),
             _bar(56, "M15", 91.1, 92, 90, 91.5)]),
        "PRICE_ACTION_STAR_REVERSAL@V1": _lifecycle(
            StarReversalEvaluator(), star_version, star_parameters,
            [_bar(50, "M15", 96, 96.2, 94.8, 95), _bar(51, "M15", 95, 95.2, 92.8, 93),
             _bar(52, "M15", 93, 93.2, 90.8, 91), _bar(53, "M15", 91, 91.2, 88.8, 89.5),
             _bar(54, "M15", 89.5, 90, 89.2, 89.7), _bar(55, "M15", 89.5, 91.5, 88.8, 91.2),
             _bar(56, "M15", 91.2, 92, 90, 91.5)]),
        "PRICE_ACTION_PIN_BAR_REVERSAL@V1": _lifecycle(
            PinBarReversalEvaluator(), pin_version, pin_parameters,
            [_bar(50, "M15", 96, 96.2, 94.8, 95), _bar(51, "M15", 95, 95.2, 92.8, 93),
             _bar(52, "M15", 93, 93.2, 90.8, 91), _bar(53, "M15", 91, 91.2, 89.8, 90),
             _bar(54, "M15", 90, 90.2, 88.8, 89), _bar(55, "M15", 89.1, 89.35, 88.6, 89.3),
             _bar(56, "M15", 89.3, 90, 88.8, 89.5)]),
    }
    return {
        "schema": "price-action-reversal-synthetic-replay-v1",
        "evidence_class": "synthetic_fixture",
        "historical_claim": False,
        "strategies": strategies,
        "lifecycle_by_strategy": lifecycle,
        "count_reconciliation": {strategy: report["evaluations"] == sum(report["rejection_counts"].values())
                                  for strategy, report in lifecycle.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = replay()
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
