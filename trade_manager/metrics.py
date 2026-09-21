"""Management research metrics."""
from __future__ import annotations


def mfe_surrender(mfe_r: float | None, current_r: float | None) -> dict[str, float | None]:
    if mfe_r is None or current_r is None or float(mfe_r) <= 0:
        return {"MFE_SURRENDER_R": None, "mfe_capture_ratio": None}
    return {"MFE_SURRENDER_R": max(0.0, float(mfe_r) - float(current_r)),
            "mfe_capture_ratio": float(current_r) / float(mfe_r)}
