"""The observation scheduler loop (mission section 3/6). Smallest possible scheduling: a
fixed-interval poll, one instrument-batch per tick, no priority queue, no adaptive backoff - the
chosen interval is configuration-driven (`RuntimeConfig.observation_interval_seconds`) and
recorded in `docs/engineering/OPTIMIZATION_REGISTER.md` for later tuning, not hidden as a
constant.

Each tick: discover open ManagedTrades (relationally, grouped by instrument so one quote/bar
fetch serves every trade on that instrument - the same "instrument-level snapshot, trade-scoped
derivation" shape A6 04 recommended), record an observation per trade via the existing
`trade_management.observation.record_observation` (unmodified domain logic - restart/redelivery
safety and per-trade gapless sequencing come from there, not from anything in this module), and
publish the event it produced. A publish failure never loses the observation - it stays
correctly recorded relationally and an unpublished outbox row, picked up by
`relay_pending_observations` on the next tick (or after a restart), so republishing after
recovery is a normal, expected, safe path, not a special case.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from infrastructure.messaging.contracts import EventEnvelope
from infrastructure.messaging.jetstream import JetStreamPublisher
from postgres.foundation import mark_outbox_published
from trade_management.market_data import MarketDataProvider
from trade_management.mode import OFF, current_mode
from trade_management.observation import ObservationResult, record_observation

OBSERVATION_EVENT_TYPE = "trade.observation.recorded.v1"


def load_open_managed_trades_by_instrument(conn: Any) -> dict[str, list[str]]:
    by_instrument: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT managed_trade_id, instrument FROM trade_management.managed_trade WHERE state = 'OPEN'")
        for managed_trade_id, instrument in cur.fetchall():
            by_instrument.setdefault(instrument, []).append(managed_trade_id)
    return by_instrument


def _load_outbox_row(conn: Any, event_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("""SELECT event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                             payload, occurred_at, correlation_id, causation_id
                      FROM platform.outbox_events WHERE event_id = %s""", (event_id,))
        row = cur.fetchone()
    if row is None:
        return None
    keys = ("event_id", "event_type", "aggregate_type", "aggregate_id", "aggregate_version",
            "payload", "occurred_at", "correlation_id", "causation_id")
    record = dict(zip(keys, row))
    # Real PostgreSQL/psycopg auto-decodes a jsonb column back into a dict; the in-process fake
    # substrate stores exactly what was bound (a JSON string), so this mirrors that same decode
    # step rather than special-casing the fake in callers.
    if isinstance(record["payload"], str):
        record["payload"] = json.loads(record["payload"])
    return record


async def _publish_outbox_row(publisher: JetStreamPublisher, row: dict[str, Any]) -> None:
    envelope = EventEnvelope(event_id=row["event_id"], event_type=row["event_type"],
                             aggregate_type=row["aggregate_type"], aggregate_id=row["aggregate_id"],
                             aggregate_version=row["aggregate_version"], occurred_at=row["occurred_at"],
                             payload=row["payload"], correlation_id=row["correlation_id"],
                             causation_id=row["causation_id"])
    await publisher.publish(envelope)


async def relay_pending_observations(conn: Any, publisher: JetStreamPublisher, *, limit: int = 200) -> int:
    """Catches up any `trade.observation.recorded.v1` outbox rows not yet published - a restart
    or a transient publish failure never loses an observation, it is just relayed later."""
    with conn.cursor() as cur:
        cur.execute("""SELECT event_id FROM platform.outbox_events
                      WHERE event_type = %s AND publish_status <> 'PUBLISHED'
                      ORDER BY created_at LIMIT %s""", (OBSERVATION_EVENT_TYPE, limit))
        pending_ids = [row[0] for row in cur.fetchall()]
    relayed = 0
    for event_id in pending_ids:
        row = _load_outbox_row(conn, event_id)
        if row is None:
            continue
        await _publish_outbox_row(publisher, row)
        mark_outbox_published(conn, event_id)
        conn.commit()
        relayed += 1
    return relayed


async def observe_instrument_once(conn: Any, publisher: JetStreamPublisher, provider: MarketDataProvider,
                                  *, instrument: str, managed_trade_ids: list[str],
                                  now_utc: datetime | None = None) -> list[ObservationResult]:
    """One tick's worth of work for one instrument: one quote/bar fetch, one observation per
    open trade on that instrument, publishing each as it is recorded."""
    now_utc = now_utc or datetime.now(timezone.utc)
    quote = provider.quote(instrument)
    bars = provider.bars(instrument)
    results = []
    for managed_trade_id in managed_trade_ids:
        result = record_observation(conn, managed_trade_id=managed_trade_id, quote=quote, bars=bars, now_utc=now_utc)
        results.append(result)
        if result.status == "RECORDED":
            row = _load_outbox_row(conn, result.observation_id)
            if row is not None:
                await _publish_outbox_row(publisher, row)
                mark_outbox_published(conn, result.observation_id)
                conn.commit()
    return results


async def observation_tick(conn: Any, publisher: JetStreamPublisher, provider: MarketDataProvider) -> dict[str, Any]:
    """One full scheduler tick: relay any backlog first, then observe every open trade grouped
    by instrument. Returns a small summary for logging/health, never raises for an individual
    instrument's provider failure - one instrument's market-data outage must not stop
    observations for every other open trade.

    Mode OFF (or an unreadable mode) skips the whole tick: nothing observed, nothing relayed."""
    try:
        mode = current_mode(conn)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - fail closed: no work without a readable mode
        conn.rollback()
        return {"mode": "UNAVAILABLE", "skipped": True, "error": str(exc)}
    if mode == OFF:
        return {"mode": OFF, "skipped": True}
    relayed = await relay_pending_observations(conn, publisher)
    by_instrument = load_open_managed_trades_by_instrument(conn)
    recorded, failed_instruments = 0, []
    for instrument, managed_trade_ids in by_instrument.items():
        try:
            results = await observe_instrument_once(conn, publisher, provider, instrument=instrument,
                                                     managed_trade_ids=managed_trade_ids)
            recorded += sum(1 for r in results if r.status == "RECORDED")
        except Exception as exc:  # noqa: BLE001 - one instrument's failure must not stop the tick
            failed_instruments.append({"instrument": instrument, "error": str(exc)})
    return {"mode": mode, "skipped": False, "open_trades": sum(len(v) for v in by_instrument.values()), "instruments": len(by_instrument),
            "observations_recorded": recorded, "backlog_relayed": relayed, "failed_instruments": failed_instruments}
