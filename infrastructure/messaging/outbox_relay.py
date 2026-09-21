from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .contracts import EventEnvelope
from .jetstream import JetStreamPublisher


class OutboxRelay:
    """Small, dormant-by-default relay: ack first, then mark the row published."""
    def __init__(self, conn: Any, publisher: JetStreamPublisher, *, owner: str = "signal-outbox-relay"):
        self.conn, self.publisher, self.owner = conn, publisher, owner

    async def publish_batch(self, *, limit: int = 100) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,occurred_at,correlation_id,causation_id
                FROM platform.outbox_events WHERE publish_status <> 'PUBLISHED' AND (leased_until IS NULL OR leased_until < now())
                ORDER BY aggregate_id, aggregate_version NULLS LAST, created_at FOR UPDATE SKIP LOCKED LIMIT %s""", (limit,))
            rows = cur.fetchall()
            ids = [row[0] for row in rows]
            if ids:
                cur.execute("UPDATE platform.outbox_events SET lease_owner=%s, leased_until=now()+interval '60 seconds' WHERE event_id = ANY(%s)", (self.owner, ids))
        self.conn.commit()
        result = {"published": 0, "failed": 0}
        for row in rows:
            envelope = EventEnvelope(row[0], row[1], row[2], row[3], row[4], row[7], row[6], row[8], row[9])
            try:
                await self.publisher.publish(envelope)
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE platform.outbox_events SET publish_status='PUBLISHED', published_at=now(), attempts=attempts+1, lease_owner=NULL, leased_until=NULL WHERE event_id=%s", (row[0],))
                self.conn.commit(); result["published"] += 1
            except Exception as exc:
                with self.conn.cursor() as cur:
                    cur.execute("UPDATE platform.outbox_events SET publish_status='FAILED', last_error=%s, attempts=attempts+1, lease_owner=NULL, leased_until=NULL WHERE event_id=%s", (str(exc), row[0]))
                self.conn.commit(); result["failed"] += 1
        return result
