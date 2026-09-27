"""Redis hot risk state: the latest valid RiskSnapshot, reference metadata, collector health, and
account-scoped execution risk reservations.

Redis is hot state only. PostgreSQL stays the durable system of record for intents, attempts and
results; Redis never holds execution history. Keys are account-scoped by a non-reversible account
reference (snapshot.account_ref), never the raw account id:

    risk:snapshot:<ref>      JSON RiskSnapshot (collector writes, execution reads)
    risk:reference:<ref>     hash provider_symbol -> JSON symbol sizing metadata
    risk:health:<ref>        JSON collector health
    risk:reservations:<ref>  hash execution_intent_id -> JSON reservation
    risk:refresh:<ref>       refresh request flag (post-broker invalidation)

Reservation lifecycle (atomic Lua; see RESERVE_LUA / TRANSITION_LUA):

    RESERVED --submit--> SUBMITTED --confirm--> CONFIRMED --(broker snapshot newer)--> RETIRED
       |                     |  \\--unknown--> UNKNOWN --(reconciliation only)--> CONFIRMED | RELEASED
       |                     \\--broker-confirmed rejection / never dispatched--> RELEASED
       |--definite pre-submit rejection--> RELEASED
       \\--TTL (only while RESERVED: provably never submitted)--> EXPIRED

RESERVED, SUBMITTED and UNKNOWN always consume a position slot, an in-flight order slot and their
reserved risk; CONFIRMED consumes them until a broker snapshot observed after the confirmation
(which then shows the real position). SUBMITTED and UNKNOWN never expire.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .snapshot import COMPONENTS, HEALTHY, REFERENCE_SCHEMA_VERSION, RiskSnapshot

ACTIVE_STATUSES = ("RESERVED", "SUBMITTED", "UNKNOWN")
TERMINAL_STATUSES = ("RELEASED", "EXPIRED", "RETIRED")

RESERVE_LUA = r"""
local existing = redis.call('HGET', KEYS[2], ARGV[1])
if existing then return {'EXISTING', existing} end
local raw = redis.call('GET', KEYS[1])
if not raw then return {'REJECTED', 'RISK_STATE_UNAVAILABLE'} end
local snap = cjson.decode(raw)
if tostring(snap.generation) ~= ARGV[4] then return {'RETRY', 'SNAPSHOT_CHANGED'} end
local now = tonumber(ARGV[3])
local positions_seen = tonumber(snap.positions_observed_at) or 0
local active, inflight, reserved_risk = 0, 0, 0
local all = redis.call('HGETALL', KEYS[2])
for i = 1, #all, 2 do
  local r = cjson.decode(all[i + 1])
  local s = r.status
  if s == 'RESERVED' and tonumber(r.expires_at) <= now then
    r.status = 'EXPIRED'; r.updated_at = now
    redis.call('HSET', KEYS[2], all[i], cjson.encode(r)); s = 'EXPIRED'
  end
  if s == 'RESERVED' or s == 'SUBMITTED' or s == 'UNKNOWN' then
    active = active + 1; inflight = inflight + 1; reserved_risk = reserved_risk + tonumber(r.reserved_risk)
  elseif s == 'CONFIRMED' and tonumber(r.confirmed_at) >= positions_seen then
    active = active + 1; reserved_risk = reserved_risk + tonumber(r.reserved_risk)
  end
end
local daily_loss = tonumber(snap.daily_loss)
if daily_loss == nil then return {'REJECTED', 'RISK_STATE_UNAVAILABLE'} end
if daily_loss >= tonumber(ARGV[8]) then return {'REJECTED', 'DAILY_LOSS_LIMIT_EXCEEDED'} end
if #snap.open_positions + active >= tonumber(ARGV[5]) then return {'REJECTED', 'MAX_CONCURRENT_POSITIONS_EXCEEDED'} end
if #snap.pending_orders + inflight >= tonumber(ARGV[6]) then return {'REJECTED', 'MAX_CONCURRENT_ORDERS_EXCEEDED'} end
if tonumber(ARGV[9]) + reserved_risk >= tonumber(ARGV[7]) then return {'REJECTED', 'MAX_ACCOUNT_EXPOSURE_EXCEEDED'} end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
return {'RESERVED', ARGV[2]}
"""

TRANSITION_LUA = r"""
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if not raw then return {'MISSING', ''} end
local r = cjson.decode(raw)
local now = tonumber(ARGV[4])
if r.status == 'RESERVED' and tonumber(r.expires_at) <= now then r.status = 'EXPIRED'; r.updated_at = now end
local allowed = false
for s in string.gmatch(ARGV[2], '[^,]+') do if s == r.status then allowed = true end end
if not allowed then
  redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(r))
  return {'REFUSED', r.status}
end
r.status = ARGV[3]; r.updated_at = now
if ARGV[5] ~= '' then r[ARGV[5]] = now end
if ARGV[6] ~= '' then r.note = ARGV[6] end
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(r))
return {'OK', r.status}
"""


@dataclass(frozen=True)
class ReserveOutcome:
    status: str          # RESERVED | EXISTING | REJECTED | RETRY
    reason: str | None
    reservation: dict[str, Any] | None


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class RedisRiskStateStore:
    def __init__(self, redis_client: Any, account_ref: str, *, clock: Any = time.time):
        self.redis = redis_client
        self.ref = account_ref
        self.clock = clock
        self._reserve = redis_client.register_script(RESERVE_LUA)
        self._transition = redis_client.register_script(TRANSITION_LUA)

    # keys
    def key(self, kind: str) -> str:
        return f"risk:{kind}:{self.ref}"

    # ---- snapshot (collector writes, execution reads) -------------------------------------
    def read_snapshot(self) -> RiskSnapshot | None:
        raw = self.redis.get(self.key("snapshot"))
        return None if raw is None else RiskSnapshot.from_dict(json.loads(raw))

    def write_snapshot(self, update: Any) -> RiskSnapshot:
        """Apply `update(snapshot) -> snapshot` atomically (optimistic WATCH), bumping the
        generation. The collector is the only writer; the WATCH guards against a second one."""
        key = self.key("snapshot")
        while True:
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    current = RiskSnapshot.from_dict(json.loads(raw)) if raw else RiskSnapshot(account_ref=self.ref)
                    updated = update(current)
                    updated.generation = current.generation + 1
                    stamps = [t for t in (updated.equity_observed_at, updated.positions_observed_at,
                                          updated.orders_observed_at, updated.history_observed_at) if t]
                    updated.observed_at = max(stamps) if stamps else None
                    pipe.multi()
                    pipe.set(key, json.dumps(updated.to_dict(), sort_keys=True))
                    pipe.execute()
                    return updated
                except Exception as exc:  # redis WatchError: retry; anything else propagates
                    if type(exc).__name__ != "WatchError":
                        raise

    # ---- reference metadata --------------------------------------------------------------
    def write_reference(self, provider_symbol: str, values: dict[str, Any]) -> None:
        payload = {**values, "schema_version": REFERENCE_SCHEMA_VERSION, "observed_at": self.clock()}
        self.redis.hset(self.key("reference"), provider_symbol, json.dumps(payload, sort_keys=True))

    def read_references(self) -> dict[str, dict[str, Any]]:
        raw = self.redis.hgetall(self.key("reference")) or {}
        return {_text(k): json.loads(v) for k, v in raw.items()}

    # ---- collector health ----------------------------------------------------------------
    def read_health(self) -> dict[str, Any]:
        raw = self.redis.get(self.key("health"))
        return json.loads(raw) if raw else {}

    def write_health(self, health: dict[str, Any]) -> None:
        self.redis.set(self.key("health"), json.dumps(health, sort_keys=True))

    # ---- post-broker refresh requests ----------------------------------------------------
    def request_refresh(self, reason: str) -> None:
        """Ask the collector for an immediate fast refresh; never waited on by execution."""
        self.redis.set(self.key("refresh"), json.dumps({"reason": reason, "requested_at": self.clock()}))

    def take_refresh_request(self) -> dict[str, Any] | None:
        raw = self.redis.getdel(self.key("refresh")) if hasattr(self.redis, "getdel") else None
        return json.loads(raw) if raw else None

    # ---- reservations --------------------------------------------------------------------
    def reserve(self, *, intent_id: str, signal_id: str, canonical_instrument: str, direction: str,
                reserved_risk: float, expected_generation: int, ttl_seconds: float,
                max_positions: int, max_orders: int, max_exposure: float, max_daily_loss: float,
                snapshot_exposure: float) -> ReserveOutcome:
        now = self.clock()
        reservation = {"intent_id": intent_id, "signal_id": signal_id, "account_ref": self.ref,
                       "canonical_instrument": canonical_instrument, "direction": direction,
                       "reserved_risk": float(reserved_risk), "reserved_slots": 1, "status": "RESERVED",
                       "created_at": now, "updated_at": now, "expires_at": now + float(ttl_seconds)}
        status, payload = self._reserve(
            keys=[self.key("snapshot"), self.key("reservations")],
            args=[intent_id, json.dumps(reservation, sort_keys=True), repr(now), str(expected_generation),
                  str(int(max_positions)), str(int(max_orders)), repr(float(max_exposure)),
                  repr(float(max_daily_loss)), repr(float(snapshot_exposure))])
        status, payload = _text(status), _text(payload)
        if status in ("RESERVED", "EXISTING"):
            return ReserveOutcome(status, None, json.loads(payload))
        return ReserveOutcome(status, payload, None)

    def transition(self, intent_id: str, *, allowed_from: tuple[str, ...], to: str,
                   stamp_field: str = "", note: str = "") -> tuple[bool, str]:
        status, current = self._transition(keys=[self.key("reservations")],
                                           args=[intent_id, ",".join(allowed_from), to, repr(self.clock()),
                                                 stamp_field, note])
        return _text(status) == "OK", _text(current)

    # Lifecycle, named for the execution states they follow.
    def mark_submitted(self, intent_id: str) -> tuple[bool, str]:
        return self.transition(intent_id, allowed_from=("RESERVED",), to="SUBMITTED", stamp_field="submitted_at")

    def release_before_submit(self, intent_id: str, reason: str) -> tuple[bool, str]:
        return self.transition(intent_id, allowed_from=("RESERVED",), to="RELEASED", note=reason)

    def release_not_dispatched(self, intent_id: str, reason: str) -> tuple[bool, str]:
        """Broker/bridge proved the order was never executed (explicit rejection or never dispatched)."""
        return self.transition(intent_id, allowed_from=("RESERVED", "SUBMITTED"), to="RELEASED", note=reason)

    def confirm(self, intent_id: str) -> tuple[bool, str]:
        return self.transition(intent_id, allowed_from=("SUBMITTED",), to="CONFIRMED", stamp_field="confirmed_at")

    def mark_unknown(self, intent_id: str, reason: str) -> tuple[bool, str]:
        return self.transition(intent_id, allowed_from=("SUBMITTED", "RESERVED"), to="UNKNOWN", note=reason)

    def reservations(self) -> dict[str, dict[str, Any]]:
        raw = self.redis.hgetall(self.key("reservations")) or {}
        return {_text(k): json.loads(v) for k, v in raw.items()}

    def active_reservations(self, snapshot: RiskSnapshot | None, now: float | None = None) -> list[dict[str, Any]]:
        now = self.clock() if now is None else now
        seen = (snapshot.positions_observed_at or 0) if snapshot else 0
        return [r for r in self.reservations().values()
                if (r["status"] in ACTIVE_STATUSES and not (r["status"] == "RESERVED" and r["expires_at"] <= now))
                or (r["status"] == "CONFIRMED" and r.get("confirmed_at", 0) >= seen)]

    def prune(self, snapshot: RiskSnapshot | None, *, keep_seconds: float = 86400.0) -> int:
        """Retire CONFIRMED reservations the broker snapshot has caught up with; drop old terminal ones."""
        now, removed = self.clock(), 0
        seen = (snapshot.positions_observed_at or 0) if snapshot else 0
        for intent_id, r in self.reservations().items():
            if r["status"] == "CONFIRMED" and r.get("confirmed_at", now) < seen:
                self.transition(intent_id, allowed_from=("CONFIRMED",), to="RETIRED", note="BROKER_SNAPSHOT_CAUGHT_UP")
            elif r["status"] in TERMINAL_STATUSES and now - float(r.get("updated_at", now)) > keep_seconds:
                removed += self.redis.hdel(self.key("reservations"), intent_id)
        return removed


def component_healthy(snapshot: RiskSnapshot, component: str) -> bool:
    return snapshot.source_health.get(component) == HEALTHY


__all__ = ["RedisRiskStateStore", "ReserveOutcome", "ACTIVE_STATUSES", "COMPONENTS", "component_healthy"]
