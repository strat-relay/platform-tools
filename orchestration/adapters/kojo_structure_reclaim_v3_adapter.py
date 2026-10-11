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

from orchestration.symbols import canonical_to_broker_hint

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
        self.strategy_id = STRATEGY_ID
        self.instance_id = instance_id
        self.display_name = display_name
        self.instruments = instruments
        self._active_fingerprint: str | None = parameter_set_fingerprint
        self._configuration_revision: int = configuration_revision
        self._execution_mode: str = execution_mode
        self._execution_mode_revision: int = execution_mode_revision
        self._evaluator: Any | None = None
        self._initialized = False

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[Any]:
        """Evaluate new H1/M15 bars from Redis and return unseen entry signals.

        Called every orchestrator cycle (~15 s).  State is persisted to disk so
        the evaluator resumes streaming rather than replaying history each cycle.
        On the first cycle (no state file) all bars in Redis are processed; any
        signals emitted are filtered by seen_signal_ids so DB duplicates are safe.

        One evaluator instance is maintained per instrument.  State is persisted
        in separate files: kojo_v3_{instance_id}_{instrument}_evaluator.json so
        each instrument's bar history and setup state remain independent.
        """
        import json
        import os
        from datetime import datetime, timezone
        from pathlib import Path

        runtime_dir = Path(os.environ.get("TRADING_PLATFORM_RUNTIME_DIR", "/tmp"))
        state_dir = runtime_dir / "orchestration"
        state_dir.mkdir(parents=True, exist_ok=True)

        from strategy_backtest.kojo_structure_reclaim_v3 import (
            KojoStructureReclaimV3Evaluator,
            kojo_structure_reclaim_v3_default_parameter_set,
            VERSION, EVALUATOR_KEY,
            kojo_structure_reclaim_v3_parameter_schema,
        )
        from strategy_backtest.models import MarketEvent, EntrySignal, StrategyVersion

        parameter_set = kojo_structure_reclaim_v3_default_parameter_set()
        strategy_version = StrategyVersion(
            strategy_id=STRATEGY_ID,
            version=VERSION,
            evaluator_key=EVALUATOR_KEY,
            parameter_schema=kojo_structure_reclaim_v3_parameter_schema(),
            lifecycle="IMPLEMENTED",
        )
        self._active_fingerprint = parameter_set.fingerprint

        entry_signals: list[tuple[Any, str]] = []
        try:
            import redis as redis_module
            redis_url = os.environ.get("MARKET_DATA_REDIS_URL", "")
            r = redis_module.from_url(redis_url, decode_responses=True) if redis_url else None

            for canonical_instrument in self.instruments:
                safe_name = canonical_instrument.replace("/", "_").replace(":", "_")
                state_file = state_dir / f"kojo_v3_{self.instance_id}_{safe_name}_evaluator.json"

                persisted: dict[str, Any] = {}
                if state_file.exists():
                    try:
                        persisted = json.loads(state_file.read_text(encoding="utf-8"))
                    except Exception:
                        persisted = {}

                evaluator = KojoStructureReclaimV3Evaluator()
                evaluator.initialize(
                    strategy_version, parameter_set,
                    instance_id=self.instance_id,
                    configuration_revision=str(self._configuration_revision),
                    instrument=canonical_instrument,
                )
                if persisted:
                    evaluator.restore_state(persisted)

                h1_watermark = int(persisted.get("_h1_watermark", 0))
                m15_watermark = int(persisted.get("_m15_watermark", 0))

                if r is not None:
                    raw_h1 = json.loads(r.get(f"md:bars:{canonical_instrument}:H1") or "[]")
                    raw_m15 = json.loads(r.get(f"md:bars:{canonical_instrument}:M15") or "[]")
                    now_ts = int(datetime.now(timezone.utc).timestamp())
                    # Exclude any bar whose close time (open + duration) is still in the
                    # future — these are forming candles, not closed bars.  Treating a
                    # forming candle as completed is lookahead bias.
                    new_h1 = [b for b in raw_h1
                               if int(b["time"]) > h1_watermark
                               and int(b["time"]) + 3600 <= now_ts]
                    new_m15 = [b for b in raw_m15
                                if int(b["time"]) > m15_watermark
                                and int(b["time"]) + 900 <= now_ts]
                    # Interleave H1 and M15 bars in open-timestamp order; within the
                    # same second H1 precedes M15 (H1 provides the structural context).
                    events = sorted(
                        [(int(b["time"]), 0, "H1", b) for b in new_h1] +
                        [(int(b["time"]), 1, "M15", b) for b in new_m15],
                        key=lambda x: (x[0], x[1]),
                    )
                    for ts, _, tf, bar in events:
                        try:
                            dur = 3600 if tf == "H1" else 900
                            event = MarketEvent(
                                canonical_instrument=canonical_instrument,
                                timeframe=tf,
                                open_timestamp=ts,
                                close_timestamp=ts + dur,
                                open=float(bar["open"]),
                                high=float(bar["high"]),
                                low=float(bar["low"]),
                                close=float(bar["close"]),
                                completed=True,
                                source="redis_canonical_cache",
                            )
                            for output in evaluator.consume_market_event(event):
                                if isinstance(output, EntrySignal):
                                    entry_signals.append((output, canonical_instrument))
                        except Exception as bar_exc:
                            from observability.strategy_audit import audit as _audit
                            _audit(
                                "strategy_bar_processing_failed",
                                runner="signal-orchestrator",
                                strategy_id=STRATEGY_ID,
                                instance_id=self.instance_id,
                                canonical_instrument=canonical_instrument,
                                timeframe=tf,
                                open_timestamp=ts,
                                error=str(bar_exc),
                                error_type=type(bar_exc).__name__,
                            )
                    if new_h1:
                        h1_watermark = max(h1_watermark, max(int(b["time"]) for b in new_h1))
                    if new_m15:
                        m15_watermark = max(m15_watermark, max(int(b["time"]) for b in new_m15))

                try:
                    state = evaluator.snapshot_state()
                    state["_h1_watermark"] = h1_watermark
                    state["_m15_watermark"] = m15_watermark
                    state_file.write_text(
                        json.dumps(state, sort_keys=True, default=str), encoding="utf-8"
                    )
                except Exception as persist_exc:
                    from observability.strategy_audit import audit as _audit
                    _audit(
                        "strategy_state_persist_failed",
                        runner="signal-orchestrator",
                        strategy_id=STRATEGY_ID,
                        instance_id=self.instance_id,
                        canonical_instrument=canonical_instrument,
                        state_file=str(state_file),
                        error=str(persist_exc),
                        error_type=type(persist_exc).__name__,
                    )

                # Keep self._evaluator pointing at the last-processed instrument so
                # that the consume_market_event / initialize test path still works.
                self._evaluator = evaluator
                self._initialized = True

        except Exception as exc:
            from observability.strategy_audit import audit as _audit
            _audit(
                "strategy_scan_failed",
                runner="signal-orchestrator",
                strategy_id=STRATEGY_ID,
                instance_id=self.instance_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        from orchestration.models import StrategySignal
        now_iso = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        result: list[Any] = []
        for es, canonical_instrument in entry_signals:
            if es.signal_id in seen_signal_ids:
                continue
            risk = abs(es.entry_price - es.stop_price)
            reward = abs(es.target_price - es.entry_price)
            ts_iso = (
                datetime.fromtimestamp(es.decision_timestamp, tz=timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            result.append(StrategySignal(
                signal_id=es.signal_id,
                schema_version="strategy-signal-v1",
                strategy_id=STRATEGY_ID,
                strategy_version=VERSION,
                strategy_instance_id=self.instance_id,
                source_event_id=f"kojo-v3-m15-entry:{es.signal_id}",
                market_event_id=str(es.provenance.get("market_event_id") or ""),
                setup_id=str(es.provenance.get("setup_id") or ""),
                entry_opportunity_id=None,
                economic_position_id=None,
                created_at=now_iso,
                signal_timestamp=ts_iso,
                symbol=canonical_instrument,
                canonical_symbol=canonical_instrument,
                broker_symbol_hint=canonical_to_broker_hint(canonical_instrument),
                direction=es.direction,
                entry_type=es.order_type,
                entry_price=es.entry_price,
                stop_price=es.stop_price,
                target_price=es.target_price,
                risk_distance=risk,
                target_distance=reward,
                target_r=round(reward / risk, 3) if risk > 0 else None,
                timeframe="M15",
                lower_timeframe=None,
                higher_timeframes=("H1",),
                entry_mechanism=("M15_OPEN",),
                strategy_metadata={
                    "parameter_set_fingerprint": self._active_fingerprint,
                    "execution_mode": self._execution_mode,
                    "execution_mode_revision": self._execution_mode_revision,
                    "outcome_contract": {
                        "version": "entry-outcome.v2",
                        "timeframe_minutes": 15,
                        "activation": "SIGNAL_TIMESTAMP",
                        "max_hold_minutes": None,
                        "expiration_minutes": None,
                        "time_exit_price": "CLOSE",
                        "price_basis": "THEORETICAL_TOUCH",
                        "same_candle_priority": "AMBIGUOUS_INTRABAR",
                        "time_exit_priority": "AFTER_PRICE",
                    },
                },
                provenance={"provider_symbol": canonical_to_broker_hint(canonical_instrument),
                            **dict(es.provenance or {})},
                decision_time=ts_iso,
                signal_emitted_at=now_iso,
            ))
        return result

    def initialize(self, strategy_version: Any, parameter_set: Any) -> None:
        from strategy_backtest.kojo_structure_reclaim_v3 import (
            KojoStructureReclaimV3Evaluator, INSTRUMENT,
        )
        instrument = self.instruments[0] if self.instruments else INSTRUMENT
        self._evaluator = KojoStructureReclaimV3Evaluator()
        self._evaluator.initialize(
            strategy_version,
            parameter_set,
            instance_id=self.instance_id,
            configuration_revision=str(self._configuration_revision),
            instrument=instrument,
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
