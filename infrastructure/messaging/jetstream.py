from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .contracts import EventEnvelope, STREAMS, validate_subject


class JetStreamClient(Protocol):
    async def publish(self, subject: str, payload: bytes, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class JetStreamTopology:
    streams: Mapping[str, Mapping[str, Any]]

    @classmethod
    def v1(cls) -> "JetStreamTopology":
        return cls(STREAMS)

    async def ensure(self, manager: Any) -> None:
        """Idempotently create/update streams through an injected JS manager."""
        for name, config in self.streams.items():
            subject_list = list(config["subjects"])
            try:
                await manager.stream_info(name)
            except Exception:
                await manager.add_stream(name=name, subjects=subject_list,
                                         storage="file", max_age=config["max_age"])


class JetStreamPublisher:
    def __init__(self, client: JetStreamClient):
        self.client = client

    async def publish(self, envelope: EventEnvelope) -> Any:
        validate_subject(envelope.event_type)
        return await self.client.publish(envelope.event_type, envelope.canonical_bytes())


class JetStreamConsumer:
    """Thin callback boundary; DB inbox claiming supplies effectively-once effects."""

    def __init__(self, subscribe: Callable[..., Awaitable[Any]], *, consumer_name: str):
        self.subscribe = subscribe
        self.consumer_name = consumer_name

    async def consume(self, subject: str, handler: Callable[[EventEnvelope], Awaitable[None]]) -> Any:
        validate_subject(subject)
        return await self.subscribe(subject, handler=handler, durable=self.consumer_name,
                                    manual_ack=True)
