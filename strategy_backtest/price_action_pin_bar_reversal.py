"""Research-only Rejection / Pin-Bar Reversal V1 evaluator."""
from __future__ import annotations

from typing import Any

from .price_action_primitives import Candle, pin_bar_confirmation
from .price_action_reversal_common import ReversalEvaluator, contract_parameter_set, contract_strategy_version

STRATEGY_ID = "PRICE_ACTION_PIN_BAR_REVERSAL"
VERSION = "V1"
STRATEGY_VERSION_ID = f"{STRATEGY_ID}@{VERSION}"
EVALUATOR_KEY = "PRICE_ACTION_PIN_BAR_REVERSAL_V1_RESEARCH"
EVALUATOR_VERSION = "price-action-pin-bar-reversal-v1-evaluator-1"
PRIMITIVE_MANIFEST = {
    "structure.confirmed_swings@1": "strict-left-right-2-confirmed-on-right-close",
    "structure.sr_levels@1": "confirmed-pivot-price-clusters-nearest-selection",
    "structure.prior_move@1": "strict-monotonic-completed-execution-closes",
    "price_action.zone_interaction@1": "completed-candle-extreme-enters-known-structural-zone",
    "price_action.pin_bar@1": "directional-close-wick-body-ratio-and-minimum-range",
    "risk.structural_stop@1": "pattern-extreme-plus-tick-buffer",
    "risk.opposing_target@1": "nearest-known-opposing-level",
}
RESEARCH_PARAMETER_VALUES = {
    "context_timeframe": "H1", "execution_timeframe": "M15",
    "swing_left_bars": 2, "swing_right_bars": 2,
    "level_lookback_context_bars": 80, "zone_tolerance_ticks": 8,
    "zone_max_age_context_bars": 120, "prior_move_bars": 3,
    "minimum_range_ticks": 8, "minimum_wick_body_ratio": 2.0,
    "maximum_opposite_wick_body_ratio": 0.50, "minimum_close_location": 0.70,
    "stop_buffer_ticks": 2, "minimum_planned_r": 1.5,
    "entry_gap_cancel_ticks": 8,
}


class PinBarReversalEvaluator(ReversalEvaluator):
    STRATEGY_ID = STRATEGY_ID
    VERSION = VERSION
    EVALUATOR_KEY = EVALUATOR_KEY
    EVALUATOR_VERSION = EVALUATOR_VERSION
    RESEARCH_PARAMETER_VALUES = RESEARCH_PARAMETER_VALUES
    PRIMITIVE_MANIFEST = PRIMITIVE_MANIFEST
    DISPLAY_NAME = "Rejection / Pin-Bar Reversal"

    def detect_pattern(self, candles: tuple[Candle, ...], tick: float) -> dict[str, Any] | None:
        if not candles:
            return None
        candle = candles[-1]
        total_range = candle.high - candle.low
        body = abs(candle.close - candle.open)
        if total_range <= 0:
            return {"rejection_reason": "ZERO_RANGE_INVALID"}
        if body <= 0:
            return {"rejection_reason": "ZERO_BODY_UNSUPPORTED"}
        if total_range < tick * self.values["minimum_range_ticks"]:
            return {"rejection_reason": "RANGE_BELOW_MINIMUM"}
        lower_wick = min(candle.open, candle.close) - candle.low
        upper_wick = candle.high - max(candle.open, candle.close)
        long_location = (candle.close - candle.low) / total_range
        short_location = (candle.high - candle.close) / total_range
        if pin_bar_confirmation(candle, minimum_range=tick * self.values["minimum_range_ticks"],
                                minimum_wick_body_ratio=self.values["minimum_wick_body_ratio"],
                                maximum_opposite_wick_body_ratio=self.values["maximum_opposite_wick_body_ratio"],
                                minimum_close_location=self.values["minimum_close_location"], direction="LONG"):
            return {"direction": "LONG", "primitive_id": "price_action.pin_bar@1",
                    "observed": {"bar": candle.payload(), "wick_body_ratio": lower_wick / body,
                                 "opposite_wick_body_ratio": upper_wick / body,
                                 "close_location": long_location, "zero_body_policy": "reject"},
                    "pattern_start_index": len(candles) - 1, "pattern_end_index": len(candles) - 1,
                    "pattern_high": candle.high, "pattern_low": candle.low,
                    "constituent_candle_ids": [candle.open_timestamp]}
        if pin_bar_confirmation(candle, minimum_range=tick * self.values["minimum_range_ticks"],
                                minimum_wick_body_ratio=self.values["minimum_wick_body_ratio"],
                                maximum_opposite_wick_body_ratio=self.values["maximum_opposite_wick_body_ratio"],
                                minimum_close_location=self.values["minimum_close_location"], direction="SHORT"):
            return {"direction": "SHORT", "primitive_id": "price_action.pin_bar@1",
                    "observed": {"bar": candle.payload(), "wick_body_ratio": upper_wick / body,
                                 "opposite_wick_body_ratio": lower_wick / body,
                                 "close_location": short_location, "zero_body_policy": "reject"},
                    "pattern_start_index": len(candles) - 1, "pattern_end_index": len(candles) - 1,
                    "pattern_high": candle.high, "pattern_low": candle.low,
                    "constituent_candle_ids": [candle.open_timestamp]}
        return {"rejection_reason": "PIN_BAR_GEOMETRY_NOT_CONFIRMED"}


def strategy_version():
    return contract_strategy_version(STRATEGY_ID, EVALUATOR_KEY,
                                     "price-action-pin-bar-reversal-v1", RESEARCH_PARAMETER_VALUES)


def parameter_set():
    return contract_parameter_set("price-action-pin-bar-reversal-v1-experimental",
                                  STRATEGY_VERSION_ID, "price-action-pin-bar-reversal-v1",
                                  RESEARCH_PARAMETER_VALUES)
