from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from postgres.foundation import claim_inbox, mark_inbox_processed


@dataclass
class ShadowMetrics:
    received: int = 0
    processed: int = 0
    duplicate_hits: int = 0
    redeliveries: int = 0
    failures: int = 0
    lag_messages: int = 0

    def to_dict(self) -> dict[str, int]:
        return self.__dict__.copy()


class SignalShadowConsumer:
    """TRADING_CORE consumer that only records inbox/reconciliation evidence."""

    def __init__(self, conn: Any, *, consumer_name: str = "p2-signal-shadow"):
        self.conn = conn
        self.consumer_name = consumer_name
        self.metrics = ShadowMetrics()

    def handle(self, payload: bytes | str, *, redelivered: bool = False) -> bool:
        self.metrics.received += 1
        self.metrics.redeliveries += int(redelivered)
        try:
            envelope = json.loads(payload)
            event_id = str(envelope["event_id"])
            if not claim_inbox(self.conn, self.consumer_name, event_id):
                self.metrics.duplicate_hits += 1
                self.conn.commit()
                return False
            # Shadow consumer deliberately has no domain side effect and no
            # execution connection. It records only receipt/processing.
            mark_inbox_processed(self.conn, self.consumer_name, event_id)
            self.conn.commit(); self.metrics.processed += 1
            return True
        except Exception:
            self.conn.rollback(); self.metrics.failures += 1
            raise
