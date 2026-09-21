"""Small deterministic JetStream-like harness for offline tests only."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingMessage:
    subject: str
    payload: bytes
    deliveries: int = 1


class InMemoryJetStream:
    def __init__(self):
        self.messages: dict[str, deque[PendingMessage]] = defaultdict(deque)
        self.acked: set[tuple[str, bytes]] = set()

    async def publish(self, subject: str, payload: bytes, **_: Any) -> dict[str, Any]:
        self.messages[subject].append(PendingMessage(subject, payload))
        return {"stream": "TEST", "seq": sum(len(x) for x in self.messages.values())}

    async def next(self, subject: str) -> PendingMessage:
        return self.messages[subject][0]

    async def ack(self, message: PendingMessage) -> None:
        self.messages[message.subject].popleft()
        self.acked.add((message.subject, message.payload))

    async def redeliver(self, subject: str) -> PendingMessage:
        message = await self.next(subject)
        message.deliveries += 1
        return message
