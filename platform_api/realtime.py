"""The Console realtime WebSocket server (mission sections 2, 3, 6, 8, 16, 18).

Runs as its OWN deployable process (`python -m platform_api.realtime`), separate from the
existing synchronous `platform_api.signals`/`platform_api.control` stdlib HTTP server - this
keeps every already-working REST route completely untouched (zero risk) and isolates the new
asyncio/WebSocket code in its own process. The browser connects here directly (proxied by
`platform-api-router`'s nginx, which owns TLS/Cloudflare termination exactly as it already does
for REST); this process in turn is the ONLY thing that ever touches NATS JetStream on behalf of
the Console - the browser never sees a JetStream credential, durable name, or raw payload
(mission section 2, "The browser MUST NOT connect directly to NATS JetStream").

Protocol (mission section 8):

  client -> server (JSON text frames):
    {"action": "subscribe", "resources": ["signals", "trade-management"], "since": {"signals": 42}}
    {"action": "unsubscribe", "resources": ["signals"]}
    {"action": "ping"}

  server -> client (JSON text frames, `console-realtime.v1` envelopes for real events, plus
  these control messages for protocol bookkeeping):
    {"schema": "console-realtime.v1", "type": "_control.subscribed", "resource": "signals", "sequence": 42}
    {"schema": "console-realtime.v1", "type": "_control.resync_required", "resource": "signals", "reason": "GAP_TOO_LARGE"}
    {"schema": "console-realtime.v1", "type": "_control.error", "message": "..."}
    {"schema": "console-realtime.v1", "type": "_control.pong"}

Security (mission section 16): Origin is validated at the WebSocket handshake by the
`websockets` library's own `origins=` allowlist (same env-driven allowlist the REST server
already uses, `PLATFORM_API_CORS_ORIGINS`) - no unauthenticated arbitrary-origin subscription is
possible. This matches, rather than exceeds, the REST API's own current authorization posture:
neither this process nor `platform_api.signals`/`platform_api.control` implements bearer-token
or session authentication today (confirmed by reading `platform_api/signals.py`'s request
handler end to end - it checks Origin for CORS purposes only). Extending real authN/Z beyond
that existing REST posture is out of this mission's scope and is called out explicitly in the
handoff rather than silently assumed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from .realtime_envelope import RESOURCES, SCHEMA
from .realtime_hub import RealtimeHub
from .realtime_sources import BoundedChangePoller, NatsObservationSource, NatsSignalSource

log = logging.getLogger("platform_api.realtime")

DEFAULT_PORT = 22352


def _control(type_: str, **fields: Any) -> str:
    return json.dumps({"schema": SCHEMA, "type": type_, **fields}, separators=(",", ":"))


async def handle_connection(ws: Any, hub: RealtimeHub) -> None:
    connection_id = uuid.uuid4().hex
    handle = hub.register(connection_id)
    sender_task = asyncio.ensure_future(_send_loop(ws, handle))
    try:
        async for raw in ws:
            await _handle_client_message(ws, hub, handle, raw)
    except Exception:
        log.debug("realtime connection %s closed", connection_id, exc_info=True)
    finally:
        sender_task.cancel()
        hub.unregister(handle)


async def _send_loop(ws: Any, handle: Any) -> None:
    """One task per connection, draining ITS OWN bounded queue only - a slow consumer here never
    blocks `RealtimeHub.publish()` for any other connection (mission section 15)."""
    try:
        while True:
            message = await handle.queue.get()
            await ws.send(json.dumps(message, separators=(",", ":")))
    except asyncio.CancelledError:
        pass
    except Exception:
        log.debug("realtime send loop for connection %s ended", handle.connection_id, exc_info=True)


async def _handle_client_message(ws: Any, hub: RealtimeHub, handle: Any, raw: Any) -> None:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        await ws.send(_control("_control.error", message="invalid JSON"))
        return

    action = message.get("action")
    if action == "ping":
        await ws.send(_control("_control.pong"))
        return

    if action == "subscribe":
        resources = message.get("resources") or []
        since = message.get("since") or {}
        for resource in resources:
            if resource not in RESOURCES:
                await ws.send(_control("_control.error", message=f"unknown resource: {resource!r}"))
                continue
            current_sequence = hub.subscribe(handle, resource)
            resume_from = since.get(resource)
            if isinstance(resume_from, int) and resume_from < current_sequence:
                replay = hub.replay_since(resource, resume_from)
                if replay is None:
                    await ws.send(_control("_control.resync_required", resource=resource, reason="GAP_TOO_LARGE"))
                else:
                    for buffered in replay:
                        await ws.send(json.dumps(buffered, separators=(",", ":")))
            await ws.send(_control("_control.subscribed", resource=resource, sequence=current_sequence))
        return

    if action == "unsubscribe":
        for resource in message.get("resources") or []:
            hub.unsubscribe(handle, resource)
        return

    await ws.send(_control("_control.error", message=f"unknown action: {action!r}"))


async def _http_health(hub: RealtimeHub) -> dict[str, Any]:
    return {"status": "ok", "metrics": hub.metrics.snapshot()}


def build_process_request(hub: RealtimeHub):
    """Answers plain HTTP GETs (`/healthz`, `/readyz`, `/realtime/health`) without upgrading to
    WebSocket - readiness/liveness probes and section-18 observability never need a WS client."""
    from websockets.http11 import Response
    from websockets.datastructures import Headers

    async def process_request(connection: Any, request: Any):
        path = request.path.split("?", 1)[0]
        if path in ("/healthz", "/readyz"):
            body = b'{"status":"ok"}'
            return Response(200, "OK", Headers([("Content-Type", "application/json"),
                                                ("Content-Length", str(len(body)))]), body)
        if path == "/realtime/health":
            body = json.dumps(await _http_health(hub)).encode("utf-8")
            return Response(200, "OK", Headers([("Content-Type", "application/json"),
                                                ("Content-Length", str(len(body)))]), body)
        return None  # anything else proceeds to the normal WebSocket handshake

    return process_request


async def serve_forever(*, host: str, port: int, hub: RealtimeHub, allowed_origins: frozenset[str]) -> None:
    from websockets.asyncio.server import serve

    async def handler(ws: Any) -> None:
        await handle_connection(ws, hub)

    async with serve(handler, host, port, origins=allowed_origins or None,
                     process_request=build_process_request(hub), ping_interval=20, ping_timeout=20):
        await asyncio.Future()  # run forever


async def main_async() -> None:
    hub = RealtimeHub()

    host = os.getenv("PLATFORM_REALTIME_HOST", "0.0.0.0")
    port = int(os.getenv("PLATFORM_REALTIME_PORT", str(DEFAULT_PORT)))
    configured_origins = os.getenv("PLATFORM_API_CORS_ORIGINS", "https://console.stratrelay.app")
    allowed_origins = frozenset(v.strip() for v in configured_origins.split(",") if v.strip())

    nats_url = os.getenv("NATS_URL")
    if not nats_url:
        raise RuntimeError("NATS_URL is required for platform_api.realtime")

    import nats
    nc = await nats.connect(nats_url, user=os.getenv("REALTIME_NATS_USER") or os.getenv("P2_NATS_USER"),
                            password=os.getenv("REALTIME_NATS_PASSWORD") or os.getenv("P2_NATS_PASSWORD"),
                            name="platform-realtime-api", max_reconnect_attempts=-1, reconnect_time_wait=2)
    js = nc.jetstream()

    observation_source = NatsObservationSource(hub)
    signal_source = NatsSignalSource(hub)
    await observation_source.start(js)
    await signal_source.start(js)

    from postgres.db import connect
    from postgres.config import PostgresConfig

    pg_config = PostgresConfig.from_env()
    pg_config.require_explicit_target()
    pg = connect(pg_config, readonly=True)  # this process never writes anything

    def query_fn(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        with pg.cursor() as cur:
            cur.execute(sql, params)
            columns = [c.name if hasattr(c, "name") else c[0] for c in cur.description]
            return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    poller = BoundedChangePoller(hub, query_fn)
    poller_task = asyncio.ensure_future(poller.run_forever())

    log.warning("platform_api.realtime starting: host=%s port=%d allowed_origins=%s",
               host, port, sorted(allowed_origins))
    try:
        await serve_forever(host=host, port=port, hub=hub, allowed_origins=allowed_origins)
    finally:
        poller.stop()
        poller_task.cancel()
        await nc.drain()
        pg.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
