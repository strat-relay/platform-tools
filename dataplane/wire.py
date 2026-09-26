"""Decode an EventEnvelope from JetStream wire bytes.

`EventEnvelope.canonical_bytes()`/`to_dict()` already exist (infrastructure/messaging/contracts.py);
this is the missing inverse, kept here rather than added to that shared module to avoid touching it
further than the additive subject/stream entries already required for the real-time subject.
"""
from __future__ import annotations

import json

from infrastructure.messaging.contracts import EventEnvelope


def decode_envelope(payload: bytes) -> EventEnvelope:
    data = json.loads(payload.decode("utf-8"))
    return EventEnvelope(**data)
