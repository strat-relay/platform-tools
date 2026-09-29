"""Redis store for the broker view.

Keys:
    broker:view:<tool>:<arguments json>   JSON {schema, tool, arguments, observed_at, data}
    broker:view:health                    JSON populator health {status, updated_at, ok, errors}

`observed_at` is the epoch time the bridge answered. Entries expire after ENTRY_TTL_SECONDS so a
dead populator can't leave data behind forever; readers enforce their own, much shorter max age.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SCHEMA = "broker-view.v1"
HEALTH_KEY = "broker:view:health"
ENTRY_TTL_SECONDS = 600


def entry_key(tool: str, arguments: dict[str, Any] | None) -> str:
    return f"broker:view:{tool}:{json.dumps(arguments or {}, sort_keys=True, separators=(',', ':'))}"


@dataclass(frozen=True)
class BrokerViewEntry:
    tool: str
    arguments: dict[str, Any] | None
    observed_at: float
    data: Any


class BrokerViewStore:
    def __init__(self, redis_client: Any):
        self.redis = redis_client

    def put(self, tool: str, arguments: dict[str, Any] | None, data: Any, *, observed_at: float) -> None:
        value = {"schema": SCHEMA, "tool": tool, "arguments": arguments or {},
                 "observed_at": observed_at, "data": data}
        self.redis.set(entry_key(tool, arguments), json.dumps(value, separators=(",", ":")),
                       ex=ENTRY_TTL_SECONDS)

    def get(self, tool: str, arguments: dict[str, Any] | None) -> BrokerViewEntry | None:
        raw = self.redis.get(entry_key(tool, arguments))
        if raw is None:
            return None
        value = json.loads(raw)
        if value.get("schema") != SCHEMA:
            return None
        return BrokerViewEntry(tool, arguments, float(value["observed_at"]), value.get("data"))

    def put_health(self, health: dict[str, Any]) -> None:
        self.redis.set(HEALTH_KEY, json.dumps(health, separators=(",", ":")), ex=ENTRY_TTL_SECONDS)

    def health(self) -> dict[str, Any] | None:
        raw = self.redis.get(HEALTH_KEY)
        return json.loads(raw) if raw is not None else None
