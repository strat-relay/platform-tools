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

    def archive_candles(self, signal: ResolverSignal, timeframe_minutes: int,
                        *, since: datetime, until: datetime, conn: Any) -> list[Candle]:
        """Fetch completed bars from market_data.candle_archive for the period (since, until).

        Returns an empty list on any error so callers always degrade to Redis-only
        rather than raising. Full path replay queries the archive whenever
        the entry predates the oldest Redis bar (also when Redis is empty).
        """
        symbol = self.symbol_resolver(signal)
        timeframe = f"M{timeframe_minutes}"
        # candle_time is the bar OPEN (unix seconds). Fetch any bar whose
        # close_timestamp > since: close = open + timeframe, so open > since - timeframe.
        since_open_ts = int(since.timestamp()) - timeframe_minutes * 60
        until_open_ts = int(until.timestamp())
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT candle_time, high_price, low_price, close_price
                      FROM market_data.candle_archive
                     WHERE provider_symbol = %s AND timeframe = %s
                       AND candle_time > %s AND candle_time < %s
                     ORDER BY candle_time ASC
                """, (symbol, timeframe, since_open_ts, until_open_ts))
                rows = cur.fetchall()
        except Exception:
            return []
        result: list[Candle] = []
        for (candle_time, high, low, close) in rows:
            opened = datetime.fromtimestamp(int(candle_time), timezone.utc)
            result.append(Candle(opened, opened + timedelta(minutes=timeframe_minutes),
                                 float(high), float(low),
                                 float(close) if close is not None else None))
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

    @staticmethod
    def _authority_lock(cur: Any) -> None:
        """Serialize lease changes with operator control-mode transitions."""
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("canonical-entry-outcome-writer",))

    def acquire_lease(self) -> bool:
        with self.conn.cursor() as cur:
            self._authority_lock(cur)
            expected_generation = self.lease_generation
            cur.execute("""WITH authority AS (
                    SELECT generation
                      FROM platform.outcome_resolver_control
                     WHERE control_name = 'canonical-entry-outcome-writer'
                       AND mode = 'RESOLVER_PRIMARY'
                     FOR UPDATE
                ), eligible AS (
                    SELECT authority.generation
                      FROM authority
                 LEFT JOIN platform.outcome_resolver_lease current_lease
                        ON current_lease.lease_name = 'canonical-entry-outcome-resolver'
                     WHERE (
                         (%s::bigint IS NULL)
                         OR (current_lease.holder_id = %s
                             AND current_lease.generation = %s
                             AND current_lease.generation = authority.generation)
                     )
                       AND (
                         %s::bigint IS NOT NULL
                         OR current_lease.lease_name IS NULL
                         OR current_lease.expires_at < now()
                         OR current_lease.generation <> authority.generation
                         OR current_lease.holder_id = %s
                     )
                )
                INSERT INTO platform.outcome_resolver_lease
                    (lease_name, holder_id, generation, expires_at)
                SELECT 'canonical-entry-outcome-resolver', %s, generation,
                       now() + (%s * interval '1 second')
                  FROM eligible
                ON CONFLICT (lease_name) DO UPDATE SET
                    holder_id = EXCLUDED.holder_id,
                    generation = EXCLUDED.generation,
                    acquired_at = now(), heartbeat_at = now(), expires_at = EXCLUDED.expires_at
                WHERE platform.outcome_resolver_lease.generation <> EXCLUDED.generation
                   OR (platform.outcome_resolver_lease.holder_id = EXCLUDED.holder_id
                       AND platform.outcome_resolver_lease.generation = EXCLUDED.generation)
                   OR (platform.outcome_resolver_lease.expires_at < now()
                       AND platform.outcome_resolver_lease.generation = EXCLUDED.generation)
                RETURNING generation""", (expected_generation, self.holder_id,
                                             expected_generation, expected_generation,
                                             self.holder_id, self.holder_id,
                                             self.lease_seconds))
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
        cur.execute("""SELECT lease.generation, lease.holder_id
                          FROM platform.outcome_resolver_lease lease
                          JOIN platform.outcome_resolver_control control
                            ON control.control_name = 'canonical-entry-outcome-writer'
                           AND control.generation = lease.generation
                         WHERE lease.lease_name = 'canonical-entry-outcome-resolver'
                           AND lease.holder_id = %s
                           AND lease.generation = %s
                           AND lease.expires_at > now()
                           AND control.mode = 'RESOLVER_PRIMARY'""",
                    (self.holder_id, self.lease_generation))
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
                            LEFT JOIN platform.outcome_resolver_signal_state work USING (signal_id)
                                WHERE s.entry_price IS NOT NULL AND s.stop_price IS NOT NULL
                                  AND s.target_price IS NOT NULL
                                  AND (o.signal_id IS NULL OR o.status = 'OPEN')
                             -- Oldest attempt first; unseen signals use event time.
                             -- Persisted attempts move blocked history behind waiting work.
                             ORDER BY COALESCE(work.updated_at, s.decision_time),
                                      s.decision_time, s.signal_id
                                LIMIT %s""", (limit,))
            return [ResolverSignal.from_row(row) for row in cur.fetchall()]

    def _persist_state(self, signal: ResolverSignal, contract: EvaluationContract,
                       result: OutcomeResolution, candles: list[Candle], *, attempt: int,
                       prior_cursor: datetime | None = None,
                       error: str | None = None) -> None:
        # Record evaluated coverage, not an instruction to resume a stateless
        # resolver on a suffix. Gap failures explicitly discard unsafe prior
        # cursors and pass only the path before the missing interval.
        coverage_end = max((c.close_timestamp for c in candles), default=prior_cursor)
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

    def _prior_resolution(self, signal_id: str) -> tuple[str | None, dict[str, Any]]:
        """Return the durable unresolved classification before replaying a cursor.

        A restart can legitimately find no candles newer than the durable cursor.
        In that case the resolver must preserve a previously proven ambiguity or
        unfilled expiration instead of downgrading its evidence to generic
        INSUFFICIENT_DATA.
        """
        with self.conn.cursor() as cur:
            cur.execute("""SELECT resolution_state, evidence
                             FROM platform.outcome_resolver_signal_state
                            WHERE signal_id = %s""", (signal_id,))
            row = cur.fetchone()
        if not row:
            return None, {}
        evidence = row[1] if isinstance(row[1], dict) else {}
        return row[0], evidence

    def resolve_one(self, signal: ResolverSignal) -> OutcomeResolution:
        contract = EvaluationContract.from_signal({"strategy_id": signal.strategy_id,
                                                    "entry_type": signal.entry_type,
                                                    "strategy_metadata": signal.strategy_metadata})
        candles = self.candle_store.candles(signal, contract.timeframe_minutes)
        cursor = self._cursor(signal.signal_id)
        # resolve_candle_path starts from immutable entry geometry and has no
        # resumable activation/path state. A suffix after the cursor is NOT an
        # equivalent input: it can invent an initial gap or lose a limit fill.
        # Replay the full available entry path; the cursor is evidence only.
        oldest_open = min((_utc(c.open_timestamp) for c in candles),
                          default=datetime.now(timezone.utc))
        if oldest_open > signal.decision_time:
            archive = self.candle_store.archive_candles(
                signal, contract.timeframe_minutes,
                since=signal.decision_time, until=oldest_open, conn=self.conn,
            )
            seen_opens = {_utc(c.open_timestamp) for c in archive}
            candles = archive + [c for c in candles if _utc(c.open_timestamp) not in seen_opens]
        prior_state, prior_evidence = (self._prior_resolution(signal.signal_id)
                                       if cursor is not None else (None, {}))
        if prior_state == "AMBIGUOUS_INTRABAR" or (
                prior_state == "RESOLVED" and prior_evidence.get("event") == "EXPIRED_UNFILLED"):
            result = OutcomeResolution("OPEN", None, None, prior_state,
                                       "CANDLE_REPLAY_V2", prior_evidence)
        else:
            result = resolve_candle_path(
                direction=signal.direction, entry=signal.entry, stop=signal.stop,
                target=signal.target, entry_timestamp=signal.decision_time, candles=candles,
                max_hold_minutes=contract.max_hold_minutes,
                timeframe_minutes=contract.timeframe_minutes,
                expiration_minutes=contract.expiration_minutes,
                activation=contract.activation, time_exit_price=contract.time_exit_price,
                price_basis=contract.price_basis,
                same_candle_priority=contract.same_candle_priority,
                time_exit_priority=contract.time_exit_priority,
            )
        try:
            with self.conn.cursor() as cur:
                self._authority_lock(cur)
                self._assert_lease(cur)
                # The resolver owns the complete canonical lifecycle, including
                # the initial OPEN row.  This makes missing rows and legacy OPEN
                # rows converge through the same idempotent writer before a
                # terminal transition is attempted.
                persist_outcome_row(
                    cur, signal_id=signal.signal_id, outcome_type="ENTRY_ONLY",
                    status="OPEN", realized_r=None, exit_timestamp=None,
                    source=signal.strategy_id, updated_at=datetime.now(timezone.utc),
                    resolution_state=(result.resolution_state
                                      if result.status == "OPEN" else "INSUFFICIENT_DATA"),
                    resolution_method=result.resolution_method,
                    resolution_evidence=result.evidence,
                    outcome_contract_version=contract.version,
                    price_basis=contract.price_basis,
                    outcome_kind="STRATEGY_THEORETICAL",
                    source_kind="STRATEGY_REPLAY",
                    writer_id="unified-outcome-resolver",
                )
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
            # On CANDLE_GAP the resolver returned before processing all passed candles.
            # Only advance the cursor through bars that were actually evaluated:
            # gap_start marks the last processed candle's close (or entry_timestamp
            # when no bars were processed at all).  Passing only those bars prevents
            # the cursor from jumping past the gap and burying unprocessed history.
            state_candles = candles
            if result.evidence.get("reason") == "CANDLE_GAP":
                gap_start_str = result.evidence.get("gap_start")
                if gap_start_str:
                    gap_boundary = _utc(datetime.fromisoformat(gap_start_str))
                    state_candles = [c for c in candles if _utc(c.close_timestamp) <= gap_boundary]
                else:
                    state_candles = []
            self._persist_state(signal, contract, result, state_candles,
                                attempt=self._next_attempt(signal.signal_id),
                                prior_cursor=(None if result.evidence.get("reason") == "CANDLE_GAP" else cursor))
            with self.conn.cursor() as cur:
                self._assert_lease(cur)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return result

    def tick(self, *, limit: int = 100) -> dict[str, Any]:
        if not self.acquire_lease():
            with self.conn.cursor() as cur:
                cur.execute("""SELECT mode FROM platform.outcome_resolver_control
                                WHERE control_name = 'canonical-entry-outcome-writer'""")
                control = cur.fetchone()
            self.conn.rollback()
            status = "WRITER_NOT_PRIMARY" if not control or control[0] != "RESOLVER_PRIMARY" else "LEASE_HELD"
            return {"status": status, "holder_id": self.holder_id,
                    "mode": control[0] if control else None, "resolved": 0}
        counts = {"resolved": 0, "insufficient": 0, "ambiguous": 0, "rejected": 0}
        for signal in self._signals(limit):
            result = self.resolve_one(signal)
            key = {"RESOLVED": "resolved", "INSUFFICIENT_DATA": "insufficient",
                   "AMBIGUOUS_INTRABAR": "ambiguous", "REJECTED": "rejected"}.get(result.resolution_state, "insufficient")
            counts[key] += 1
        return {"status": "OK", "holder_id": self.holder_id, **counts}
