"""Research-only Morning/Evening Star Reversal V1 evaluator."""
from __future__ import annotations

from typing import Any

from .price_action_primitives import Candle, star_reversal_confirmation
from .price_action_reversal_common import ReversalEvaluator, contract_parameter_set, contract_strategy_version

STRATEGY_ID = "PRICE_ACTION_STAR_REVERSAL"
VERSION = "V1"
STRATEGY_VERSION_ID = f"{STRATEGY_ID}@{VERSION}"
EVALUATOR_KEY = "PRICE_ACTION_STAR_REVERSAL_V1_RESEARCH"
EVALUATOR_VERSION = "price-action-star-reversal-v1-evaluator-1"
PRIMITIVE_MANIFEST = {
    "structure.confirmed_swings@1": "strict-left-right-2-confirmed-on-right-close",
    "structure.sr_levels@1": "confirmed-pivot-price-clusters-nearest-selection",
    "structure.prior_move@1": "strict-monotonic-completed-execution-closes",
    "price_action.zone_interaction@1": "completed-candle-extreme-enters-known-structural-zone",
    "price_action.star_reversal@1": "three-candle-body-recovery-with-optional-continuous-market-gap",
    "risk.structural_stop@1": "pattern-extreme-plus-tick-buffer",
    "risk.opposing_target@1": "nearest-known-opposing-level",
}
RESEARCH_PARAMETER_VALUES = {
    "context_timeframe": "H1", "execution_timeframe": "M15",
    "swing_left_bars": 2, "swing_right_bars": 2,
    "level_lookback_context_bars": 80, "zone_tolerance_ticks": 8,
    "zone_max_age_context_bars": 120, "prior_move_bars": 3,
    "minimum_first_body_ticks": 4, "minimum_third_body_ticks": 4,
    "middle_body_max_fraction_first": 0.25, "third_recovery_fraction": 0.50,
    "require_exchange_gap": False, "stop_buffer_ticks": 2,
    "minimum_planned_r": 1.5, "entry_gap_cancel_ticks": 8,
}


class StarReversalEvaluator(ReversalEvaluator):
    STRATEGY_ID = STRATEGY_ID
    VERSION = VERSION
    EVALUATOR_KEY = EVALUATOR_KEY
    EVALUATOR_VERSION = EVALUATOR_VERSION
    RESEARCH_PARAMETER_VALUES = RESEARCH_PARAMETER_VALUES
    PRIMITIVE_MANIFEST = PRIMITIVE_MANIFEST
    DISPLAY_NAME = "Morning/Evening Star Reversal"

    def detect_pattern(self, candles: tuple[Candle, ...], tick: float) -> dict[str, Any] | None:
        if len(candles) < 3:
            return None
        first, middle, third = candles[-3:]
        if star_reversal_confirmation(first, middle, third,
                                      minimum_first_body=tick * self.values["minimum_first_body_ticks"],
                                      minimum_third_body=tick * self.values["minimum_third_body_ticks"],
                                      middle_body_max_fraction=self.values["middle_body_max_fraction_first"],
                                      recovery_fraction=self.values["third_recovery_fraction"], direction="LONG"):
            return {"direction": "LONG", "primitive_id": "price_action.star_reversal@1",
                    "observed": {"first": first.payload(), "middle": middle.payload(),
                                 "third": third.payload(), "recovery_above_first_body_midpoint": True,
                                 "exchange_gap_required": False}, "pattern_start_index": len(candles) - 3,
                    "pattern_end_index": len(candles) - 1, "pattern_high": max(first.high, middle.high, third.high),
                    "pattern_low": min(first.low, middle.low, third.low),
                    "constituent_candle_ids": [first.open_timestamp, middle.open_timestamp, third.open_timestamp]}
        if star_reversal_confirmation(first, middle, third,
                                      minimum_first_body=tick * self.values["minimum_first_body_ticks"],
                                      minimum_third_body=tick * self.values["minimum_third_body_ticks"],
                                      middle_body_max_fraction=self.values["middle_body_max_fraction_first"],
                                      recovery_fraction=self.values["third_recovery_fraction"], direction="SHORT"):
            return {"direction": "SHORT", "primitive_id": "price_action.star_reversal@1",
                    "observed": {"first": first.payload(), "middle": middle.payload(),
                                 "third": third.payload(), "recovery_below_first_body_midpoint": True,
                                 "exchange_gap_required": False}, "pattern_start_index": len(candles) - 3,
                    "pattern_end_index": len(candles) - 1, "pattern_high": max(first.high, middle.high, third.high),
                    "pattern_low": min(first.low, middle.low, third.low),
                    "constituent_candle_ids": [first.open_timestamp, middle.open_timestamp, third.open_timestamp]}
        return None


def strategy_version():
    return contract_strategy_version(STRATEGY_ID, EVALUATOR_KEY,
                                     "price-action-star-reversal-v1", RESEARCH_PARAMETER_VALUES)


def parameter_set():
    return contract_parameter_set("price-action-star-reversal-v1-experimental",
                                  STRATEGY_VERSION_ID, "price-action-star-reversal-v1",
                                  RESEARCH_PARAMETER_VALUES)
