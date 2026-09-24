"""In-process fakes for runtime wiring tests only: no real NATS server is reachable in this
sandbox. Mimics the subset of `nats.js.client.JetStreamContext`'s real async interface this
package's modules actually call (verified against the installed `nats-py` package's real method
signatures), so the wiring logic (which `ConsumerConfig`/`DeliverPolicy` it builds, which
subject/stream it targets, ack/nak behaviour) is exercised against the real `nats.js.api`
classes, without a live NATS server.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from trade_management.fakes import FakeConnection, FakeCursor

_OUTBOX_COLUMNS = ("event_id", "event_type", "aggregate_type", "aggregate_id", "aggregate_version",
                  "payload", "occurred_at", "correlation_id", "causation_id")


class RuntimeFakeCursor(FakeCursor):
    """Extends the domain `FakeCursor` (unmodified) with the additional read queries the
    runtime layer issues - `trade_management/fakes.py` itself is left untouched, so its
    existing, already-proven test coverage is unaffected by this additive subclass."""

    def _select(self, upper: str, sql: str, params: Any) -> None:
        if "TRADE_MANAGEMENT.MANAGED_TRADE" in upper and "STATE = 'OPEN'" in upper:
            rows = [(r["managed_trade_id"], r["instrument"])
                    for r in self.conn.view("trade_management.managed_trade").values()
                    if r["state"] == "OPEN"]
            self._rows = rows
            self._result = "MULTI"
            return
        if "PLATFORM.OUTBOX_EVENTS" in upper and "EVENT_ID = %S" in upper:
            row = self.conn.view("platform.outbox_events").get(params[0])
            self._result = tuple(row[k] for k in _OUTBOX_COLUMNS) if row else None
            return
        if "PLATFORM.OUTBOX_EVENTS" in upper and "EVENT_TYPE = %S" in upper:
            event_type, limit = params
            rows = [(r["event_id"],) for r in self.conn.view("platform.outbox_events").values()
                    if r["event_type"] == event_type and r["publish_status"] != "PUBLISHED"]
            self._rows = rows[:limit]
            self._result = "MULTI"
            return
        if "PLATFORM.SYSTEM_METADATA" in upper:
            row = self.conn.view("platform.system_metadata").get(params[0])
            self._result = (row["value"],) if row else None
            return
        super()._select(upper, sql, params)

    def _update(self, upper: str, sql: str, params: Any) -> None:
        if "PLATFORM.OUTBOX_EVENTS" in upper:
            event_id = params[-1]  # always the last bound param, per postgres/foundation.py's WHERE event_id=%s
            row = self.conn.pending_and_committed("platform.outbox_events").get(event_id)
            if row is None:
                self.rowcount = 0
                return
            updated = dict(row)
            if "PUBLISH_STATUS='PUBLISHED'" in upper.replace(" ", ""):
                updated["publish_status"] = "PUBLISHED"
            elif "PUBLISH_STATUS='FAILED'" in upper.replace(" ", ""):
                updated["publish_status"] = "FAILED"
            self.conn.pending["platform.outbox_events"][event_id] = updated
            self.rowcount = 1
            return
        super()._update(upper, sql, params)


class RuntimeFakeConnection(FakeConnection):
    def cursor(self) -> RuntimeFakeCursor:
        return RuntimeFakeCursor(self)

    def build_insert(self, upper: str, sql: str, params: Any):
        if "PLATFORM.SYSTEM_METADATA" in upper:
            key, value = params
            import json
            return "platform.system_metadata", key, {"key": key,
                                                      "value": json.loads(value) if isinstance(value, str) else value}
        return super().build_insert(upper, sql, params)


class ConsumerNotFound(Exception):
    pass


class StreamNotFound(Exception):
    pass


@dataclass
class FakeMsg:
    data: bytes
    acked: bool = False
    naked: bool = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.naked = True


@dataclass
class FakeStream:
    subjects: list[str]
    messages: list[bytes] = field(default_factory=list)


@dataclass
class FakeSubscription:
    stream: str
    subject: str
    durable: str
    cb: Callable[[FakeMsg], Any]


@dataclass
class FakeJetStreamManager:
    """`publish()` delivers synchronously to any subscription registered (via `subscribe()`)
    *before* the publish call - matching DeliverPolicy.NEW's real-world effect (a durable
    consumer created after a message was published never receives it) closely enough to prove
    this package's own wiring never replays pre-existing messages, without claiming to
    reimplement JetStream's actual delivery/redelivery semantics."""

    streams: dict[str, FakeStream] = field(default_factory=dict)
    consumers: dict[tuple[str, str], Any] = field(default_factory=dict)
    subscriptions: list[FakeSubscription] = field(default_factory=list)
    add_stream_calls: list[str] = field(default_factory=list)
    add_consumer_calls: list[tuple[str, Any]] = field(default_factory=list)

    async def stream_info(self, name: str) -> Any:
        if name not in self.streams:
            raise StreamNotFound(name)
        stream = self.streams[name]
        return SimpleNamespace(state=SimpleNamespace(messages=len(stream.messages)))

    async def add_stream(self, *, name: str, subjects: list[str], storage: str, max_age: int) -> Any:
        self.streams.setdefault(name, FakeStream(subjects=list(subjects)))
        self.add_stream_calls.append(name)
        return await self.stream_info(name)

    async def consumer_info(self, stream: str, consumer: str) -> Any:
        key = (stream, consumer)
        if key not in self.consumers:
            raise ConsumerNotFound(key)
        return self.consumers[key]

    async def add_consumer(self, stream: str, config: Any) -> Any:
        key = (stream, config.durable_name)
        self.consumers[key] = config
        self.add_consumer_calls.append((stream, config))
        return config

    async def subscribe(self, subject: str, *, stream: str, durable: str, manual_ack: bool, cb: Any) -> FakeSubscription:
        sub = FakeSubscription(stream=stream, subject=subject, durable=durable, cb=cb)
        self.subscriptions.append(sub)
        return sub

    async def publish(self, subject: str, payload: bytes, *, headers: Any = None) -> Any:
        for name, stream in self.streams.items():
            if subject in stream.subjects:
                stream.messages.append(payload)
                for sub in self.subscriptions:
                    if sub.stream == name and sub.subject == subject:
                        await sub.cb(FakeMsg(data=payload))
                return SimpleNamespace(stream=name, seq=len(stream.messages))
        raise RuntimeError(f"no stream registered for subject {subject!r} in this fake")

    # -- test helpers, not part of the real interface --

    def seed_pre_existing_messages(self, stream: str, subject: str, payloads: list[bytes]) -> None:
        """Simulates messages already durably in a stream before this process ever starts -
        used to prove the activation boundary excludes them."""
        self.streams.setdefault(stream, FakeStream(subjects=[subject])).messages.extend(payloads)
