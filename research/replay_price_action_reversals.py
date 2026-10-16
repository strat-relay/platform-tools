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

from strategy_backtest.price_action_engulfing_reversal import EngulfingReversalEvaluator, parameter_set as engulfing_parameters, strategy_version as engulfing_version
from strategy_backtest.price_action_pin_bar_reversal import PinBarReversalEvaluator, parameter_set as pin_parameters, strategy_version as pin_version
from strategy_backtest.price_action_primitives import Candle
from strategy_backtest.price_action_star_reversal import StarReversalEvaluator, parameter_set as star_parameters, strategy_version as star_version


def _candle(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(index, index * 900, (index + 1) * 900, open_, high, low, close)


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
    return {
        "schema": "price-action-reversal-synthetic-replay-v1",
        "evidence_class": "synthetic_fixture",
        "historical_claim": False,
        "strategies": [
            _fixture(EngulfingReversalEvaluator(), engulfing_version, engulfing_parameters,
                     (_candle(0, 91, 91.2, 88.8, 89), _candle(1, 88.9, 91.2, 88.8, 91)),
                     (_candle(0, 91, 91.2, 90.8, 91), _candle(1, 90.9, 91.1, 90.7, 91))),
            _fixture(StarReversalEvaluator(), star_version, star_parameters,
                     (_candle(0, 91, 91.2, 88.9, 89.5), _candle(1, 89.5, 90, 89.2, 89.7), _candle(2, 89.5, 91.5, 88.8, 91.2)),
                     (_candle(0, 91, 91.2, 90.8, 91), _candle(1, 90.9, 91, 90.8, 90.95), _candle(2, 90.9, 91.1, 90.7, 90.8))),
            _fixture(PinBarReversalEvaluator(), pin_version, pin_parameters,
                     (_candle(0, 89.1, 89.35, 88.6, 89.3),),
                     (_candle(0, 89, 89.4, 88.9, 89.2),)),
        ],
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
