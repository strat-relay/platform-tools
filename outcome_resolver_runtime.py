"""Platform-owned post-emission outcome resolver runtime.

The runtime discovers immutable canonical EntrySignals, reads completed candles,
advances one durable per-signal cursor, and delegates the only canonical outcome
mutation to :mod:`outcome_resolver`.  It never calls strategy engines, setup
evaluators, broker clients, or execution services.
"""
from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from outcome_resolver import (Candle, EvaluationContract, OutcomeResolution,
                              persist_outcome_row, resolve_candle_path)


class ResolverLeaseLost(RuntimeError):
    """Raised when a worker no longer owns the current fencing generation."""


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class ResolverSignal:
    signal_id: str
    strategy_id: str
    instrument: str
    direction: str
    entry: float
    stop: float
    target: float
    decision_time: datetime
    entry_type: str | None
    strategy_metadata: dict[str, Any]
    source_provenance: dict[str, Any]

    @classmethod
    def from_row(cls, row: tuple[Any, ...]) -> "ResolverSignal":
        (signal_id, strategy_id, instrument, direction, entry, stop, target,
         decision_time, entry_type, metadata, provenance) = row
        return cls(str(signal_id), str(strategy_id), str(instrument), str(direction),
                   float(entry), float(stop), float(target), _utc(decision_time),
                   str(entry_type) if entry_type else None, _json_object(metadata),
                   _json_object(provenance))


class ResolverRedisCandleStore:
    """Read completed candles from the canonical market-data cache."""

    def __init__(self, redis_client: Any, *, symbol_resolver: Callable[[ResolverSignal], str] | None = None):
        self.redis = redis_client
        self.symbol_resolver = symbol_resolver or self._default_symbol

    @staticmethod
    def _default_symbol(signal: ResolverSignal) -> str:
        provenance = signal.source_provenance
        return str(provenance.get("provider_symbol") or provenance.get("symbol") or signal.instrument)

    def candles(self, signal: ResolverSignal, timeframe_minutes: int) -> list[Candle]:
        symbol = self.symbol_resolver(signal)
        timeframe = f"M{timeframe_minutes}"
        raw = self.redis.get(f"md:bars:{symbol}:{timeframe}")
        if not raw:
            return []
        rows = json.loads(raw)
        result: list[Candle] = []
        for row in rows:
            opened = datetime.fromtimestamp(int(row["time"]), timezone.utc)
            result.append(Candle(opened, opened + timedelta(minutes=timeframe_minutes),
                                 float(row["high"]), float(row["low"]),
                                 float(row["close"]) if row.get("close") is not None else None,
                                 bid_open=_number(row.get("bid_open")), bid_high=_number(row.get("bid_high")),
                                 bid_low=_number(row.get("bid_low")), bid_close=_number(row.get("bid_close")),
                                 ask_open=_number(row.get("ask_open")), ask_high=_number(row.get("ask_high")),
                                 ask_low=_number(row.get("ask_low")), ask_close=_number(row.get("ask_close")),
                                 spread=_number(row.get("spread"))))
        return result


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


class OutcomeResolverRuntime:
    def __init__(self, *, conn: Any, candle_store: ResolverRedisCandleStore,
                 holder_id: str | None = None, lease_seconds: int = 45):
        self.conn = conn
        self.candle_store = candle_store
        self.holder_id = holder_id or f"resolver:{socket.gethostname()}:{os.getpid()}"
        self.lease_seconds = lease_seconds
        self.lease_generation: int | None = None

    def acquire_lease(self) -> bool:
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.outcome_resolver_lease
                (lease_name, holder_id, generation, expires_at)
                VALUES ('canonical-entry-outcome-resolver', %s, 1, now() + (%s * interval '1 second'))
                ON CONFLICT (lease_name) DO UPDATE SET
                    holder_id = EXCLUDED.holder_id,
                    generation = platform.outcome_resolver_lease.generation + 1,
                    acquired_at = now(), heartbeat_at = now(), expires_at = EXCLUDED.expires_at
                WHERE platform.outcome_resolver_lease.expires_at < now()
                   OR platform.outcome_resolver_lease.holder_id = EXCLUDED.holder_id
                RETURNING generation""", (self.holder_id, self.lease_seconds))
            row = cur.fetchone()
            acquired = row is not None
            if row is not None:
                self.lease_generation = int(row[0])
        if acquired:
            self.conn.commit()
        else:
            self.conn.rollback()
        return acquired

    def _assert_lease(self, cur: Any) -> None:
        if self.lease_generation is None:
            raise ResolverLeaseLost("resolver has not acquired a lease")
        cur.execute("""SELECT generation, holder_id
                          FROM platform.outcome_resolver_lease
                         WHERE lease_name = 'canonical-entry-outcome-resolver'
                           AND holder_id = %s
                           AND generation = %s
                           AND expires_at > now()""", (self.holder_id, self.lease_generation))
        if cur.fetchone() is None:
            raise ResolverLeaseLost("resolver lease generation is stale or expired")

    def _signals(self, limit: int) -> list[ResolverSignal]:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT s.signal_id, s.strategy_id, s.instrument, s.direction,
                                      s.entry_price, s.stop_price, s.target_price,
                                      s.decision_time, s.entry_type, s.strategy_metadata,
                                      s.source_provenance
                                 FROM strategy.entry_signals s
                            LEFT JOIN strategy.entry_signal_outcomes o USING (signal_id)
                                WHERE s.entry_price IS NOT NULL AND s.stop_price IS NOT NULL
                                  AND s.target_price IS NOT NULL
                                  AND (o.signal_id IS NULL OR o.status = 'OPEN')
                             ORDER BY s.decision_time, s.signal_id
                                LIMIT %s""", (limit,))
            return [ResolverSignal.from_row(row) for row in cur.fetchall()]

    def _persist_state(self, signal: ResolverSignal, contract: EvaluationContract,
                       result: OutcomeResolution, candles: list[Candle], *, attempt: int,
                       error: str | None = None) -> None:
        coverage_end = max((c.close_timestamp for c in candles), default=None)
        last_close = coverage_end
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.outcome_resolver_signal_state
                (signal_id, contract_version, activation_state, activated_at,
                 next_candle_open, last_candle_close, coverage_end, resolution_state,
                 resolution_method, evidence, attempt_count, last_error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (signal_id) DO UPDATE SET
                    contract_version = EXCLUDED.contract_version,
                    activation_state = EXCLUDED.activation_state,
                    activated_at = EXCLUDED.activated_at,
                    next_candle_open = EXCLUDED.next_candle_open,
                    last_candle_close = EXCLUDED.last_candle_close,
                    coverage_end = EXCLUDED.coverage_end,
                    resolution_state = EXCLUDED.resolution_state,
                    resolution_method = EXCLUDED.resolution_method,
                    evidence = EXCLUDED.evidence,
                    attempt_count = EXCLUDED.attempt_count,
                    last_error = EXCLUDED.last_error,
                    updated_at = now()""",
                       (signal.signal_id, contract.version,
                        "EXPIRED_UNFILLED" if result.evidence.get("event") == "EXPIRED_UNFILLED" else
                        ("ACTIVE" if result.status != "OPEN" else "PENDING"),
                        signal.decision_time if result.status != "OPEN" else None,
                        (coverage_end + timedelta(minutes=contract.timeframe_minutes)) if coverage_end else None,
                        last_close, coverage_end, result.resolution_state,
                        result.resolution_method, json.dumps(result.evidence), attempt, error))

    def _next_attempt(self, signal_id: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT attempt_count FROM platform.outcome_resolver_signal_state
                            WHERE signal_id = %s""", (signal_id,))
            row = cur.fetchone()
        return int(row[0] or 0) + 1 if row else 1

    def _cursor(self, signal_id: str) -> datetime | None:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT last_candle_close
                             FROM platform.outcome_resolver_signal_state
                            WHERE signal_id = %s""", (signal_id,))
            row = cur.fetchone()
        return _utc(row[0]) if row and row[0] else None

    def resolve_one(self, signal: ResolverSignal) -> OutcomeResolution:
        contract = EvaluationContract.from_signal({"strategy_id": signal.strategy_id,
                                                    "entry_type": signal.entry_type,
                                                    "strategy_metadata": signal.strategy_metadata})
        candles = self.candle_store.candles(signal, contract.timeframe_minutes)
        cursor = self._cursor(signal.signal_id)
        if cursor is not None:
            candles = [candle for candle in candles if _utc(candle.close_timestamp) > cursor]
            if not candles:
                return OutcomeResolution("OPEN", None, None, "INSUFFICIENT_DATA",
                                         "CANDLE_REPLAY_V2", {"reason": "NO_NEW_COMPLETED_CANDLES",
                                                               "cursor": cursor.isoformat()})
        result = resolve_candle_path(
            direction=signal.direction, entry=signal.entry, stop=signal.stop,
            target=signal.target, entry_timestamp=signal.decision_time, candles=candles,
            max_hold_minutes=contract.max_hold_minutes,
            timeframe_minutes=contract.timeframe_minutes,
            expiration_minutes=contract.expiration_minutes,
            activation=contract.activation, time_exit_price=contract.time_exit_price,
            price_basis=contract.price_basis,
        )
        try:
            with self.conn.cursor() as cur:
                self._assert_lease(cur)
                if result.resolution_state == "RESOLVED" and result.status != "OPEN":
                    persist_outcome_row(cur, signal_id=signal.signal_id,
                                        outcome_type="ENTRY_ONLY", status=result.status,
                                        realized_r=result.realized_r,
                                        exit_timestamp=result.exit_timestamp,
                                        source=signal.strategy_id,
                                        updated_at=datetime.now(timezone.utc),
                                        resolution_state=result.resolution_state,
                                        resolution_method=result.resolution_method,
                                        resolution_evidence=result.evidence,
                                        outcome_contract_version=contract.version,
                                        price_basis=result.price_basis,
                                        exit_price=result.exit_price,
                                        activation_price=result.activation_price,
                                        outcome_kind=result.outcome_kind,
                                        source_kind="STRATEGY_REPLAY",
                                        writer_id="unified-outcome-resolver")
            self._persist_state(signal, contract, result, candles,
                                attempt=self._next_attempt(signal.signal_id))
            with self.conn.cursor() as cur:
                self._assert_lease(cur)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return result

    def tick(self, *, limit: int = 100) -> dict[str, Any]:
        if not self.acquire_lease():
            return {"status": "LEASE_HELD", "holder_id": self.holder_id, "resolved": 0}
        counts = {"resolved": 0, "insufficient": 0, "ambiguous": 0, "rejected": 0}
        for signal in self._signals(limit):
            result = self.resolve_one(signal)
            key = {"RESOLVED": "resolved", "INSUFFICIENT_DATA": "insufficient",
                   "AMBIGUOUS_INTRABAR": "ambiguous", "REJECTED": "rejected"}.get(result.resolution_state, "insufficient")
            counts[key] += 1
        return {"status": "OK", "holder_id": self.holder_id, **counts}
