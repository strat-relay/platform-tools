"""Runtime adapter for KOJO_STRUCTURE_RECLAIM_V3.

Wraps KojoStructureReclaimV3Evaluator for live/shadow forward evaluation.
Mirrors the interface used by signal_orchestrator.load_adapters().

EXECUTION_ELIGIBLE never changes here.  BROKER_WRITES = 0.
The adapter starts OFFLINE; an operator must toggle online=true on the
strategy_instance_v2 row to begin forward shadow evaluation.

Parameter set hot-reload:
  Each orchestration cycle the adapter is passed the current config dict
  via maybe_reload_parameter_set(config).  If the instance's
  parameter_set_fingerprint in the config differs from the one the
  evaluator was initialized with, the evaluator is reinitialized with the
  new ParameterSet.  In-flight evaluations keep the original snapshot;
  the next market event uses the new configuration.

  Reload does NOT reset accumulated streaming state (bar history, open
  setups, retirement keys).  A parameter change that requires a clean
  slate must be implemented by creating a new StrategyInstance, not by
  reinitializing the same one.
"""
from __future__ import annotations

from typing import Any


STRATEGY_ID = "KOJO_STRUCTURE_RECLAIM_V3"
EVALUATOR_KEY = "kojo_structure_reclaim_v3"


class KojoStructureReclaimV3Adapter:
    """Orchestrator adapter for KOJO_STRUCTURE_RECLAIM_V3.

    Accepts market events via consume_market_event() and emits
    EntrySignal / SetupLifecycleEvent outputs from the V3 evaluator.

    Configuration revision is incremented each time the active
    ParameterSet changes, providing a monotonic counter for
    signal provenance attribution.
    """

    STRATEGY_ID = STRATEGY_ID
    EVALUATOR_KEY = EVALUATOR_KEY

    def __init__(
        self,
        instance_id: str,
        display_name: str,
        instruments: list[str],
        parameter_set_fingerprint: str | None = None,
        configuration_revision: int = 1,
        execution_mode: str = "OFF",
        execution_mode_revision: int = 0,
    ) -> None:
        self.instance_id = instance_id
        self.display_name = display_name
        self.instruments = instruments
        self._active_fingerprint: str | None = parameter_set_fingerprint
        self._configuration_revision: int = configuration_revision
        self._execution_mode: str = execution_mode
        self._execution_mode_revision: int = execution_mode_revision
        self._evaluator: Any | None = None
        self._initialized = False

    def initialize(self, strategy_version: Any, parameter_set: Any) -> None:
        from strategy_backtest.kojo_structure_reclaim_v3 import KojoStructureReclaimV3Evaluator
        self._evaluator = KojoStructureReclaimV3Evaluator()
        self._evaluator.initialize(
            strategy_version,
            parameter_set,
            instance_id=self.instance_id,
            configuration_revision=str(self._configuration_revision),
        )
        self._active_fingerprint = parameter_set.fingerprint
        self._initialized = True

    def maybe_reload_parameter_set(
        self,
        config: dict[str, Any],
        *,
        strategy_version: Any = None,
        parameter_set_loader: Any = None,
    ) -> bool:
        """Check config for a parameter set or execution_mode change; reload if changed.

        Returns True if a reload occurred.

        Args:
          config: The current orchestration config dict (from refresh_lifecycle).
          strategy_version: Pre-loaded StrategyVersion; if None, loads from registry.
          parameter_set_loader: Callable(parameter_set_id) -> ParameterSet.
                                If None, uses the Python default parameter set factory.
        """
        if not self._initialized or self._evaluator is None:
            return False

        reloaded = False

        # Check execution_mode change (runtime, no evaluator reinit needed)
        new_mode, new_mode_rev = _execution_mode_for_instance(config, self.instance_id)
        if new_mode is not None and new_mode != self._execution_mode:
            self._execution_mode = new_mode
            self._execution_mode_revision = new_mode_rev or 0
            reloaded = True

        # Check parameter set change
        new_fingerprint = _fingerprint_for_instance(config, self.instance_id)
        if new_fingerprint is None or new_fingerprint == self._active_fingerprint:
            return reloaded

        if parameter_set_loader is None or strategy_version is None:
            return reloaded

        try:
            new_ps = parameter_set_loader(new_fingerprint)
        except Exception:
            return reloaded

        self._configuration_revision += 1
        self._evaluator.initialize(
            strategy_version,
            new_ps,
            instance_id=self.instance_id,
            configuration_revision=str(self._configuration_revision),
        )
        self._active_fingerprint = new_ps.fingerprint
        return True

    def consume_market_event(self, event: Any) -> tuple:
        if not self._initialized or self._evaluator is None:
            return ()
        outputs = self._evaluator.consume_market_event(event)
        # Stamp execution mode provenance onto each signal.
        # The mode captured here is the authoritative value at decision time.
        # Old SHADOW signals are never retroactively executable even if the
        # instance later transitions to LIVE; execution_mode_at_decision is immutable.
        return tuple(
            _stamp_execution_mode(o, self._execution_mode, self._execution_mode_revision)
            for o in outputs
        )

    def snapshot_state(self) -> dict[str, Any]:
        if self._evaluator is None:
            return {}
        return {
            **self._evaluator.snapshot_state(),
            "_configuration_revision": self._configuration_revision,
            "_active_fingerprint": self._active_fingerprint,
            "_execution_mode": self._execution_mode,
            "_execution_mode_revision": self._execution_mode_revision,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        if self._evaluator is not None:
            self._evaluator.restore_state(state)
        if "_configuration_revision" in state:
            self._configuration_revision = int(state["_configuration_revision"])
        if "_active_fingerprint" in state:
            self._active_fingerprint = state["_active_fingerprint"]
        if "_execution_mode" in state:
            self._execution_mode = str(state["_execution_mode"])
        if "_execution_mode_revision" in state:
            self._execution_mode_revision = int(state["_execution_mode_revision"])

    def diagnostics(self) -> dict[str, Any]:
        base = {} if self._evaluator is None else self._evaluator.diagnostics()
        return {
            **base,
            "instance_id": self.instance_id,
            "configuration_revision": self._configuration_revision,
            "active_parameter_set_fingerprint": self._active_fingerprint,
            "execution_mode": self._execution_mode,
            "execution_mode_revision": self._execution_mode_revision,
            "execution_eligible": False,
            "broker_writes": 0,
        }

    @property
    def is_online(self) -> bool:
        """Adapter exists only when online; always True when instantiated."""
        return True


def _fingerprint_for_instance(config: dict[str, Any], instance_id: str) -> str | None:
    """Look up the current parameter_set_fingerprint for this instance in the config."""
    for inst in config.get("instances", []):
        if inst.get("instance_id") == instance_id:
            return inst.get("parameter_set_fingerprint")
    return None


def _execution_mode_for_instance(
    config: dict[str, Any], instance_id: str
) -> tuple[str | None, int | None]:
    """Look up current execution_mode and execution_mode_revision for this instance."""
    for inst in config.get("instances", []):
        if inst.get("instance_id") == instance_id:
            return inst.get("execution_mode"), inst.get("execution_mode_revision")
    return None, None


def _stamp_execution_mode(output: Any, mode: str, revision: int) -> Any:
    """Stamp execution_mode_at_decision and execution_mode_revision onto a signal.

    Only modifies objects that carry a `provenance` dict.  Other outputs pass through.
    The stamp is immutable at emit time — old SHADOW signals cannot become executable
    merely because the instance later transitions to LIVE.
    """
    from strategy_backtest.models import EntrySignal, SetupLifecycleEvent
    if isinstance(output, (EntrySignal, SetupLifecycleEvent)):
        prov = dict(output.provenance or {})
        prov["execution_mode_at_decision"] = mode
        prov["execution_mode_revision"] = revision
        try:
            return output.__class__(**{**output.__dict__, "provenance": prov})
        except Exception:
            return output
    return output
