"""The publication boundary: `TradeManagerDecision != ManagementSignal` (A6 12).

A decision is a trading/domain fact, produced and persisted for every observation regardless of
whether anyone will ever see it. Publication is a separate, later question, decided by rules
that only look at trading facts - never entitlement, subscribers, or account state (A6 12
section 2). This module is the **gate only**: it decides `PUBLISHED` vs `WITHHELD(reason)` and
is called by every Trade Manager version's decision-recording transaction (`tm_none.py` for
TM-NONE). It does **not** construct a `ManagementSignal`, publish `signal.management.published.v1`,
or implement any customer distribution - those remain future, explicitly out-of-scope work
(mission section 7: "do not implement customer distribution unless already required").
"""
from __future__ import annotations

from dataclasses import dataclass

PUBLISHED = "PUBLISHED"
WITHHELD = "WITHHELD"

REASON_NOT_ACTIONABLE = "NOT_ACTIONABLE_HOLD"
REASON_ENTRY_NOT_PUBLISHED = "ENTRY_NOT_PUBLISHED"
REASON_VERSION_NOT_PUBLISHABLE = "TM_VERSION_NOT_PUBLISHABLE"


@dataclass(frozen=True)
class GateInputs:
    """Trading facts only (A6 12 section 2) - deliberately has no field for an account,
    subscription, or entitlement."""
    action: str
    entry_signal_published: bool = False  # True once a PublishedSignal concept exists upstream
    tm_version_publication_eligibility: str = "SHADOW_ONLY"  # SHADOW_ONLY | PUBLISHABLE


def evaluate_publication_gate(inputs: GateInputs) -> tuple[str, str | None]:
    """Returns `(outcome, reason)`. `reason` is None only when `outcome == PUBLISHED`."""
    if inputs.action == "HOLD":
        return WITHHELD, REASON_NOT_ACTIONABLE
    if inputs.tm_version_publication_eligibility != "PUBLISHABLE":
        return WITHHELD, REASON_VERSION_NOT_PUBLISHABLE
    if not inputs.entry_signal_published:
        return WITHHELD, REASON_ENTRY_NOT_PUBLISHED
    return PUBLISHED, None
