"""Orchestrator adapter for the Liquidity Live strategy engine.

Routes LiquidityLiveRuntime through the canonical signal orchestrator so that
every signal passes through the shared admission, tradeability, risk, routing,
and delivery pipeline — the same boundary every other strategy uses.

Responsibility boundary
-----------------------
This adapter owns exactly two things:
  1. Calling LiquidityLiveRuntime.collect_signals() to evaluate market data.
  2. Returning StrategySignal objects to the orchestrator for canonical publication.

It does NOT:
  - Publish signals itself.
  - Manage broker execution.
  - Write strategy.entry_signal_outcomes. The Unified Outcome Resolver owns
    initial OPEN creation and all subsequent outcome progression.
  - Call monitor_open_liquidity_entries(), ensure_open_liquidity_outcomes(),
    or ensure_open_liquidity_outcome(). Those are standalone-service paths
    that must not execute in orchestrator mode.
  - Own transaction boundaries (the orchestrator's conn.commit() covers that).

Outcome contract
----------------
Each signal carries strategy_metadata.outcome_contract with version
"entry-outcome.v2", encoding STOP_FIRST collision ordering and BEFORE_PRICE
time-exit ordering per the Liquidity V1 frozen rule semantics.
See docs/UNIFIED_OUTCOME_RESOLVER_SIGNAL_CONTRACT.md for the full contract.

The Unified Outcome Resolver (outcome_resolver.py + migration 051) discovers
published signals, creates/adopts the initial OPEN row, and owns all terminal
outcome projection. This adapter must be silent on outcome writes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from orchestration.models import StrategySignal

STRATEGY_ID = "LIQUIDITY_DISPLACEMENT_SCALP_V1"

if TYPE_CHECKING:
    from liquidity_live_runtime import LiquidityLiveRuntime


class LiquidityLiveAdapter:
    """StrategyAdapter wrapping LiquidityLiveRuntime for orchestrator ingestion."""

    strategy_id = STRATEGY_ID

    def __init__(self, runtime: "LiquidityLiveRuntime") -> None:
        self._runtime = runtime

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[StrategySignal]:
        """Evaluate active memberships and return new signals.

        Does NOT publish.  The orchestrator publishes via CanonicalSignalPublisher.
        Setup state is persisted inside collect_signals() before this returns,
        so state advancement and signal emission are consistent even when the
        orchestrator fails to publish (the seen_signal_ids deduplication prevents
        re-emission on the next cycle).
        """
        return self._runtime.collect_signals(seen_signal_ids)

    def after_publish_hook(self, signal_id: str, conn: Any) -> None:
        """Called by the orchestrator after canonical publication succeeds.

        Emits an observability audit event only. Does NOT write to
        strategy.entry_signal_outcomes — the Unified Outcome Resolver owns
        OPEN row creation. This hook exists solely to provide a publication
        confirmation trace; it must remain a no-op on the canonical DB.
        """
        try:
            from observability.strategy_audit import audit as _audit
            _audit(
                "signal_published_to_orchestrator",
                runner="signal-orchestrator",
                strategy_id=STRATEGY_ID,
                signal_id=signal_id,
                outcome_writer="UNIFIED_OUTCOME_RESOLVER",
            )
        except Exception:
            pass
