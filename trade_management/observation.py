"""Trade Observation Service (A6 04 option C/F, A6 02, A6 08).

Observes open ManagedTrades through a `MarketDataProvider` only. No MT5 broker position, order,
execution result, the execution consumer, port 22348, `REAL_EXECUTION`, or Phase 7 dependency
anywhere in this module - verified by `tests/test_trade_management_isolation.py`.

Sequencing (A6 08 section 5): the producer assigns `observation_seq` *inside its own
transaction*, under a row lock on `managed_trade`, so the sequence is gapless by construction
for a single producer. Deduplication is content-addressed (`observation_id`), independent of
the sequence, so identical facts delivered twice never create a second observation or consume a
second sequence number - this is what makes recording restart/redelivery-safe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.strategies.evaluation import canonical_bytes
from postgres.db import transaction

from .ids import market_snapshot_id as _market_snapshot_id
from .ids import observation_id as _observation_id
from .market_data import BarWindow, MarketDataProvider, MarketQuote


@dataclass(frozen=True)
class ObservationResult:
    status: str  # RECORDED | DUPLICATE | MANAGED_TRADE_MISSING | TRADE_NOT_OPEN | WAITING_FOR_ENTRY
    observation_id: str | None
    observation_seq: int | None
    market_snapshot_id: str | None
    managed_trade_id: str
    tm_version_id: str | None = None


def _load_managed_trade_for_update(conn: Any, managed_trade_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT managed_trade_id, state, tm_version_id, last_observation_seq, instrument,
                             entry_signal_id, time_exit_minutes, time_exit_at
                      FROM trade_management.managed_trade WHERE managed_trade_id=%s FOR UPDATE""",
                    (managed_trade_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("managed_trade_id", "state", "tm_version_id", "last_observation_seq", "instrument",
            "entry_signal_id", "time_exit_minutes", "time_exit_at")
    return dict(zip(keys, row))


def record_observation(conn: Any, *, managed_trade_id: str, quote: MarketQuote,
                       bars: BarWindow | None, now_utc: datetime) -> ObservationResult:
    with transaction(conn):
        trade = _load_managed_trade_for_update(conn, managed_trade_id)
        if trade is None:
            return ObservationResult(status="MANAGED_TRADE_MISSING", observation_id=None,
                                     observation_seq=None, market_snapshot_id=None,
                                     managed_trade_id=managed_trade_id)

        obs_id = _observation_id(managed_trade_id=managed_trade_id, provider_id=quote.provider_id,
                                 feed_id=quote.feed_id, instrument=quote.instrument,
                                 source_timestamp=quote.source_timestamp, bid=quote.bid, ask=quote.ask)

        with conn.cursor() as cur:
            cur.execute("SELECT observation_seq FROM trade_management.trade_observation WHERE observation_id=%s",
                        (obs_id,))
            existing = cur.fetchone()
        if existing is not None:
            return ObservationResult(status="DUPLICATE", observation_id=obs_id, observation_seq=existing[0],
                                     market_snapshot_id=None, managed_trade_id=managed_trade_id,
                                     tm_version_id=trade["tm_version_id"])

        if trade["state"] != "OPEN":
            return ObservationResult(status="TRADE_NOT_OPEN", observation_id=obs_id, observation_seq=None,
                                     market_snapshot_id=None, managed_trade_id=managed_trade_id,
                                     tm_version_id=trade["tm_version_id"])

        # A time exit starts at the confirmed entry fill, never at signal creation. Signals
        # without a filled execution are not managed positions and cannot consume observations.
        if trade.get("time_exit_minutes") is not None:
            with conn.cursor() as cur:
                cur.execute("""SELECT r.confirmed_at
                              FROM execution_v2.execution_intent i
                              JOIN execution_v2.execution_result r
                                ON r.execution_intent_id = i.execution_intent_id
                             WHERE i.entry_signal_id = %s AND r.outcome = 'FILLED'
                             ORDER BY r.confirmed_at ASC LIMIT 1""", (trade["entry_signal_id"],))
                fill = cur.fetchone()
            if fill is None:
                return ObservationResult(status="WAITING_FOR_ENTRY", observation_id=None,
                                         observation_seq=None, market_snapshot_id=None,
                                         managed_trade_id=managed_trade_id,
                                         tm_version_id=trade["tm_version_id"])
            if trade.get("time_exit_at") is None:
                fill_at = fill[0]
                if not isinstance(fill_at, datetime):
                    fill_at = datetime.fromisoformat(str(fill_at).replace("Z", "+00:00"))
                if fill_at.tzinfo is None:
                    fill_at = fill_at.replace(tzinfo=timezone.utc)
                deadline = fill_at + timedelta(minutes=int(trade["time_exit_minutes"]))
                with conn.cursor() as cur:
                    cur.execute("UPDATE trade_management.managed_trade SET time_exit_at=%s WHERE managed_trade_id=%s",
                                (deadline, managed_trade_id))
                trade["time_exit_at"] = deadline

        snapshot_id = _market_snapshot_id(provider_id=quote.provider_id, feed_id=quote.feed_id,
                                          instrument=quote.instrument, source_timestamp=quote.source_timestamp,
                                          bid=quote.bid, ask=quote.ask)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO trade_management.market_snapshot
                (market_snapshot_id, provider_id, feed_id, instrument, source_timestamp, bid, ask,
                 spread, data_status, quote_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (market_snapshot_id) DO NOTHING""",
                       (snapshot_id, quote.provider_id, quote.feed_id, quote.instrument,
                        quote.source_timestamp, quote.bid, quote.ask, quote.spread,
                        quote.data_status, snapshot_id))

        seq = trade["last_observation_seq"] + 1
        observed_at = now_utc.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        bars_ref = bars.as_ref() if bars is not None else {}
        payload_for_hash = {"managed_trade_id": managed_trade_id, "observation_seq": seq,
                            "quote": {"bid": quote.bid, "ask": quote.ask, "source_timestamp": quote.source_timestamp},
                            "bars_ref": bars_ref}
        payload_hash = canonical_bytes(payload_for_hash).hex()[:64]

        with conn.cursor() as cur:
            cur.execute("""INSERT INTO trade_management.trade_observation
                (observation_id, managed_trade_id, observation_seq, market_snapshot_id, tm_version_id,
                 observed_at, effective_at, bars_ref, data_status, payload_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)""",
                       (obs_id, managed_trade_id, seq, snapshot_id, trade["tm_version_id"],
                        observed_at, observed_at, canonical_bytes(bars_ref).decode("utf-8"),
                        quote.data_status, payload_hash))
            cur.execute("""UPDATE trade_management.managed_trade SET last_observation_seq=%s
                          WHERE managed_trade_id=%s""", (seq, managed_trade_id))

        payload = {
            "schema": "trade-observation.v1", "observation_id": obs_id, "observation_seq": seq,
            "managed_trade_id": managed_trade_id, "trade_manager_version_id": trade["tm_version_id"],
            "instrument": quote.instrument, "observed_at": observed_at, "effective_at": observed_at,
            "market_snapshot_id": snapshot_id,
            "quote": {"bid": quote.bid, "ask": quote.ask, "spread": quote.spread,
                     "source_timestamp": quote.source_timestamp, "provider_id": quote.provider_id,
                     "feed_id": quote.feed_id, "price_semantics_version": quote.price_semantics_version},
            "bars_ref": bars_ref, "data_status": quote.data_status,
        }
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO platform.outbox_events
                (event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                 schema_version, payload, occurred_at, correlation_id, causation_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                ON CONFLICT (event_id) DO NOTHING""",
                       (obs_id, "trade.observation.recorded.v1", "managed_trade", managed_trade_id, seq,
                        "event-envelope.v1", canonical_bytes(payload).decode("utf-8"), observed_at,
                        managed_trade_id, snapshot_id))

        return ObservationResult(status="RECORDED", observation_id=obs_id, observation_seq=seq,
                                 market_snapshot_id=snapshot_id, managed_trade_id=managed_trade_id,
                                 tm_version_id=trade["tm_version_id"])


class TradeObservationService:
    """Polls one instrument's `MarketDataProvider` quote/bars and records an observation for
    every open ManagedTrade on that instrument. Not activated: nothing calls `poll_once` on a
    schedule from this module; a future runner (not built here) would own that loop."""

    def __init__(self, conn_factory: Any, provider: MarketDataProvider, *,
                clock: Any = lambda: datetime.now(timezone.utc)) -> None:
        self.conn_factory = conn_factory
        self.provider = provider
        self.clock = clock

    def poll_instrument(self, instrument: str, managed_trade_ids: list[str]) -> list[ObservationResult]:
        quote = self.provider.quote(instrument)
        bars = self.provider.bars(instrument)
        results = []
        for trade_id in managed_trade_ids:
            conn = self.conn_factory()
            results.append(record_observation(conn, managed_trade_id=trade_id, quote=quote,
                                              bars=bars, now_utc=self.clock()))
        return results
