"""Shared strategy-outcome contract and canonical persistence boundary.

Strategy engines may produce setup state and normalized signals.  They may not
write ``strategy.entry_signal_outcomes``.  During migration, legacy runners can
call the compatibility helpers below, but the SQL mutation lives only here so
the eventual resolver cutover has one auditable write boundary.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

OUTCOME_CONTRACT_VERSION = "entry-outcome.v2"
STRATEGY_REPLAY_SOURCE_KIND = "STRATEGY_REPLAY"
PRICE_BASES = frozenset({"THEORETICAL_TOUCH", "EXECUTABLE_BID_ASK"})
OUTCOME_KINDS = frozenset({"STRATEGY_THEORETICAL", "BROKER_REALIZED"})
RESOLUTION_STATES = frozenset({"RESOLVED", "INSUFFICIENT_DATA", "AMBIGUOUS_INTRABAR", "REJECTED"})
TERMINAL_STATUSES = frozenset({
    "TARGET_HIT", "STOPPED", "TIME_EXIT", "PROFIT_EXIT", "EXPIRED", "INVALIDATED",
})


@dataclass(frozen=True)
class Candle:
    """A completed candle with explicit UTC coverage."""

    open_timestamp: datetime
    close_timestamp: datetime
    high: float
    low: float
    close: float | None = None
    bid_open: float | None = None
    bid_high: float | None = None
    bid_low: float | None = None
    bid_close: float | None = None
    ask_open: float | None = None
    ask_high: float | None = None
    ask_low: float | None = None
    ask_close: float | None = None
    spread: float | None = None

    def __post_init__(self) -> None:
        if self.open_timestamp.tzinfo is None or self.close_timestamp.tzinfo is None:
            raise ValueError("candle timestamps must be timezone-aware")
        if self.close_timestamp <= self.open_timestamp:
            raise ValueError("candle close must be after open")
        if self.high < self.low:
            raise ValueError("candle high must be >= low")


@dataclass(frozen=True)
class OutcomeResolution:
    status: str
    realized_r: float | None
    exit_timestamp: datetime | None
    resolution_state: str
    resolution_method: str
    evidence: dict[str, Any]
    price_basis: str = "THEORETICAL_TOUCH"
    exit_price: float | None = None
    activation_price: float | None = None
    outcome_kind: str = "STRATEGY_THEORETICAL"


@dataclass(frozen=True)
class EvaluationContract:
    """Versioned post-emission semantics carried by a canonical signal.

    Strategy engines only emit the contract payload.  The resolver owns its
    interpretation, so adding a strategy never requires adding an outcome
    monitor.  ``SIGNAL_TIMESTAMP`` is the safe default for existing market
    observations; pending entries must opt into ``ENTRY_PRICE_TOUCH``.
    """

    version: str = OUTCOME_CONTRACT_VERSION
    timeframe_minutes: int = 15
    activation: str = "SIGNAL_TIMESTAMP"
    max_hold_minutes: int | None = None
    expiration_minutes: int | None = None
    time_exit_price: str = "CLOSE"
    price_semantics: str = "OHLC_UNPROVABLE_INTRABAR"
    price_basis: str = "THEORETICAL_TOUCH"

    @classmethod
    def from_signal(cls, signal: dict[str, Any]) -> "EvaluationContract":
        raw = (signal.get("strategy_metadata") or {}).get("outcome_contract") or {}
        if not isinstance(raw, dict):
            raw = {}
        strategy_id = str(signal.get("strategy_id") or "")
        # Legacy signals predate the nested contract.  These are compatibility
        # interpretations, not strategy monitors; newly emitted signals must
        # carry the explicit contract payload.
        legacy_timeframe = 5 if strategy_id == "LIQUIDITY_DISPLACEMENT_SCALP_V1" else 15
        legacy_hold = 120 if strategy_id == "LIQUIDITY_DISPLACEMENT_SCALP_V1" else None
        metadata = signal.get("strategy_metadata") or {}
        parameter_values = metadata.get("v2_parameter_values") if isinstance(metadata, dict) else {}
        if not isinstance(parameter_values, dict):
            parameter_values = {}
        return cls(
            version=str(raw.get("version") or OUTCOME_CONTRACT_VERSION),
            timeframe_minutes=int(raw.get("timeframe_minutes") or metadata.get("timeframe_minutes") or legacy_timeframe),
            activation=str(raw.get("activation") or ("ENTRY_PRICE_TOUCH" if str(signal.get("entry_type") or "").upper() in {"LIMIT", "RETRACE"} else "SIGNAL_TIMESTAMP")),
            max_hold_minutes=(int(raw["max_hold_minutes"]) if raw.get("max_hold_minutes") is not None else
                              int(metadata.get("max_hold_minutes") or parameter_values.get("max_hold_minutes") or legacy_hold)
                              if (metadata.get("max_hold_minutes") or parameter_values.get("max_hold_minutes") or legacy_hold) is not None else None),
            expiration_minutes=(int(raw["expiration_minutes"]) if raw.get("expiration_minutes") is not None else None),
            time_exit_price=str(raw.get("time_exit_price") or "CLOSE"),
            price_semantics=str(raw.get("price_semantics") or "OHLC_UNPROVABLE_INTRABAR"),
            price_basis=str(raw.get("price_basis") or "THEORETICAL_TOUCH"),
        )

    def __post_init__(self) -> None:
        if self.price_basis not in PRICE_BASES:
            raise ValueError(f"unsupported price basis: {self.price_basis}")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def resolve_candle_path(*, direction: str, entry: float, stop: float, target: float,
                        entry_timestamp: datetime, candles: Iterable[Candle],
                        max_hold_minutes: int | None = None,
                        timeframe_minutes: int = 15,
                        expiration_minutes: int | None = None,
                        activation: str = "SIGNAL_TIMESTAMP",
                        time_exit_price: str = "CLOSE",
                        price_basis: str = "THEORETICAL_TOUCH") -> OutcomeResolution:
    """Resolve the first provable terminal event from completed candles.

    If one candle contains both stop and target, intrabar ordering is unknown and
    the resolver deliberately leaves the outcome OPEN with an ambiguity state.
    A time exit is evaluated at the first completed candle at or after the
    deadline, using its close only; missing close data remains unresolved.
    """
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("direction must be LONG or SHORT")
    entry_timestamp = _utc(entry_timestamp)
    valid_geometry = (stop < entry < target) if direction == "LONG" else (target < entry < stop)
    if not valid_geometry:
        raise ValueError("invalid entry/stop/target geometry")
    if price_basis not in PRICE_BASES:
        raise ValueError(f"unsupported price basis: {price_basis}")
    executable = price_basis == "EXECUTABLE_BID_ASK"
    ordered = sorted(candles, key=lambda candle: _utc(candle.open_timestamp))
    if timeframe_minutes <= 0:
        raise ValueError("timeframe_minutes must be positive")
    if activation not in {"SIGNAL_TIMESTAMP", "ENTRY_PRICE_TOUCH"}:
        raise ValueError("unsupported entry activation")
    deadline = None
    if max_hold_minutes is not None:
        deadline = entry_timestamp.timestamp() + int(max_hold_minutes) * 60
        deadline = datetime.fromtimestamp(deadline, tz=timezone.utc)
    expiration = None
    if expiration_minutes is not None:
        expiration = entry_timestamp + timedelta(minutes=int(expiration_minutes))
    activated_at = entry_timestamp if activation == "SIGNAL_TIMESTAMP" else None
    activation_price = entry if activated_at is not None else None
    activation_candle: Candle | None = None
    previous_end: datetime | None = None
    observed = 0
    for candle in ordered:
        start, end = _utc(candle.open_timestamp), _utc(candle.close_timestamp)
        if end <= entry_timestamp:
            continue
        if previous_end is not None and start > previous_end + timedelta(minutes=timeframe_minutes):
            return OutcomeResolution(
                "OPEN", None, None, "INSUFFICIENT_DATA", "CANDLE_REPLAY_V2",
                {"reason": "CANDLE_GAP", "gap_start": previous_end.isoformat(),
                 "gap_end": start.isoformat(), "observed_candles": observed},
            )
        previous_end = end
        observed += 1
        if executable:
            # A spread value or midpoint is not an executable quote.  The
            # resolver refuses to synthesize BID/ASK history when a provider
            # did not persist the relevant side of the market.
            required = ((candle.ask_high, candle.bid_high, candle.bid_low)
                        if direction == "LONG" else
                        (candle.bid_low, candle.ask_high, candle.ask_low))
            if any(value is None for value in required):
                return OutcomeResolution(
                    "OPEN", None, None, "INSUFFICIENT_DATA", "CANDLE_REPLAY_V2",
                    {"reason": "MISSING_EXECUTABLE_QUOTES", "price_basis": price_basis,
                     "candle_open": start.isoformat(), "required": "BID_ASK_EXTREMA"},
                    price_basis=price_basis,
                )
        if executable:
            activation_high = candle.ask_high if direction == "LONG" else candle.bid_high
            activation_low = candle.ask_low if direction == "LONG" else candle.bid_low
            exit_high = candle.bid_high if direction == "LONG" else candle.ask_high
            exit_low = candle.bid_low if direction == "LONG" else candle.ask_low
            close = candle.bid_close if direction == "LONG" else candle.ask_close
        else:
            activation_high, activation_low = candle.high, candle.low
            exit_high, exit_low, close = candle.high, candle.low, candle.close
        if activated_at is None:
            touched = activation_high >= entry if direction == "LONG" else activation_low <= entry
            if not touched:
                if expiration is not None and end >= expiration:
                    return OutcomeResolution("OPEN", None, None, "RESOLVED", "CANDLE_REPLAY_V2",
                                              {"event": "EXPIRED_UNFILLED", "candle_close": end.isoformat()})
                continue
            activated_at = end
            activation_price = float(entry)
            activation_candle = candle
        if activated_at is not None and end <= activated_at and candle is not activation_candle:
            continue
        if expiration is not None and start >= expiration and activated_at is None:
            return OutcomeResolution("OPEN", None, None, "RESOLVED", "CANDLE_REPLAY_V2",
                                      {"event": "EXPIRED_UNFILLED", "expiration": expiration.isoformat()})
        stop_hit = exit_low <= stop if direction == "LONG" else exit_high >= stop
        target_hit = exit_high >= target if direction == "LONG" else exit_low <= target
        if activation_candle is candle and (stop_hit or target_hit):
            return OutcomeResolution(
                "OPEN", None, None, "AMBIGUOUS_INTRABAR", "CANDLE_REPLAY_V2",
                {"candle_open": start.isoformat(), "candle_close": end.isoformat(),
                 "reason": "ENTRY_AND_EXIT_IN_SAME_CANDLE", "entry": entry,
                 "stop": stop, "target": target, "price_basis": price_basis},
                price_basis=price_basis, activation_price=activation_price,
            )
        if stop_hit and target_hit:
            return OutcomeResolution(
                "OPEN", None, None, "AMBIGUOUS_INTRABAR", "CANDLE_REPLAY_V2",
                {"candle_open": start.isoformat(), "candle_close": end.isoformat(),
                 "stop": stop, "target": target},
            )
        if stop_hit or target_hit:
            status = "STOPPED" if stop_hit else "TARGET_HIT"
            signed = stop - entry if stop_hit else target - entry
            if direction == "SHORT":
                signed = -signed
            risk = abs(entry - stop)
            event_time = end
            return OutcomeResolution(
                status, signed / risk if risk else None, event_time, "RESOLVED",
                "CANDLE_REPLAY_V2",
                {"candle_open": start.isoformat(), "candle_close": end.isoformat(),
                 "event": status, "price_basis": price_basis,
                 "price_source": "BID_ASK" if executable else "THEORETICAL_TOUCH"},
                price_basis=price_basis, exit_price=float(stop if stop_hit else target),
                activation_price=activation_price,
            )
        if deadline is not None and end >= deadline:
            if time_exit_price != "CLOSE":
                return OutcomeResolution("OPEN", None, None, "REJECTED", "CANDLE_REPLAY_V2",
                                          {"reason": "UNSUPPORTED_TIME_EXIT_PRICE", "requested": time_exit_price})
            exit_price = close
            if exit_price is None:
                return OutcomeResolution("OPEN", None, None, "INSUFFICIENT_DATA", "CANDLE_REPLAY_V2",
                                          {"reason": "TIME_EXIT_CLOSE_UNAVAILABLE", "candle_close": end.isoformat()})
            return OutcomeResolution(
                "TIME_EXIT", ((float(exit_price) - entry) / abs(entry - stop)) * (1 if direction == "LONG" else -1),
                end, "RESOLVED", "CANDLE_REPLAY_V2",
                {"candle_open": start.isoformat(), "candle_close": end.isoformat(),
                 "event": "TIME_EXIT", "price": float(exit_price),
                 "price_source": "BID_ASK_CLOSE" if executable else "CANDLE_CLOSE",
                 "price_basis": price_basis},
                price_basis=price_basis, exit_price=float(exit_price),
                activation_price=activation_price,
            )
    return OutcomeResolution(
        "OPEN", None, None, "INSUFFICIENT_DATA", "CANDLE_REPLAY_V2",
        {"observed_candles": observed, "coverage_end": (
            _utc(ordered[-1].close_timestamp).isoformat() if ordered else None)},
    )


def persist_outcome_row(cur: Any, *, signal_id: str, outcome_type: str, status: str,
                        realized_r: float | None, exit_timestamp: Any, source: str,
                        updated_at: Any, resolution_state: str | None = None,
                        resolution_method: str | None = None,
                        resolution_evidence: dict[str, Any] | None = None,
                        outcome_contract_version: str | None = None,
                        price_basis: str = "THEORETICAL_TOUCH",
                        exit_price: float | None = None,
                        activation_price: float | None = None,
                        outcome_kind: str = "STRATEGY_THEORETICAL",
                        source_kind: str = STRATEGY_REPLAY_SOURCE_KIND,
                        writer_id: str = "legacy-compat") -> bool:
    """The only canonical outcome INSERT/UPDATE SQL mutation in the platform."""
    if resolution_state is None and resolution_method is None and resolution_evidence is None and outcome_contract_version is None:
        if os.environ.get("OUTCOME_RESOLVER_ENFORCE_WRITER_GATE", "false").lower() == "true":
            cur.execute("""SELECT mode FROM platform.outcome_resolver_control
                            WHERE control_name = 'canonical-entry-outcome-writer'""")
            mode = cur.fetchone()
            if mode and mode[0] != "LEGACY_COMPAT":
                return False
        cur.execute(
            """INSERT INTO strategy.entry_signal_outcomes
                   (signal_id, outcome_type, status, realized_r, exit_timestamp, source)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (signal_id) DO UPDATE SET
                   status = EXCLUDED.status,
                   realized_r = EXCLUDED.realized_r,
                   exit_timestamp = EXCLUDED.exit_timestamp,
                   updated_at = %s
               WHERE strategy.entry_signal_outcomes.status = 'OPEN'
                 AND (strategy.entry_signal_outcomes.status,
                      strategy.entry_signal_outcomes.realized_r,
                      strategy.entry_signal_outcomes.exit_timestamp,
                      strategy.entry_signal_outcomes.outcome_type,
                      strategy.entry_signal_outcomes.source)
                     IS DISTINCT FROM
                     (EXCLUDED.status, EXCLUDED.realized_r, EXCLUDED.exit_timestamp,
                      EXCLUDED.outcome_type, EXCLUDED.source)
               RETURNING signal_id""",
            (signal_id, outcome_type, status, realized_r, exit_timestamp, source, updated_at),
        )
        return cur.fetchone() is not None
    resolution_state = resolution_state or "RESOLVED"
    outcome_contract_version = outcome_contract_version or OUTCOME_CONTRACT_VERSION
    cur.execute(
        """INSERT INTO strategy.entry_signal_outcomes
               (signal_id, outcome_type, status, realized_r, exit_timestamp, source,
                outcome_contract_version, source_kind, resolution_state, resolution_method, resolution_evidence,
                price_basis, exit_price, activation_price, outcome_kind)
           SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s
             WHERE EXISTS (
                 SELECT 1 FROM platform.outcome_resolver_control
                  WHERE control_name = 'canonical-entry-outcome-writer'
                    AND mode = %s)
           ON CONFLICT (signal_id) DO UPDATE SET
               status = EXCLUDED.status,
               realized_r = EXCLUDED.realized_r,
               exit_timestamp = EXCLUDED.exit_timestamp,
               outcome_contract_version = EXCLUDED.outcome_contract_version,
               source_kind = EXCLUDED.source_kind,
               resolution_state = EXCLUDED.resolution_state,
               resolution_method = EXCLUDED.resolution_method,
               resolution_evidence = EXCLUDED.resolution_evidence,
               price_basis = EXCLUDED.price_basis,
               exit_price = EXCLUDED.exit_price,
               activation_price = EXCLUDED.activation_price,
               outcome_kind = EXCLUDED.outcome_kind,
               updated_at = %s
           WHERE strategy.entry_signal_outcomes.status = 'OPEN'
             AND (strategy.entry_signal_outcomes.status,
                  strategy.entry_signal_outcomes.realized_r,
                  strategy.entry_signal_outcomes.exit_timestamp,
                  strategy.entry_signal_outcomes.outcome_type,
                  strategy.entry_signal_outcomes.source)
                 IS DISTINCT FROM
                 (EXCLUDED.status, EXCLUDED.realized_r, EXCLUDED.exit_timestamp,
                  EXCLUDED.outcome_type, EXCLUDED.source)
             AND EXISTS (
                 SELECT 1 FROM platform.outcome_resolver_control
                  WHERE control_name = 'canonical-entry-outcome-writer'
                    AND mode = %s)
           RETURNING signal_id""",
        (signal_id, outcome_type, status, realized_r, exit_timestamp, source,
         outcome_contract_version, source_kind, resolution_state, resolution_method,
         json.dumps(resolution_evidence or {}), price_basis, exit_price, activation_price, outcome_kind,
         "RESOLVER_PRIMARY" if writer_id == "unified-outcome-resolver" else "LEGACY_COMPAT",
         updated_at,
         "RESOLVER_PRIMARY" if writer_id == "unified-outcome-resolver" else "LEGACY_COMPAT"),
    )
    return cur.fetchone() is not None


def ensure_open_outcomes(conn: Any, *, strategy_id: str, outcome_type: str,
                         source: str, updated_at: Any) -> int:
    """Backfill OPEN rows through the same canonical writer used for terminals."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.signal_id FROM strategy.entry_signals s
               WHERE s.strategy_id = %s
                 AND NOT EXISTS (
                     SELECT 1 FROM strategy.entry_signal_outcomes o
                     WHERE o.signal_id = s.signal_id
                 )""",
            (strategy_id,),
        )
        signal_ids = [row[0] for row in cur.fetchall()]
        count = 0
        for signal_id in signal_ids:
            if persist_outcome_row(
                cur, signal_id=signal_id, outcome_type=outcome_type, status="OPEN",
                realized_r=None, exit_timestamp=None, source=source,
                updated_at=updated_at,
            ):
                count += 1
        return count


def persist_broker_attribution(cur: Any, *, signal_id: str, strategy_outcome: str,
                               strategy_realized_r: float | None, strategy_exit_timestamp: Any,
                               execution_outcome: str, broker_realized_r: float | None,
                               broker_fill_timestamp: Any, broker_exit_timestamp: Any,
                               broker_exit_reason: str, updated_at: Any) -> None:
    """Record broker truth separately without replacing the strategy outcome."""
    cur.execute(
        """UPDATE strategy.entry_signal_outcomes
              SET strategy_outcome = %s, strategy_realized_r = %s,
                  strategy_exit_timestamp = %s, execution_outcome = %s,
                  broker_realized_r = %s, broker_fill_timestamp = %s,
                  broker_exit_timestamp = %s, broker_exit_reason = %s,
                  attribution_status = 'BROKER_AUTHORITATIVE', attribution_error = NULL,
                  updated_at = %s WHERE signal_id = %s""",
        (strategy_outcome, strategy_realized_r, strategy_exit_timestamp,
         execution_outcome, broker_realized_r, broker_fill_timestamp,
         broker_exit_timestamp, broker_exit_reason, updated_at, signal_id),
    )


def economic_position_groups(signals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group reporting views without collapsing individual signal identity."""
    groups: dict[tuple[str, str], list[str]] = {}
    for signal in signals:
        if signal.get("economic_position_id"):
            key_value, key_kind = signal["economic_position_id"], "ECONOMIC_POSITION"
        elif signal.get("entry_opportunity_id"):
            key_value, key_kind = signal["entry_opportunity_id"], "ENTRY_OPPORTUNITY"
        else:
            # This is a reporting hint only.  It must never be used to merge
            # rows or infer a terminal outcome when the producer omitted the
            # authoritative economic-position identity.
            signature = tuple(signal.get(field) for field in
                              ("instrument", "direction", "entry_price", "stop_price",
                               "target_price", "decision_time"))
            key_value = json.dumps(signature, default=str)
            key_kind = "INFERRED_ECONOMIC_SIGNATURE" if all(value is not None for value in signature) else "INDEPENDENT_SIGNAL"
        groups.setdefault((key_kind, str(key_value)), []).append(str(signal["signal_id"]))
    return [{"group_type": kind, "group_id": group_id,
             "signal_ids": signal_ids, "signal_count": len(signal_ids),
             "independent_opportunities": (len(signal_ids) if kind == "INDEPENDENT_SIGNAL" else None),
             "identity_authoritative": kind in {"ECONOMIC_POSITION", "ENTRY_OPPORTUNITY"}}
            for (kind, group_id), signal_ids in groups.items()]
