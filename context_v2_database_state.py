"""PostgreSQL-only state store for Context V2.

The runner may keep a Python dict during one poll cycle, but durable state,
lifecycle events, candidates, manifest, and heartbeat are all persisted in
PostgreSQL.  There is deliberately no filesystem fallback.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from postgres.config import PostgresConfig
from postgres.db import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContextV2DatabaseState:
    def __init__(self, instance_id: str):
        self.instance_id = instance_id
        cfg = PostgresConfig.from_env()
        if not ((cfg.dsn and cfg.dsn.strip()) or cfg.host):
            raise RuntimeError("Context V2 requires an explicit PostgreSQL target")
        self.conn = connect(cfg)
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (f"context-v2:{instance_id}",))
        self.conn.commit()

    def close(self) -> None:
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"context-v2:{self.instance_id}",))
            self.conn.commit()
        finally:
            self.conn.close()

    def load(self, empty_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT state, manifest FROM strategy.context_v2_runner_state
                           WHERE instance_id = %s""", (self.instance_id,))
            row = cur.fetchone()
        if row is None:
            return empty_state, {}
        return dict(row[0]), dict(row[1] or {})

    def save(self, state: dict[str, Any], manifest: dict[str, Any] | None = None) -> None:
        manifest = manifest or {}
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.context_v2_runner_state
                (instance_id, strategy_version, state, manifest, revision, runner_status,
                 last_heartbeat_at, updated_at)
                VALUES (%s,'CONTEXT_STRUCTURE_RETRACE_V2',%s::jsonb,%s::jsonb,1,%s,%s,now())
                ON CONFLICT (instance_id) DO UPDATE SET
                  state = EXCLUDED.state, manifest = CASE WHEN EXCLUDED.manifest = '{}'::jsonb
                    THEN strategy.context_v2_runner_state.manifest ELSE EXCLUDED.manifest END,
                  revision = strategy.context_v2_runner_state.revision + 1,
                  runner_status = EXCLUDED.runner_status,
                  last_heartbeat_at = EXCLUDED.last_heartbeat_at, updated_at = now()""",
                        (self.instance_id, json.dumps(state, default=str), json.dumps(manifest),
                         state.get("runner_status", "ACTIVE"), _now()))
            self._upsert_candidates(cur, state)
        self.conn.commit()

    def append_event(self, state: dict[str, Any], event: dict[str, Any]) -> None:
        event_id = str(event.get("event_id") or f"{event.get('type')}|{event.get('economic_position_id') or event.get('setup_id') or event.get('symbol')}")
        event = {"schema": "context-v2-db", "strategy_version": "CONTEXT_STRUCTURE_RETRACE_V2", **event}
        with self.conn.cursor() as cur:
            cur.execute("""INSERT INTO strategy.context_v2_lifecycle_events
                (event_id, instance_id, event_type, event_time, payload)
                VALUES (%s,%s,%s,%s,%s::jsonb) ON CONFLICT (event_id) DO NOTHING""",
                        (event_id, self.instance_id, str(event.get("type", "UNKNOWN")),
                         event.get("event_time") or _now(), json.dumps(event, default=str)))
        state.setdefault("counters", {}).setdefault("events", 0)
        state["counters"]["events"] += 1
        self.save(state)

    def _upsert_candidates(self, cur: Any, state: dict[str, Any]) -> None:
        # Candidate payloads remain queryable in PostgreSQL; no JSONL handoff is used.
        for setup in state.get("setups", {}).values():
            for position in setup.get("opportunities", []):
                candidate_id = str(position.get("entry_opportunity_id") or "")
                if not candidate_id:
                    continue
                signal_id = f"SIG_{candidate_id}"
                payload = {"setup": setup, "position": position}
                cur.execute("""INSERT INTO strategy.context_v2_signal_candidates
                    (signal_id, instance_id, candidate_id, economic_position_id, status, candidate)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (signal_id) DO UPDATE SET status=EXCLUDED.status,
                      candidate=EXCLUDED.candidate, updated_at=now()""",
                            (signal_id, self.instance_id, candidate_id, position.get("economic_position_id"),
                             position.get("status", "DISCOVERED"), json.dumps(payload, default=str)))
