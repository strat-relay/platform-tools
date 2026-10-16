"""Research-only Engulfing Reversal V1 evaluator."""
from __future__ import annotations

from typing import Any

from .price_action_primitives import Candle, engulfing_confirmation
from .price_action_reversal_common import ReversalEvaluator, contract_parameter_set, contract_strategy_version

STRATEGY_ID = "PRICE_ACTION_ENGULFING_REVERSAL"
VERSION = "V1"
STRATEGY_VERSION_ID = f"{STRATEGY_ID}@{VERSION}"
EVALUATOR_KEY = "PRICE_ACTION_ENGULFING_REVERSAL_V1_RESEARCH"
EVALUATOR_VERSION = "price-action-engulfing-reversal-v1-evaluator-1"
PRIMITIVE_MANIFEST = {
    "structure.confirmed_swings@1": "strict-left-right-2-confirmed-on-right-close",
    "structure.sr_levels@1": "confirmed-pivot-price-clusters-nearest-selection",
    "structure.prior_move@1": "strict-monotonic-completed-execution-closes",
    "price_action.zone_interaction@1": "completed-candle-extreme-enters-known-structural-zone",
    "price_action.engulfing_reversal@1": "strict-opposing-body-engulfing-close-beyond-prior-body",
    "risk.structural_stop@1": "pattern-extreme-plus-tick-buffer",
    "risk.opposing_target@1": "nearest-known-opposing-level",
}
RESEARCH_PARAMETER_VALUES = {
    "context_timeframe": "H1", "execution_timeframe": "M15",
    "swing_left_bars": 2, "swing_right_bars": 2,
    "level_lookback_context_bars": 80, "zone_tolerance_ticks": 8,
    "zone_max_age_context_bars": 120, "prior_move_bars": 3,
    "minimum_engulfing_body_ticks": 4, "stop_buffer_ticks": 2,
    "minimum_planned_r": 1.5, "entry_gap_cancel_ticks": 8,
}


class EngulfingReversalEvaluator(ReversalEvaluator):
    STRATEGY_ID = STRATEGY_ID
    VERSION = VERSION
    EVALUATOR_KEY = EVALUATOR_KEY
    EVALUATOR_VERSION = EVALUATOR_VERSION
    RESEARCH_PARAMETER_VALUES = RESEARCH_PARAMETER_VALUES
    PRIMITIVE_MANIFEST = PRIMITIVE_MANIFEST
    DISPLAY_NAME = "Engulfing Reversal"

    def detect_pattern(self, candles: tuple[Candle, ...], tick: float) -> dict[str, Any] | None:
        if len(candles) < 2:
            return None
        previous, current = candles[-2:]
        body = abs(current.close - current.open)
        if body < tick * self.values["minimum_engulfing_body_ticks"]:
            return None
        # The frozen shared primitive is inclusive for the existing
        # continuation evaluator.  This new definition pins its own contract
        # to strict body penetration so equality at either boundary is rejected.
        if engulfing_confirmation(previous, current, direction="LONG") \
                and current.open < previous.close and current.close > previous.open:
            return {"direction": "LONG", "primitive_id": "price_action.engulfing_reversal@1",
                    "observed": {"previous": previous.payload(), "pattern": current.payload(),
                                 "confirmation": "close_beyond_prior_body_extreme"},
                    "pattern_start_index": len(candles) - 2, "pattern_end_index": len(candles) - 1,
                    "pattern_high": max(previous.high, current.high), "pattern_low": min(previous.low, current.low),
                    "constituent_candle_ids": [previous.open_timestamp, current.open_timestamp]}
        if engulfing_confirmation(previous, current, direction="SHORT") \
                and current.open > previous.close and current.close < previous.open:
            return {"direction": "SHORT", "primitive_id": "price_action.engulfing_reversal@1",
                    "observed": {"previous": previous.payload(), "pattern": current.payload(),
                                 "confirmation": "close_beyond_prior_body_extreme"},
                    "pattern_start_index": len(candles) - 2, "pattern_end_index": len(candles) - 1,
                    "pattern_high": max(previous.high, current.high), "pattern_low": min(previous.low, current.low),
                    "constituent_candle_ids": [previous.open_timestamp, current.open_timestamp]}
        return None


def strategy_version():
    return contract_strategy_version(STRATEGY_ID, EVALUATOR_KEY,
                                     "price-action-engulfing-reversal-v1", RESEARCH_PARAMETER_VALUES)


def parameter_set():
    return contract_parameter_set("price-action-engulfing-reversal-v1-experimental",
                                  STRATEGY_VERSION_ID, "price-action-engulfing-reversal-v1",
                                  RESEARCH_PARAMETER_VALUES)
