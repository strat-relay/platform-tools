"""Minimal runtime bridge adapter for KOJO_STRUCTURE_RECLAIM_V1.

This adapter makes a pipeline-created strategy_instance_v2 observable inside the
signal orchestrator.  It is dormant while the instance is OFFLINE (online=false).

When online, this adapter:
  - Reads market events from the broker shadow feed (READ_ONLY_BRIDGE_TOOLS)
  - Runs the KojoStructureReclaimEvaluator in streaming mode
  - Emits signals to the orchestration store

EXECUTION_ELIGIBLE NEVER changes here.  The only state written is signals +
lifecycle events in the orchestration store.  BROKER_WRITES = 0.

NOTE: As of initial creation, all KOJO_STRUCTURE_RECLAIM_V1 instances start with
online=false.  This adapter will not be instantiated until an operator explicitly
toggles the instance online through the strategy management pipeline.
"""
from __future__ import annotations

from typing import Any


STRATEGY_ID = "KOJO_STRUCTURE_RECLAIM_V1"
EVALUATOR_KEY = "kojo_structure_reclaim"


class KojoStructureReclaimAdapter:
    """Minimal orchestrator adapter for KOJO_STRUCTURE_RECLAIM_V1.

    Wraps KojoStructureReclaimEvaluator for live/shadow market event consumption.
    Mirrors the interface expected by signal_orchestrator.load_adapters().

    This adapter is intentionally minimal — it exposes the evaluator contract
    to the orchestrator without adding execution, risk, or sizing logic.
    Those concerns belong to the existing execution authority system.
    """

    STRATEGY_ID = STRATEGY_ID
    EVALUATOR_KEY = EVALUATOR_KEY

    def __init__(self, instance_id: str, display_name: str, instruments: list[str]) -> None:
        self.strategy_id = STRATEGY_ID  # A-2: lowercase attr required by StrategyAdapter protocol
        self.instance_id = instance_id
        self.display_name = display_name
        self.instruments = instruments
        self._evaluator: Any | None = None
        self._initialized = False

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[Any]:
        """A-1: KOJO V1 implements an event-push interface (consume_market_event),
        not the polling interface expected by the orchestrator.  This stub satisfies
        the StrategyAdapter protocol so loading a V1 instance does not crash the
        orchestrator cycle.  It emits one audit warning per cycle while an instance
        is online so the operator knows V1 requires a streaming integration design
        before it can produce real signals.
        """
        try:
            from observability.strategy_audit import audit as _audit
            _audit(
                "strategy_scan_not_implemented",
                runner="signal-orchestrator",
                strategy_id=STRATEGY_ID,
                instance_id=self.instance_id,
                reason=(
                    "KOJO_V1 uses an event-push interface (consume_market_event) that is "
                    "architecturally incompatible with the orchestrator's polling model. "
                    "A streaming adapter integration is required before V1 can produce signals."
                ),
            )
        except Exception:
            pass
        return []

    def initialize(self, strategy_version: Any, parameter_set: Any) -> None:
        from strategy_backtest.kojo_structure_reclaim import KojoStructureReclaimEvaluator
        self._evaluator = KojoStructureReclaimEvaluator()
        self._evaluator.initialize(strategy_version, parameter_set)
        self._initialized = True

    def consume_market_event(self, event: Any) -> tuple:
        if not self._initialized or self._evaluator is None:
            return ()
        return self._evaluator.consume_market_event(event)

    def snapshot_state(self) -> dict[str, Any]:
        if self._evaluator is None:
            return {}
        return self._evaluator.snapshot_state()

    def restore_state(self, state: dict[str, Any]) -> None:
        if self._evaluator is not None:
            self._evaluator.restore_state(state)

    def diagnostics(self) -> dict[str, Any]:
        if self._evaluator is None:
            return {}
        return self._evaluator.diagnostics()

    @property
    def is_online(self) -> bool:
        """Adapter exists only when online; always True when instantiated."""
        return True
