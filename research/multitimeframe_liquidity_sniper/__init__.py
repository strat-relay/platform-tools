"""Research-only multi-timeframe liquidity sniper-entry family.

This package is deliberately offline and paper/research-only.  It has no
bridge, runner, orchestration, execution, or broker imports.
"""

from .engine import (
    CONTROL_GROUPS,
    DEFAULT_PAIRS,
    M5_TRIGGER_FAMILIES,
    M15_PARAMETER_GRID,
    align_completed_timeframes,
    evaluate_controls,
    inspect_candidate,
    pip_size,
)
from .validation import bounded_replay, source_configuration_hash, validate_alignment

__all__ = [
    "CONTROL_GROUPS", "DEFAULT_PAIRS", "M5_TRIGGER_FAMILIES",
    "M15_PARAMETER_GRID", "align_completed_timeframes", "evaluate_controls",
    "inspect_candidate", "pip_size",
    "bounded_replay", "source_configuration_hash", "validate_alignment",
]
