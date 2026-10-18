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

from datetime import datetime, timezone
from typing import Any

from orchestration.symbols import canonical_to_broker_hint

STRATEGY_ID = "KOJO_STRUCTURE_RECLAIM_V3"
EVALUATOR_KEY = "kojo_structure_reclaim_v3"


def _utc_now_ts() -> int:
    """Return the wall-clock timestamp used for candle and publication checks.

    Keeping this behind a small seam makes historical replay tests deterministic
    without changing the production clock source.
    """
    return int(datetime.now(timezone.utc).timestamp())


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

        Crash-consistency guarantee
        ---------------------------
        Entry signals are written to a pending-publications file BEFORE the
        evaluator checkpoint is advanced.  This ensures that a crash between
        "checkpoint written" and "signal published" cannot permanently lose the
        signal.  The pending file is cleaned up by after_publish_hook() once
        the orchestrator confirms canonical publication.  Signals surviving in
        the pending file are re-emitted on the next cycle with the same signal_id,
        which the idempotent canonical publisher (INSERT ON CONFLICT DO NOTHING)
        handles safely.

        Research evidence
        -----------------
        Signals whose emission_latency_s exceeds KOJO_V3_MAX_SIGNAL_AGE_S are
        published with signal_emission_class=RETROSPECTIVE_RESEARCH_EVIDENCE
        instead of being silently discarded.  This preserves historical setup
        outcomes for research queries while keeping them out of forward-performance
        metrics (filtered in strategy_entry_outcome_report).
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

        # Publication eligibility threshold derived from the M15 entry-bar contract:
        # the entry price is the open of the bar at decision_timestamp (duration 900s).
        # Once that bar closes, the entry price is no longer achievable.  Allow up to
        # 1200s (= 900s bar + ~20 × 15s orchestrator cycles) before suppressing.
        # Override with KOJO_V3_MAX_SIGNAL_AGE_S; set to 0 to disable the gate.
        _max_age_raw = os.environ.get("KOJO_V3_MAX_SIGNAL_AGE_S", "1200").strip()
        try:
            _max_signal_age_s: float | None = float(_max_age_raw) if _max_age_raw else None
        except (ValueError, TypeError) as _exc:
            raise ValueError(
                f"KOJO_V3_MAX_SIGNAL_AGE_S must be a numeric value in seconds (got {_max_age_raw!r}). "
                "Fix the environment variable or remove it to restore the 1200s default."
            ) from _exc

        # Pending-publications file: crash-safe bridge between evaluator checkpoint
        # and canonical signal publication.
        pending_file = state_dir / f"kojo_v3_{self.instance_id}_pending_publications.json"
        _pending_by_id: dict[str, dict] = {}
        if pending_file.exists():
            try:
                for rec in json.loads(pending_file.read_text(encoding="utf-8")):
                    if rec.get("signal_id") not in seen_signal_ids:
                        _pending_by_id[rec["signal_id"]] = rec
            except Exception:
                _pending_by_id = {}
        # Snapshot the pre-cycle pending IDs. Phase 1.5 may add current-cycle signals
        # to _pending_by_id for durability; Phase 2a must only re-emit pre-cycle signals
        # so current-cycle signals don't get emitted twice.
        _pre_cycle_pending_ids: frozenset[str] = frozenset(_pending_by_id.keys())

        # 3-tuple: (EntrySignal, canonical_instrument, entry_source)
        # entry_source distinguishes the emission path (BAR_CLOSE or LIVE_ENTRY).
        entry_signals: list[tuple[Any, str, str]] = []
        # Checkpoint snapshots are collected here and written AFTER the pending
        # file is updated so the pending file always precedes the checkpoint advance.
        _checkpoint_queue: list[tuple[Path, dict]] = []
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
                    except Exception as corrupt_exc:
                        from observability.strategy_audit import audit as _audit
                        _audit(
                            "kojo_checkpoint_corrupt",
                            runner="signal-orchestrator",
                            strategy_id=STRATEGY_ID,
                            instance_id=self.instance_id,
                            canonical_instrument=canonical_instrument,
                            state_file=str(state_file),
                            error=str(corrupt_exc),
                            error_type=type(corrupt_exc).__name__,
                        )
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
                    broker_symbol = canonical_to_broker_hint(canonical_instrument)
                    raw_h1 = json.loads(r.get(f"md:bars:{broker_symbol}:H1") or "[]")
                    raw_m15 = json.loads(r.get(f"md:bars:{broker_symbol}:M15") or "[]")
                    now_ts = _utc_now_ts()
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
                                    entry_signals.append((output, canonical_instrument, "BAR_CLOSE"))
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

                    from observability.strategy_audit import audit as _audit
                    _audit(
                        "kojo_market_replay",
                        runner="signal-orchestrator",
                        strategy_id=STRATEGY_ID,
                        instance_id=self.instance_id,
                        canonical_instrument=canonical_instrument,
                        new_h1_bars=len(new_h1),
                        new_m15_bars=len(new_m15),
                        emitted_signals=sum(1 for _, instrument, _src in entry_signals
                                            if instrument == canonical_instrument),
                        h1_watermark=h1_watermark,
                        m15_watermark=m15_watermark,
                        source="redis_canonical_cache",
                    )

                    # ── Live entry path ────────────────────────────────────────
                    # If any setup just entered CONFIRMED_PENDING_NEXT_OPEN, resolve
                    # it immediately using the current broker quote rather than waiting
                    # for the next completed M15 bar (~900 s).  No bar watermarks are
                    # advanced; the normal M15 path remains the fallback.
                    if evaluator.has_pending_live_entry_setups():
                        try:
                            from orchestration.brokers import MT5ShadowProvider
                            from strategy_backtest.models import LiveEntryPriceObservation
                            mcp_url = os.environ.get(
                                "MT5_BRIDGE_URL",
                                os.environ.get("MT5_BRIDGE_EXECUTION_URL", "http://10.10.10.100:22347/mcp"),
                            )
                            provider = MT5ShadowProvider(mcp_url=mcp_url)
                            quote = provider.quote(canonical_to_broker_hint(canonical_instrument))
                            bid = quote.get("bid")
                            ask = quote.get("ask")
                            if bid and ask and float(bid) > 0 and float(ask) >= float(bid):
                                live_obs = LiveEntryPriceObservation(
                                    canonical_instrument=canonical_instrument,
                                    bid=float(bid),
                                    ask=float(ask),
                                    observed_at=int(quote.get("timestamp") or now_ts),
                                )
                                for live_out in evaluator.consume_live_entry_price(live_obs):
                                    if isinstance(live_out, EntrySignal):
                                        entry_signals.append((live_out, canonical_instrument, "LIVE_ENTRY"))
                        except Exception as live_exc:
                            from observability.strategy_audit import audit as _audit
                            _audit(
                                "live_entry_quote_failed",
                                runner="signal-orchestrator",
                                strategy_id=STRATEGY_ID,
                                instance_id=self.instance_id,
                                canonical_instrument=canonical_instrument,
                                error=str(live_exc),
                                error_type=type(live_exc).__name__,
                            )

                try:
                    state = evaluator.snapshot_state()
                    state["_h1_watermark"] = h1_watermark
                    state["_m15_watermark"] = m15_watermark
                    # Queue the checkpoint for writing after the pending file is
                    # updated — see Phase 1.5 below for the ordering guarantee.
                    _checkpoint_queue.append((state_file, state))
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

        # Phase 1.5 — write the pending-publications file BEFORE advancing any
        # evaluator checkpoints.  This ordering guarantees that a crash between
        # "pending written" and "checkpoint written" leaves the next cycle able to
        # re-evaluate (checkpoint not advanced) and produce the same signals (which
        # the pending file also carries, with dedup via seen_signal_ids).  A crash
        # after "checkpoint written" is the dangerous case: without the pending file
        # the signal would be lost because the evaluator would not re-produce it.
        _new_pending_ids = {r["signal_id"] for r in _pending_by_id.values()}
        for _es, _inst, _src in entry_signals:
            if _es.signal_id not in seen_signal_ids and _es.signal_id not in _new_pending_ids:
                _pending_by_id[_es.signal_id] = {
                    "signal_id": _es.signal_id,
                    "instrument": _inst,
                    "decision_timestamp": int(_es.decision_timestamp),
                    "entry_price": float(_es.entry_price),
                    "stop_price": float(_es.stop_price),
                    "target_price": float(_es.target_price),
                    "direction": str(_es.direction),
                    "order_type": str(_es.order_type),
                    "provenance": dict(_es.provenance or {}),
                }
        if _pending_by_id:
            try:
                tmp_p = pending_file.with_suffix(".tmp")
                tmp_p.write_text(
                    json.dumps(list(_pending_by_id.values()), sort_keys=True, default=str),
                    encoding="utf-8",
                )
                os.replace(tmp_p, pending_file)
            except Exception:
                pass
        elif pending_file.exists():
            try:
                pending_file.unlink()
            except Exception:
                pass

        # Phase 1.6 — now safe to advance evaluator checkpoints.
        for _ckpt_file, _ckpt_state in _checkpoint_queue:
            try:
                _tmp_ckpt = _ckpt_file.with_suffix(".tmp")
                _tmp_ckpt.write_text(
                    json.dumps(_ckpt_state, sort_keys=True, default=str), encoding="utf-8"
                )
                os.replace(_tmp_ckpt, _ckpt_file)
            except Exception as _ckpt_exc:
                from observability.strategy_audit import audit as _audit
                _audit(
                    "strategy_state_persist_failed",
                    runner="signal-orchestrator",
                    strategy_id=STRATEGY_ID,
                    instance_id=self.instance_id,
                    state_file=str(_ckpt_file),
                    error=str(_ckpt_exc),
                    error_type=type(_ckpt_exc).__name__,
                )

        from orchestration.models import StrategySignal
        now_iso = (
            datetime.now(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        now_ts_wall = _utc_now_ts()
        result: list[Any] = []

        # Phase 2a — re-emit unconfirmed pending signals from a PREVIOUS cycle.
        # These survive when a crash occurred after the evaluator checkpoint was
        # written but before canonical publication succeeded.  Only pre-cycle
        # pending entries are re-emitted here; current-cycle entries are emitted
        # by Phase 2b (they went through the evaluator this cycle).
        _seen_in_result: set[str] = set()
        for _rec in _pending_by_id.values():
            if _rec["signal_id"] not in _pre_cycle_pending_ids:
                continue  # current-cycle signal — Phase 2b will emit it
            if _rec["signal_id"] in seen_signal_ids:
                continue
            _r_ts = int(_rec["decision_timestamp"])
            _r_latency = now_ts_wall - _r_ts
            _r_class = "PROSPECTIVE" if _r_latency <= 1200 else "RETROSPECTIVE"
            if _max_signal_age_s is not None and _max_signal_age_s > 0 and _r_latency > _max_signal_age_s:
                _r_class = "RETROSPECTIVE_RESEARCH_EVIDENCE"
            _r_ts_iso = (
                datetime.fromtimestamp(_r_ts, tz=timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            _r_risk = abs(_rec["entry_price"] - _rec["stop_price"])
            _r_reward = abs(_rec["target_price"] - _rec["entry_price"])
            _r_prov = dict(_rec.get("provenance") or {})
            result.append(StrategySignal(
                signal_id=_rec["signal_id"],
                schema_version="strategy-signal-v1",
                strategy_id=STRATEGY_ID,
                strategy_version=VERSION,
                strategy_instance_id=self.instance_id,
                source_event_id=f"kojo-v3-m15-entry:{_rec['signal_id']}",
                market_event_id=str(_r_prov.get("market_event_id") or ""),
                setup_id=str(_r_prov.get("setup_id") or ""),
                entry_opportunity_id=None,
                economic_position_id=None,
                created_at=now_iso,
                signal_timestamp=_r_ts_iso,
                symbol=_rec["instrument"],
                canonical_symbol=_rec["instrument"],
                broker_symbol_hint=canonical_to_broker_hint(_rec["instrument"]),
                direction=_rec["direction"],
                entry_type=_rec["order_type"],
                entry_price=_rec["entry_price"],
                stop_price=_rec["stop_price"],
                target_price=_rec["target_price"],
                risk_distance=_r_risk,
                target_distance=_r_reward,
                target_r=round(_r_reward / _r_risk, 3) if _r_risk > 0 else None,
                timeframe="M15",
                lower_timeframe=None,
                higher_timeframes=("H1",),
                entry_mechanism=("M15_OPEN",),
                strategy_metadata={
                    "parameter_set_fingerprint": self._active_fingerprint,
                    "parameter_set_id": _r_prov.get("parameter_set_id"),
                    "execution_mode": self._execution_mode,
                    "execution_mode_revision": self._execution_mode_revision,
                    "signal_emission_class": _r_class,
                    "emission_latency_s": _r_latency,
                    "entry_source": "PENDING_RECOVERY",
                    "pending_recovery": True,
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
                provenance={
                    "provider_symbol": canonical_to_broker_hint(_rec["instrument"]),
                    "signal_emission_class": _r_class,
                    "emission_latency_s": _r_latency,
                    "entry_source": "PENDING_RECOVERY",
                    "pending_recovery": True,
                    **_r_prov,
                },
                decision_time=_r_ts_iso,
                signal_emitted_at=now_iso,
            ))
            _seen_in_result.add(_rec["signal_id"])

        # Phase 2b — assemble signals from the current evaluation cycle.
        for es, canonical_instrument, entry_source in entry_signals:
            if es.signal_id in seen_signal_ids or es.signal_id in _seen_in_result:
                continue
            risk = abs(es.entry_price - es.stop_price)
            reward = abs(es.target_price - es.entry_price)
            ts_iso = (
                datetime.fromtimestamp(es.decision_timestamp, tz=timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            # Forward cohort classification: signals emitted within one M15 bar duration
            # (900 s) + one orchestrator cycle (≤ 300 s) of their decision timestamp are
            # prospective LIVE signals.  All others are catch-up/replay emissions.
            emission_latency_s = now_ts_wall - int(es.decision_timestamp)
            signal_emission_class = "PROSPECTIVE" if emission_latency_s <= 1200 else "RETROSPECTIVE"
            # Stale gate: signals beyond the actionable threshold are published as
            # RETROSPECTIVE_RESEARCH_EVIDENCE rather than silently discarded.  This
            # preserves historical setup outcomes for research queries (the evaluator
            # checkpoint has already advanced past these bars so discarding would be
            # permanent).  strategy_entry_outcome_report() excludes them from forward
            # performance metrics via provenance filtering.
            if (_max_signal_age_s is not None and _max_signal_age_s > 0 and
                    emission_latency_s > _max_signal_age_s):
                signal_emission_class = "RETROSPECTIVE_RESEARCH_EVIDENCE"
                from observability.strategy_audit import audit as _audit
                from execution_v2.trace import emit as trace_emit
                _audit(
                    "kojo_signal_research_evidence",
                    runner="signal-orchestrator",
                    strategy_id=STRATEGY_ID,
                    instance_id=self.instance_id,
                    signal_id=es.signal_id,
                    canonical_instrument=canonical_instrument,
                    decision_timestamp=int(es.decision_timestamp),
                    entry_price=float(es.entry_price),
                    stop_price=float(es.stop_price),
                    target_price=float(es.target_price),
                    direction=es.direction,
                    emission_latency_s=emission_latency_s,
                    max_signal_age_s=_max_signal_age_s,
                    signal_emission_class=signal_emission_class,
                    reason="KOJO_V3_ENTRY_BAR_CLOSED",
                )
                trace_emit(
                    "SIGNAL_RESEARCH_EVIDENCE",
                    signal_id=es.signal_id,
                    strategy_id=STRATEGY_ID,
                    strategy_instance_id=self.instance_id,
                    instrument=canonical_instrument,
                    decision_timestamp=int(es.decision_timestamp),
                    emission_latency_s=emission_latency_s,
                    signal_emission_class=signal_emission_class,
                )
            # Causal timestamp chain:
            # decision_available_at = when the confirmation bar closed (= decision_timestamp)
            # entry_eligible_at     = when the next bar opened (= decision_timestamp, same
            #                         instant — the entry bar opens at confirmation close)
            # These allow downstream analytics to measure each stage of emission lag.
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
                    "parameter_set_id": (es.provenance or {}).get("parameter_set_id"),
                    "execution_mode": self._execution_mode,
                    "execution_mode_revision": self._execution_mode_revision,
                    "signal_emission_class": signal_emission_class,
                    "emission_latency_s": emission_latency_s,
                    "entry_source": entry_source,
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
                provenance={
                    "provider_symbol": canonical_to_broker_hint(canonical_instrument),
                    "signal_emission_class": signal_emission_class,
                    "emission_latency_s": emission_latency_s,
                    "entry_source": entry_source,
                    "decision_available_at": ts_iso,
                    "entry_eligible_at": ts_iso,
                    **dict(es.provenance or {}),
                },
                decision_time=ts_iso,
                signal_emitted_at=now_iso,
            ))
            _seen_in_result.add(es.signal_id)
        return result

    def after_publish_hook(self, signal_id: str, conn: Any) -> None:
        """Remove a confirmed-published signal from the pending-publications file.

        Called by the orchestrator after a successful canonical INSERT.  Until
        this is called the signal remains in the pending file so a subsequent
        crash-recovery cycle can re-emit it idempotently.
        """
        import json
        import os
        from pathlib import Path

        runtime_dir = Path(os.environ.get("TRADING_PLATFORM_RUNTIME_DIR", "/tmp"))
        pending_file = runtime_dir / "orchestration" / f"kojo_v3_{self.instance_id}_pending_publications.json"
        if not pending_file.exists():
            return
        try:
            remaining = [r for r in json.loads(pending_file.read_text(encoding="utf-8"))
                         if r.get("signal_id") != signal_id]
            if remaining:
                tmp = pending_file.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(remaining, sort_keys=True, default=str), encoding="utf-8"
                )
                os.replace(tmp, pending_file)
            else:
                pending_file.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            from observability.strategy_audit import audit as _audit
            _audit(
                "kojo_signal_publication_confirmed",
                runner="signal-orchestrator",
                strategy_id=STRATEGY_ID,
                instance_id=self.instance_id,
                signal_id=signal_id,
            )
        except Exception:
            pass

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
