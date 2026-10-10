"""Orchestrator adapter for the Liquidity Live strategy engine.

Routes LiquidityLiveRuntime through the canonical signal orchestrator so that
every signal passes through the shared admission, tradeability, risk, routing,
and delivery pipeline — the same boundary every other strategy uses.

Responsibility boundary
-----------------------
This adapter owns exactly three things:
  1. Calling LiquidityLiveRuntime.collect_signals() to evaluate market data.
  2. Returning StrategySignal objects to the orchestrator for canonical publication.
  3. Persisting the initial OPEN outcome after the orchestrator confirms publication
     (via after_publish_hook).

It does NOT:
  - Publish signals itself.
  - Manage broker execution.
  - Own transaction boundaries (the orchestrator's conn.commit() covers that).

Codex dependency
----------------
The initial OPEN outcome creation (after_publish_hook calling
ensure_open_liquidity_outcome) is a temporary boundary until the Unified Outcome
Resolver's canonical persistence layer (migration 051 + outcome_resolver.py)
absorbs it.  When that lands, this hook can be removed.
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

        Creates the initial OPEN outcome row that outcome monitoring depends on.
        This is a temporary boundary until the Unified Outcome Resolver absorbs it.
        """
        from liquidity_live_runtime import ensure_open_liquidity_outcome
        try:
            ensure_open_liquidity_outcome(conn, signal_id)
        except Exception as exc:
            try:
                from observability.strategy_audit import audit as _audit
                _audit(
                    "after_publish_hook_failed",
                    runner="signal-orchestrator",
                    strategy_id=STRATEGY_ID,
                    signal_id=signal_id,
                    hook="ensure_open_liquidity_outcome",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            except Exception:
                pass
