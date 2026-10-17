"""Canonical orchestrator adapter for accepted Price Action research entries.

The frozen evaluators remain unchanged.  Only an explicit
``ENTRY_OBSERVED_RESEARCH_ONLY`` evaluation is promoted to a research signal;
proposals, cancellations, and rejected candidates stay in the research ledger.
The orchestrator publishes the signal and the Unified Outcome Resolver owns
the theoretical outcome lifecycle.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

from orchestration.models import StrategySignal, stable_id

SR_RETEST_STRATEGY_ID = "PRICE_ACTION_SUPPORT_RESISTANCE_RETEST"
SR_RETEST_INSTANCE_ID = "7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0104"
SR_RETEST_STRATEGY_KEY = "sr_retest"

ENGULFING_STRATEGY_ID = "PRICE_ACTION_ENGULFING_CONTINUATION"
ENGULFING_INSTANCE_ID = "7f1d6e5a-5ab3-4b59-8d2f-0c6f9e3d0204"
ENGULFING_STRATEGY_KEY = "engulfing_continuation"

ACCEPTED_REASON = "ENTRY_OBSERVED_RESEARCH_ONLY"
# Outcome contract max hold — no built-in exit policy in the frozen V1 evaluators, so
# the resolver uses this bound to time-exit unresolved signals rather than leaving them open.
MAX_HOLD_MINUTES = 1440

# Legacy environment parsing is retained for configuration compatibility. This
# value cannot promote a reconstructed entry to prospective evidence. New
# observed-open entries use the shared bounded entry-timing contract instead.
_PA_MAX_AGE_RAW = os.environ.get("PA_SIGNAL_MAX_AGE_S", "86400").strip()
try:
    PA_SIGNAL_MAX_AGE_S: float | None = float(_PA_MAX_AGE_RAW) if _PA_MAX_AGE_RAW else None
except (ValueError, TypeError) as _pa_age_exc:
    raise ValueError(
        f"PA_SIGNAL_MAX_AGE_S must be a numeric value in seconds (got {_PA_MAX_AGE_RAW!r}). "
        "Fix the environment variable or remove it to restore the 86400s default."
    ) from _pa_age_exc


def _iso_epoch(value: Any) -> str:
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


class PriceActionObserverAdapter:
    strategy_version = "V1"

    def __init__(self, conn: Any, *,
                 strategy_id: str = SR_RETEST_STRATEGY_ID,
                 instance_id: str = SR_RETEST_INSTANCE_ID,
                 strategy_key: str = SR_RETEST_STRATEGY_KEY,
                 market_input_policy: str | None = None,
                 activation_time: int | None = None,
                 parameter_set_id: str | None = None,
                 parameter_hash: str | None = None,
                 active_instruments: tuple[str, ...] = ()) -> None:
        self.conn = conn
        self.strategy_id = strategy_id
        self.instance_id = instance_id
        self.strategy_key = strategy_key
        self.market_input_policy = market_input_policy
        self.activation_time = activation_time
        self.parameter_set_id = parameter_set_id
        self.parameter_hash = parameter_hash
        self.active_instruments = active_instruments

    def discover_new_signals(self, seen_signal_ids: set[str]) -> list[StrategySignal]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT event_id, instrument, payload, observed_at
                     FROM research.price_action_observer_evaluation
                    WHERE instance_id=%s
                      AND strategy_key=%s
                      AND payload->'event'->'provenance'->>'rejection_reason'=%s
                    ORDER BY observed_at, event_id""",
                (self.instance_id, self.strategy_key, ACCEPTED_REASON),
            )
            rows = cur.fetchall()
        now_wall = time.time()
        result: list[StrategySignal] = []
        for event_id, instrument, payload, observed_at in rows:
            event = payload.get("event") or {}
            provenance = event.get("provenance") or {}
            if self.market_input_policy:
                from research.price_action_metadata import tick_size
                if instrument not in self.active_instruments:
                    raise ValueError("corrected observation outside registered instrument scope")
                if provenance.get("market_input_policy") != self.market_input_policy:
                    raise ValueError("corrected instance contains incompatible market input evidence")
                tick_size(provenance.get("provider_tick_size"))
                if provenance.get("parameter_hash") != self.parameter_hash:
                    raise ValueError("corrected instance parameter fingerprint mismatch")
                if int(provenance["information_cutoff"]) < self.activation_time:
                    continue
            if provenance.get("entry_cancelled") is True:
                continue
            signal_id = stable_id("SIG", {"strategy_id": self.strategy_id, "strategy_version": self.strategy_version,
                                           "strategy_instance_id": self.instance_id, "source_event_id": event_id})
            if signal_id in seen_signal_ids:
                continue
            entry = float(provenance["entry_open"])
            stop = float(provenance["stop"])
            target = float(provenance["target"])
            direction = str(provenance["direction"])
            decision_time = _iso_epoch(provenance["information_cutoff"])
            emitted_at = observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            risk = abs(entry - stop)
            reward = abs(target - entry)

            # Age gate: classify based on how long ago the information_cutoff was.
            decision_epoch = int(provenance["information_cutoff"])
            signal_age_s = max(0.0, now_wall - decision_epoch)
            # Completed-bar observation is reconstructed regardless of delivery age.
            signal_emission_class = "RETROSPECTIVE_RESEARCH_EVIDENCE"
            classification = "RETROSPECTIVE_RESEARCH_EVIDENCE"
            entry_timing = provenance.get("entry_timing") or {}
            live_open = entry_timing.get("entry_classification") == "CONTEMPORANEOUS_CANDLE_OPEN"
            if live_open:
                signal_emission_class = "PROSPECTIVE"
                classification = "PROSPECTIVE_ORCHESTRATOR_SIGNAL"

            result.append(StrategySignal(
                signal_id=signal_id, schema_version="strategy-signal-v1",
                strategy_id=self.strategy_id, strategy_version=self.strategy_version,
                strategy_instance_id=self.instance_id,
                source_event_id=f"price-action-observer:{event_id}", market_event_id=None,
                setup_id=event.get("candidate_id"), entry_opportunity_id=event.get("candidate_id"),
                economic_position_id=None, created_at=emitted_at,
                signal_timestamp=decision_time, symbol=str(instrument),
                canonical_symbol=str(instrument), broker_symbol_hint=f"{instrument}m",
                direction=direction, entry_type="MARKET_PAPER_OBSERVATION",
                entry_price=entry, stop_price=stop, target_price=target,
                risk_distance=risk, target_distance=reward,
                target_r=(reward / risk) if risk else None, timeframe="M15",
                lower_timeframe=None, higher_timeframes=("H1",),
                entry_mechanism=("RESEARCH_ENTRY_OBSERVED",),
                strategy_metadata={
                    "research_only": True, "execution_eligible": False,
                    **({"market_input_policy": self.market_input_policy,
                        "provider_tick_size": provenance["provider_tick_size"],
                        "tick_size_source": provenance.get("tick_size_source"),
                        "parameter_set_id": self.parameter_set_id,
                        "publication_activation_time": self.activation_time} if self.market_input_policy else {}),
                    "signal_emission_class": signal_emission_class,
                    "signal_age_s": round(signal_age_s, 1),
                    "research_evidence_class": ("CONTEMPORANEOUS_CANDLE_OPEN" if live_open else "FORWARD_COMPLETED_CANDLE_RECONSTRUCTION"),
                    **({"entry_timing": entry_timing} if live_open else {}),
                    "parameter_hash": provenance.get("parameter_hash"),
                    "data_snapshot_id": provenance.get("data_snapshot_id"),
                    "outcome_contract": {
                        "version": "entry-outcome.v2", "timeframe_minutes": 15,
                        "activation": "SIGNAL_TIMESTAMP", "time_exit_price": "CLOSE",
                        "price_basis": "THEORETICAL_TOUCH",
                        "same_candle_priority": "AMBIGUOUS_INTRABAR",
                        "time_exit_priority": "AFTER_PRICE",
                        "max_hold_minutes": MAX_HOLD_MINUTES,
                    },
                },
                provenance={
                    "source_process": "research.price_action_forward_runtime",
                    "source_strategy_fingerprint": provenance.get("parameter_hash"),
                    "source_market_data_timestamp": _iso_epoch(decision_epoch if live_open else decision_epoch + 900),
                    "source_data_age": max(0.0, observed_at.timestamp() - decision_epoch - (0 if live_open else 900)),
                    "source_read_health": True, "gap_recovery": False,
                    "classification": classification,
                    "signal_emission_class": signal_emission_class,
                    "orchestrator_freeze_timestamp": decision_time,
                    # Broker symbol for resolver candle lookup (m-suffix Exness convention).
                    "provider_symbol": f"{instrument}m",
                    **({"market_input_policy": self.market_input_policy,
                        "provider_tick_size": provenance["provider_tick_size"]} if self.market_input_policy else {}),
                }, decision_time=decision_time, signal_emitted_at=emitted_at,
            ))
        return result

    def after_publish_hook(self, signal_id: str, conn: Any) -> None:
        from observability.strategy_audit import audit
        audit("signal_published_to_orchestrator", runner="signal-orchestrator",
              strategy_id=self.strategy_id, signal_id=signal_id,
              outcome_writer="UNIFIED_OUTCOME_RESOLVER", research_only=True)


def adapters_for_strategy(conn, config, strategy_id):
    """Discover explicitly bound corrected instances; retain legacy drain adapter."""
    from research.price_action_metadata import POLICY
    from strategy_backtest.price_action_sr_retest import PARAMETER_HASH as SR_HASH
    from strategy_backtest.price_action_engulfing_continuation import PARAMETER_HASH as EG_HASH
    if strategy_id == SR_RETEST_STRATEGY_ID:
        key, legacy, expected = SR_RETEST_STRATEGY_KEY, SR_RETEST_INSTANCE_ID, SR_HASH
    elif strategy_id == ENGULFING_STRATEGY_ID:
        key, legacy, expected = ENGULFING_STRATEGY_KEY, ENGULFING_INSTANCE_ID, EG_HASH
    else:
        raise ValueError('unknown PA strategy')
    result = [PriceActionObserverAdapter(conn, strategy_id=strategy_id, instance_id=legacy, strategy_key=key)]
    seen = {legacy}
    for instance in config.get('instances', []):
        if (instance.get('strategy_id') != strategy_id or not instance.get('enabled') or
                instance.get('market_input_policy') != POLICY or not instance.get('signal_publication')):
            continue
        identity = instance.get('instance_id')
        if identity in seen:
            if identity == legacy:
                raise ValueError('corrected PA observation requires a new managed instance')
            continue
        activation = instance.get('publication_activation_time')
        if (not isinstance(identity, str) or not identity or type(activation) is not int or activation <= 0 or
                not instance.get('parameter_set_id') or not instance.get('active_instruments') or
                instance.get('parameter_set_fingerprint') != expected or
                instance.get('execution_mode') != 'OFF' or instance.get('broker_writes') is not False):
            raise ValueError('corrected PA publication requires explicit research identity, fingerprint and activation')
        result.append(PriceActionObserverAdapter(conn, strategy_id=strategy_id, instance_id=identity,
            strategy_key=key, market_input_policy=POLICY, activation_time=activation,
            parameter_set_id=instance['parameter_set_id'], parameter_hash=expected,
            active_instruments=tuple(instance['active_instruments'])))
        seen.add(identity)
    return result
