"""The P4 activation boundary (mission section 0/1 "SHIP-FIRST DECISIONS" #1).

The 9 canonical EntrySignals that existed before this runtime's first start are NOT eligible for
ManagedTrade creation - the durable JetStream consumer for `signal.entry.created.v1` is created
with `DeliverPolicy.NEW` the *first* time it is established, so JetStream itself never delivers
any message already in the stream at that instant (this is the actual enforcement mechanism, not
an application-level filter re-checking every message). This module's job is narrower: persist a
durable, auditable record of exactly when and against what stream state that boundary was
established, so the claim "pre-boundary signals were never replayed" is provable after the fact,
not just asserted.

Reuses the existing generic `platform.system_metadata(key, value jsonb)` key-value table
(001_foundation.sql) rather than adding a new table for one row - this is not itself
"understood domain state" of the kind `trade_management`'s own relational-first convention
applies to; it is a single one-time operational marker, the same class of thing
`migration/cutoff.py`'s `establish_cutoff` already persists for P2-A1's own cutover.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

METADATA_KEY = "trade_management.p4_activation_boundary"


def establish_activation_boundary(conn: Any, *, consumer_name: str, subject: str,
                                  stream: str, stream_messages_at_establishment: int,
                                  deliver_policy: str = "NEW", now_utc: datetime | None = None) -> dict[str, Any]:
    """Idempotent: the first successful call wins and is never overwritten (ON CONFLICT DO
    NOTHING) - re-running this against an already-activated deployment returns the *original*
    boundary record, not a new one, so a redeploy/restart can never silently move the boundary
    forward and start excluding signals that arrived between the two starts."""
    now_utc = now_utc or datetime.now(timezone.utc)
    record = {
        "established_at": now_utc.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "consumer_name": consumer_name, "subject": subject, "stream": stream,
        "deliver_policy": deliver_policy,
        "stream_messages_at_establishment": stream_messages_at_establishment,
        "note": "signal.entry.created.v1 messages already in the stream at this instant are "
                "excluded by JetStream DeliverPolicy.NEW at consumer creation, not replayed.",
    }
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO platform.system_metadata(key, value) VALUES (%s, %s::jsonb)
                      ON CONFLICT (key) DO NOTHING""", (METADATA_KEY, json.dumps(record, sort_keys=True)))
    conn.commit()
    return load_activation_boundary(conn) or record


def load_activation_boundary(conn: Any) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM platform.system_metadata WHERE key = %s", (METADATA_KEY,))
        row = cur.fetchone()
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, dict) else json.loads(value)
