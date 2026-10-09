"""Create a ManagedTrade from a canonical EntrySignal (A6 06, A7 10 section "Behaviour").

`create_managed_trade` is the single creation transaction: claim inbox -> load the canonical
EntrySignal by `signal_id` -> verify identity -> compute eligibility -> resolve the
TradeManagerVersion binding exactly once -> insert `managed_trade` (idempotent) -> insert the
`trade.opened.v1` outbox row -> mark inbox processed -> commit. Every step happens inside the
same `postgres.db.transaction(conn)` block the rest of this codebase uses (`migration/signal.py`,
`postgres/foundation.py`); nothing here opens its own connection or reads a runtime/JSONL file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction
from postgres.foundation import claim_inbox, mark_inbox_processed

from .binding import BindingResolution, StreamBindingResolver, TmVersionUnavailable
from .ids import managed_trade_id as _managed_trade_id
from .lifecycle import close_terminal_trades
from .mode import OFF, SKIP_REASON_OFF, current_mode

OPEN_CONSUMER_NAME = "trade-mgmt-open"


class EntrySignalRecordMissing(RuntimeError):
    """The EntrySignal referenced by the event does not (yet) exist in
    `strategy.entry_signals`. Caller retries with backoff, then quarantines
    `ENTRY_SIGNAL_RECORD_MISSING` (A7 10) - never creates a ManagedTrade from the event
    payload alone."""


@dataclass(frozen=True)
class CreationResult:
    status: str  # CREATED | DUPLICATE | QUARANTINED | SKIPPED | INBOX_DUPLICATE
    managed_trade_id: str | None
    reason: str | None = None
    tm_version_id: str | None = None


def _load_entry_signal(conn: Any, signal_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT signal_id, strategy_id, strategy_version, strategy_ref, parameter_set_ref,
                             parameter_set_status, strategy_instance_id, instrument, direction,
                             decision_time, entry_price, stop_price, risk_distance, target_price,
                             strategy_metadata, publication_state, entry_signal_hash
                      FROM strategy.entry_signals WHERE signal_id=%s""", (signal_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("signal_id", "strategy_id", "strategy_version", "strategy_ref", "parameter_set_ref",
            "parameter_set_status", "strategy_instance_id", "instrument", "direction",
            "decision_time", "entry_price", "stop_price", "risk_distance", "target_price",
            "strategy_metadata", "publication_state", "entry_signal_hash")
    return dict(zip(keys, row))


def compute_eligibility(*, decision_time: str, now_utc: datetime,
                        max_creation_lag_seconds: float | None) -> tuple[str, str | None, float]:
    """`max_creation_lag_seconds=None` (the default) means the lag policy is unset:
    `eligibility = ELIGIBILITY_UNEVALUATED`, never a guessed threshold (A7 10 "Behaviour").
    When a threshold is configured and exceeded: `FORWARD_INELIGIBLE(LATE_CREATION)` - the
    trade is still created (A7 10 acceptance test 6), just not counted as FORWARD-clean."""
    decided = datetime.fromisoformat(decision_time.replace("Z", "+00:00"))
    if decided.tzinfo is None:
        decided = decided.replace(tzinfo=timezone.utc)
    lag = (now_utc - decided).total_seconds()
    if max_creation_lag_seconds is None:
        return "ELIGIBILITY_UNEVALUATED", None, lag
    if lag > max_creation_lag_seconds:
        return "FORWARD_INELIGIBLE", "LATE_CREATION", lag
    return "ELIGIBLE", None, lag


def _time_exit_minutes(metadata: Any) -> int | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("time_exit_minutes")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("time_exit_minutes in signal metadata must be a positive integer")
    return value


def _optional_positive(metadata: Any, key: str) -> float | None:
    if not isinstance(metadata, dict) or metadata.get(key) is None:
        return None
    value = metadata[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        raise ValueError(f"{key} in signal metadata must be null or a positive number")
    return float(value)


def _record_quarantine(conn: Any, *, entry_signal_id: str, expected_hash: str, actual_hash: str,
                       reason: str) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO trade_management.managed_trade_quarantine
                      (entry_signal_id, expected_entry_signal_hash, actual_entry_signal_hash, reason)
                      VALUES (%s,%s,%s,%s) ON CONFLICT (entry_signal_id) DO NOTHING""",
                    (entry_signal_id, expected_hash, actual_hash, reason))


def create_managed_trade(conn: Any, *, event_id: str, signal_id: str, resolver: StreamBindingResolver,
                         now_utc: datetime, consumer_name: str = OPEN_CONSUMER_NAME,
                         max_creation_lag_seconds: float | None = None,
                         claimed_entry_signal_hash: str | None = None) -> CreationResult:
    """`claimed_entry_signal_hash`, if given, is the hash carried by the `signal.entry.created.v1`
    event payload; if it disagrees with the row actually stored in `strategy.entry_signals`, that
    is treated the same as a post-creation hash mismatch (quarantine, never silently trusted)."""
    with transaction(conn):
        if not claim_inbox(conn, consumer_name, event_id):
            return CreationResult(status="INBOX_DUPLICATE", managed_trade_id=_managed_trade_id(signal_id))

        # Operator mode (migration 024). OFF: no ManagedTrade; the event is consumed and the skip
        # recorded. An unreadable mode raises, rolling back the inbox claim for redelivery.
        if current_mode(conn) == OFF:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO trade_management.managed_trade_skip (entry_signal_id, reason, detail)
                              VALUES (%s,%s,%s) ON CONFLICT (entry_signal_id) DO NOTHING""",
                            (signal_id, SKIP_REASON_OFF, f"event {event_id}"))
            mark_inbox_processed(conn, consumer_name, event_id)
            return CreationResult(status="SKIPPED", managed_trade_id=None, reason=SKIP_REASON_OFF)

        record = _load_entry_signal(conn, signal_id)
        if record is None:
            raise EntrySignalRecordMissing(signal_id)
        if claimed_entry_signal_hash is not None and claimed_entry_signal_hash != record["entry_signal_hash"]:
            _record_quarantine(conn, entry_signal_id=signal_id, expected_hash=record["entry_signal_hash"],
                              actual_hash=claimed_entry_signal_hash, reason="EVENT_HASH_MISMATCH")
            mark_inbox_processed(conn, consumer_name, event_id)
            return CreationResult(status="QUARANTINED", managed_trade_id=None, reason="EVENT_HASH_MISMATCH")

        eligibility, eligibility_reason, lag = compute_eligibility(
            decision_time=record["decision_time"].isoformat() if hasattr(record["decision_time"], "isoformat")
            else str(record["decision_time"]), now_utc=now_utc, max_creation_lag_seconds=max_creation_lag_seconds)

        trade_id = _managed_trade_id(signal_id)
        # Fail closed (A6 07 section 4 / A7 04 section 4): if resolution raises, the whole
        # transaction rolls back - including the inbox claim above - so this event is eligible
        # for redelivery once TM-NONE-1 (or the resolver's target) is actually registered. No
        # ManagedTrade is created and the inbox is never marked processed for a failed creation.
        binding = resolver.resolve(conn, strategy_id=record["strategy_id"],
                                   strategy_instance_id=record["strategy_instance_id"],
                                   instrument=record["instrument"],
                                   decision_time=str(record["decision_time"]))

        decision_time = record["decision_time"]
        entry_price, stop_price, target_price = record["entry_price"], record["stop_price"], record["target_price"]
        risk_distance = record["risk_distance"]
        if risk_distance is None and entry_price is not None and stop_price is not None:
            risk_distance = abs(float(entry_price) - float(stop_price))
        time_exit_minutes = _time_exit_minutes(record.get("strategy_metadata"))
        net_profit_target_usd = _optional_positive(record.get("strategy_metadata"), "net_profit_target_usd")
        profit_target_pips = _optional_positive(record.get("strategy_metadata"), "profit_target_pips")
        profit_target_r = _optional_positive(record.get("strategy_metadata"), "profit_target_r")
        pip_size = _optional_positive(record.get("strategy_metadata"), "pip_size")

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO trade_management.managed_trade
                (managed_trade_id, entry_signal_id, entry_signal_hash, strategy_id, strategy_version,
                 strategy_ref, parameter_set_ref, parameter_set_status, instrument, direction,
                 decision_time, reference_entry_price, initial_stop, initial_target, risk_distance,
                 time_exit_minutes, time_exit_at, net_profit_target_usd, profit_target_pips,
                 profit_target_r, pip_size,
                 tm_version_id, tm_binding_id, binding_hash, tm_bound_at, binding_resolution,
                 evidence_mode, eligibility, eligibility_reason, creation_lag_seconds,
                 record_mode, state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (entry_signal_id) DO NOTHING
                RETURNING managed_trade_id""",
                       (trade_id, signal_id, record["entry_signal_hash"], record["strategy_id"],
                        record["strategy_version"], record["strategy_ref"], record["parameter_set_ref"],
                        record["parameter_set_status"], record["instrument"], record["direction"],
                        decision_time, entry_price, stop_price, target_price, risk_distance,
                        time_exit_minutes, None, net_profit_target_usd, profit_target_pips,
                        profit_target_r, pip_size,
                        binding.tm_version_id, binding.binding_id, binding.binding_hash, now_utc,
                        binding.resolution, "FORWARD", eligibility, eligibility_reason, lag,
                        "SHADOW", "OPEN"))
            inserted = cur.fetchone()

        if inserted is None:
            # ON CONFLICT hit: another delivery already created this trade. Compare identity,
            # never overwrite (A7 10 "Duplicates").
            with conn.cursor() as cur:
                cur.execute("""SELECT entry_signal_hash FROM trade_management.managed_trade
                              WHERE entry_signal_id=%s""", (signal_id,))
                existing_hash = cur.fetchone()[0]
            mark_inbox_processed(conn, consumer_name, event_id)
            if existing_hash == record["entry_signal_hash"]:
                return CreationResult(status="DUPLICATE", managed_trade_id=trade_id,
                                      tm_version_id=binding.tm_version_id)
            _record_quarantine(conn, entry_signal_id=signal_id, expected_hash=existing_hash,
                              actual_hash=record["entry_signal_hash"], reason="ENTRY_SIGNAL_MISMATCH")
            return CreationResult(status="QUARANTINED", managed_trade_id=trade_id, reason="ENTRY_SIGNAL_MISMATCH")

        payload = {
            "managed_trade_id": trade_id, "entry_signal_id": signal_id,
            "entry_signal_hash": record["entry_signal_hash"], "strategy_ref": record["strategy_ref"],
            "instrument": record["instrument"], "direction": record["direction"],
            "decision_time": str(decision_time),
            "reference_entry_price": float(entry_price) if entry_price is not None else None,
            "initial_stop": float(stop_price) if stop_price is not None else None,
            "initial_target": float(target_price) if target_price is not None else None,
            "time_exit_minutes": time_exit_minutes,
            "net_profit_target_usd": net_profit_target_usd,
            "profit_target_pips": profit_target_pips,
            "profit_target_r": profit_target_r,
            "pip_size": pip_size,
            "tm_version_id": binding.tm_version_id, "tm_binding_id": binding.binding_id,
            "evidence_mode": "FORWARD", "eligibility": eligibility, "record_mode": "SHADOW",
        }
        occurred_at = now_utc.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.outbox_events
                (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                 schema_version, payload, occurred_at, correlation_id, causation_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING""",
                       (f"{trade_id}:trade.opened", "trade.opened.v1", "managed_trade", trade_id, 1,
                        "event-envelope.v1", canonical_bytes(payload).decode("utf-8"), occurred_at,
                        signal_id, event_id))

        # The strategy may already have a terminal outcome for this signal (exits can precede
        # signal emission). Close it now, in the same transaction, so it never enters the
        # observation work set.
        closed_on_creation = close_terminal_trades(conn, now_utc=now_utc, managed_trade_id=trade_id)

        mark_inbox_processed(conn, consumer_name, event_id)
        return CreationResult(status="CREATED", managed_trade_id=trade_id, tm_version_id=binding.tm_version_id,
                              reason="CLOSED_ON_CREATION_STRATEGY_OUTCOME" if closed_on_creation else None)
