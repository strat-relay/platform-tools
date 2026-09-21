"""Frozen research search space; not a production strategy configuration."""

from __future__ import annotations

DEFAULT_PAIRS = (
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
    "EURJPY", "GBPJPY", "EURGBP", "AUDJPY", "CADJPY", "CHFJPY", "GBPAUD",
    "GBPCAD",
)

M15_PARAMETER_GRID = {
    "liquidity_lookback": (3, 5, 8, 12, 20),
    "sweep_depth_atr": (0.0, 0.05, 0.10, 0.20, 0.30),
    "reclaim_delay": (0, 1, 2, 3, 5),
    "displacement_body_atr": (0.25, 0.40, 0.50, 0.65, 0.80, 1.00, 1.25),
    "bos_delay": (0, 1, 2, 3, 5),
    "structure_lookback": (3, 5, 8, 10, 15),
}

M5_TRIGGER_FAMILIES = (
    "MICRO_STRUCTURE_BREAK", "SHALLOW_RETRACEMENT", "HOLD_RECLAIM",
    "M5_LIQUIDITY_SWEEP_RECLAIM", "PSYCHOLOGICAL_LEVEL",
)

STOP_MODELS = ("M5_SWEEP_EXTREME", "M5_MICRO_SWING", "M5_DISPLACEMENT_ORIGIN", "M15_INVALIDATION")
STOP_BUFFERS_ATR = (0.0, 0.05, 0.10, 0.20)
ENTRY_WINDOWS_M5 = (1, 2, 3, 5, 8, 12)
TARGET_R = (0.75, 1.00, 1.25, 1.50, 2.00, 2.50, 3.00)
MAX_HOLD_MINUTES = (30, 60, 90, 120, 180, 240)
CONTROL_GROUPS = ("CONTROL_A_M5_ONLY", "CONTROL_B_M15", "CONTROL_C_M15_H1", "CONTROL_D_H4_H1_M15")

RESEARCH_MANIFEST = {
    "family": "MULTITIMEFRAME_LIQUIDITY_SNIPER_RESEARCH",
    "mode": "RESEARCH_ONLY",
    "execution_timeframe": "M5",
    "context_timeframes": ("H4", "H1", "M15"),
    "production_imports_allowed": False,
    "broker_writes": False,
    "selection_protocol": ("DISCOVERY", "SELECTION", "FINAL_UNTOUCHED_TEST", "WALK_FORWARD"),
    "primary_comparison": "same_M5_trigger_across_control_groups_A_to_D",
}
