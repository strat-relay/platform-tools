"""Small relational control-plane store for global V2 execution authority."""
from __future__ import annotations

import uuid
from typing import Any, Callable

from postgres.db import connect, transaction
from .risk import RiskPolicyError

SOURCE_POSTGRES = "POSTGRES"
DISABLED = "DISABLED"
ENABLED = "ENABLED"


def read_authority(connect_fn: Callable[..., Any] = connect) -> dict[str, Any]:
    try:
        with connect_fn(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT authority_id, state, revision, updated_at, updated_by, source
                              FROM execution_v2.execution_authority WHERE authority_id='current'""")
                row = cur.fetchone()
        if row is None:
            return {"state": DISABLED, "revision": 0, "source": SOURCE_POSTGRES,
                    "error": "AUTHORITY_ROW_MISSING"}
        authority = dict(zip(("authority_id", "state", "revision", "updated_at", "updated_by", "source"), row))
        if authority["state"] not in (DISABLED, ENABLED):
            authority.update(state=DISABLED, error="AUTHORITY_STATE_INVALID")
        return authority
    except Exception:
        return {"state": DISABLED, "revision": 0, "source": SOURCE_POSTGRES,
                "error": "AUTHORITY_UNAVAILABLE"}


def set_authority(state: str, *, expected_revision: int, changed_by: str | None,
                  preflight: dict[str, Any] | None = None,
                  connect_fn: Callable[..., Any] = connect) -> dict[str, Any]:
    if state not in (DISABLED, ENABLED):
        raise RiskPolicyError("authority state must be DISABLED or ENABLED")
    conn = connect_fn(readonly=False)
    with transaction(conn):
        with conn.cursor() as cur:
            cur.execute("""SELECT state, revision FROM execution_v2.execution_authority
                          WHERE authority_id='current' FOR UPDATE""")
            row = cur.fetchone()
            if row is None:
                raise RiskPolicyError("canonical execution authority row is missing")
            previous_state, previous_revision = row
            if int(previous_revision) != int(expected_revision):
                raise RiskPolicyError("stale authority revision")
            revision = int(previous_revision) + 1
            cur.execute("""UPDATE execution_v2.execution_authority
                          SET state=%s, revision=%s, updated_at=now(), updated_by=%s, source='POSTGRES'
                          WHERE authority_id='current'""", (state, revision, changed_by))
            cur.execute("""INSERT INTO execution_v2.execution_authority_change
                (change_id, authority_id, previous_state, new_state, previous_revision,
                 new_revision, changed_by, source, preflight)
                VALUES (%s,'current',%s,%s,%s,%s,%s,'POSTGRES',%s::jsonb)""",
                       (str(uuid.uuid4()), previous_state, state, previous_revision, revision,
                        changed_by, __import__('json').dumps(preflight or {})))
            cur.execute("""INSERT INTO platform.outbox_events
                (event_id,event_type,aggregate_type,aggregate_id,aggregate_version,schema_version,payload,occurred_at)
                VALUES (%s,'system.status_changed.v1','execution_authority','current',%s,'event-envelope.v1',%s::jsonb,now())""",
                       (str(uuid.uuid4()), revision, __import__('json').dumps({
                           "authority": state, "revision": revision, "source": SOURCE_POSTGRES})))
    return {"state": state, "revision": revision, "source": SOURCE_POSTGRES,
            "updated_by": changed_by, "preflight": preflight or {}}
