"""P4 JetStream stream topology verification. Deliberately narrower than
`infrastructure.messaging.jetstream.JetStreamTopology.ensure()`, which would also attempt to
`add_stream` TRADING_CORE/EXECUTION if either were ever missing - this module never calls
`add_stream` for anything but `TRADING_OBSERVATION`, the one P4-owned stream this vertical slice
needs (mission section 5, "Prefer the smallest coherent P4 stream topology";
"TRADING_CORE MUST REMAIN UNCHANGED").
"""
from __future__ import annotations

from typing import Any

from infrastructure.messaging.contracts import STREAMS

P4_OWNED_STREAMS = ("TRADING_OBSERVATION",)


class StreamTopologyError(RuntimeError):
    """A required stream is missing or cannot be reached; the runtime must not start."""


async def verify_trading_core_unchanged(js_manager: Any) -> None:
    """Read-only. Fails closed if TRADING_CORE is unreachable - never creates or modifies it."""
    try:
        await js_manager.stream_info("TRADING_CORE")
    except Exception as exc:
        raise StreamTopologyError("TRADING_CORE is not reachable; P4 runtime will not start "
                                  "(it never creates or modifies this stream)") from exc


async def ensure_p4_streams(js_manager: Any) -> dict[str, Any]:
    """Idempotently ensures only the P4-owned streams exist. Returns each stream's current
    message count (used to record the activation boundary)."""
    counts: dict[str, Any] = {}
    for name in P4_OWNED_STREAMS:
        config = STREAMS[name]
        try:
            info = await js_manager.stream_info(name)
        except Exception:
            await js_manager.add_stream(name=name, subjects=list(config["subjects"]),
                                        storage=config["storage"], max_age=config["max_age"])
            info = await js_manager.stream_info(name)
        counts[name] = getattr(getattr(info, "state", None), "messages", None)
    return counts
