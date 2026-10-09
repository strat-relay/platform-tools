from __future__ import annotations

import asyncio
from typing import Any

from .contracts import EventEnvelope
from .jetstream import JetStreamPublisher
from execution_v2.trace import elapsed_ms, emit as trace_emit


class OutboxRelay:
    """At-least-once outbox relay; database commit is independent of NATS.

    A crash after JetStream accepts a message but before PostgreSQL marks it
    published can redeliver the same event. ``Nats-Msg-Id`` and downstream
    inbox idempotency bound duplicate domain effects; network delivery is not
    claimed to be exactly once.
    """
    def __init__(self, conn: Any, publisher: JetStreamPublisher, *, owner: str = "signal-outbox-relay"):
        self.conn, self.publisher, self.owner = conn, publisher, owner

    async def publish_batch(self, *, limit: int = 100) -> dict[str, int]:
        with self.conn.cursor() as cur:
            # Prioritize the oldest pending event globally.  Ordering by aggregate_id first
            # lets a busy aggregate monopolize a batch and can leave a newly-created signal
            # waiting behind unrelated backlog long enough to fail the execution freshness
            # gate.  Aggregate/version remain deterministic tie-breakers for equal creation
            # timestamps; downstream consumers remain idempotent and enforce their own
            # aggregate lifecycle rules.
            cur.execute("""SELECT event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,occurred_at,correlation_id,causation_id,created_at
                FROM platform.outbox_events WHERE publish_status <> 'PUBLISHED' AND (leased_until IS NULL OR leased_until < now())
                ORDER BY created_at, aggregate_id, aggregate_version NULLS LAST, event_id FOR UPDATE SKIP LOCKED LIMIT %s""", (limit,))
            rows = cur.fetchall()
            ids = [row[0] for row in rows]
            if ids:
                cur.execute("UPDATE platform.outbox_events SET lease_owner=%s, leased_until=now()+interval '60 seconds' WHERE event_id = ANY(%s)", (self.owner, ids))
        self.conn.commit()
        result = {"published": 0, "failed": 0}
        for row in rows:
            # Some lightweight/fake database cursors used by migration and relay tests return
            # the historical ten-column shape without created_at.  Keep tracing additive and
            # preserve the event's occurred_at as the fallback age baseline for those callers.
            outbox_created_at = row[10] if len(row) > 10 else row[7]
            try:
                payload = row[6] if isinstance(row[6], dict) else {}
                trace_emit("OUTBOX_PUBLISH_ATTEMPT", event_id=row[0], signal_id=row[3] if row[2] == "signal" else None,
                           outbox_event_type=row[1], outbox_age_ms=elapsed_ms(outbox_created_at),
                           decision_time=payload.get("decision_time"),
                           signal_emitted_at=payload.get("signal_emitted_at"),
                           created_at=payload.get("created_at"), transport="DB_PRIMARY")
                # Keep validation and transport failures inside the row-level failure boundary.
                # One malformed/unsupported row must not terminate the relay and hide the
                # outbox identity needed to diagnose it.  Failed rows remain unpublished and
                # are retried after the lease is cleared; they are never silently dropped.
                envelope = EventEnvelope(row[0], row[1], row[2], row[3], row[4], row[7], row[6], row[8], row[9])
                await self.publisher.publish(envelope)
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE platform.outbox_events SET publish_status='PUBLISHED', published_at=now(), attempts=attempts+1, lease_owner=NULL, leased_until=NULL WHERE event_id=%s", (row[0],))
                self.conn.commit(); result["published"] += 1
                trace_emit("OUTBOX_PUBLISHED", event_id=row[0], signal_id=row[3] if row[2] == "signal" else None,
                           outbox_event_type=row[1], outbox_age_ms=elapsed_ms(outbox_created_at), outcome="PUBLISHED",
                           decision_time=payload.get("decision_time"),
                           signal_emitted_at=payload.get("signal_emitted_at"),
                           created_at=payload.get("created_at"), transport="DB_PRIMARY")
            except Exception as exc:
                error = f"outbox_id={row[0]} subject={row[1]} event_type={row[1]} error={exc}"
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE platform.outbox_events SET publish_status='FAILED', last_error=%s, attempts=attempts+1, lease_owner=NULL, leased_until=NULL WHERE event_id=%s", (error, row[0]))
                self.conn.commit(); result["failed"] += 1
                trace_emit("OUTBOX_FAILED", event_id=row[0], signal_id=row[3] if row[2] == "signal" else None,
                           outbox_event_type=row[1], outbox_age_ms=elapsed_ms(outbox_created_at), outcome="FAILED",
                           decision_time=payload.get("decision_time"),
                           signal_emitted_at=payload.get("signal_emitted_at"),
                           created_at=payload.get("created_at"), transport="DB_PRIMARY",
                           error=f"{type(exc).__name__}: {exc}")
        return result

    async def run_forever(self, *, stop: asyncio.Event, idle_seconds: float = 1.0,
                          batch_size: int = 100) -> None:
        """Relay pending rows without any dependency on signal-file ingestion."""
        while not stop.is_set():
            result = await self.publish_batch(limit=batch_size)
            if result["failed"] or not result["published"]:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_seconds)
                except TimeoutError:
                    pass
