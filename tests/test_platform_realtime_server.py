"""End-to-end proof against a REAL `websockets` server/client pair (mission sections 6, 8, 16,
19 "unauthorized WebSocket connection rejected", "subscription filtering works"). No fakes for
the transport itself - only the NATS/Postgres sources are out of scope here (covered by
test_platform_realtime_sources.py); this proves the actual wire protocol."""
from __future__ import annotations

import asyncio
import json
import unittest

from platform_api.realtime import serve_forever
from platform_api.realtime_envelope import RESOURCE_SIGNALS, RESOURCE_TRADE_MANAGEMENT, RealtimeEvent
from platform_api.realtime_hub import RealtimeHub

ALLOWED_ORIGIN = "https://console.stratrelay.app"


async def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RealtimeServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = RealtimeHub()
        self.port = await _free_port()
        self.server_task = asyncio.ensure_future(
            serve_forever(host="127.0.0.1", port=self.port, hub=self.hub,
                         allowed_origins=frozenset({ALLOWED_ORIGIN})))
        await asyncio.sleep(0.2)  # let the listener bind

    async def asyncTearDown(self) -> None:
        self.server_task.cancel()
        try:
            await self.server_task
        except (asyncio.CancelledError, Exception):
            pass

    async def _connect(self, *, origin: str | None = ALLOWED_ORIGIN):
        import websockets
        return await websockets.connect(f"ws://127.0.0.1:{self.port}", origin=origin)

    async def test_subscribe_ack_carries_the_current_sequence(self):
        self.hub.publish(RealtimeEvent(type="signal.created", occurred_at="t", resource=RESOURCE_SIGNALS,
                                       resource_id="SIG_1", payload={}))
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "subscribe", "resources": ["signals"]}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            self.assertEqual(ack["type"], "_control.subscribed")
            self.assertEqual(ack["resource"], "signals")
            self.assertEqual(ack["sequence"], 1)
        finally:
            await ws.close()

    async def test_a_published_event_after_subscribe_is_delivered_live(self):
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "subscribe", "resources": ["signals"]}))
            await asyncio.wait_for(ws.recv(), timeout=2)  # ack

            self.hub.publish(RealtimeEvent(type="signal.created", occurred_at="t", resource=RESOURCE_SIGNALS,
                                           resource_id="SIG_LIVE", payload={"signalId": "SIG_LIVE"}))
            delivered = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            self.assertEqual(delivered["type"], "signal.created")
            self.assertEqual(delivered["resourceId"], "SIG_LIVE")
        finally:
            await ws.close()

    async def test_unsubscribed_resource_never_delivers_events(self):
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "subscribe", "resources": ["signals"]}))
            await asyncio.wait_for(ws.recv(), timeout=2)

            self.hub.publish(RealtimeEvent(type="trade_observation.created", occurred_at="t",
                                           resource=RESOURCE_TRADE_MANAGEMENT, resource_id="MT_1", payload={}))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)
        finally:
            await ws.close()

    async def test_unsubscribe_stops_further_delivery(self):
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "subscribe", "resources": ["signals"]}))
            await asyncio.wait_for(ws.recv(), timeout=2)
            await ws.send(json.dumps({"action": "unsubscribe", "resources": ["signals"]}))
            await asyncio.sleep(0.1)

            self.hub.publish(RealtimeEvent(type="signal.created", occurred_at="t", resource=RESOURCE_SIGNALS,
                                           resource_id="SIG_X", payload={}))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.3)
        finally:
            await ws.close()

    async def test_resume_since_a_sequence_still_in_the_buffer_replays_missed_events(self):
        self.hub.publish(RealtimeEvent(type="signal.created", occurred_at="t", resource=RESOURCE_SIGNALS,
                                       resource_id="SIG_1", payload={}))
        self.hub.publish(RealtimeEvent(type="signal.created", occurred_at="t", resource=RESOURCE_SIGNALS,
                                       resource_id="SIG_2", payload={}))
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "subscribe", "resources": ["signals"], "since": {"signals": 1}}))
            replayed = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            self.assertEqual(replayed["resourceId"], "SIG_2")
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            self.assertEqual(ack["type"], "_control.subscribed")
        finally:
            await ws.close()

    async def test_resume_since_a_sequence_older_than_the_buffer_requests_resync(self):
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "subscribe", "resources": ["signals"], "since": {"signals": -100}}))
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            self.assertEqual(first["type"], "_control.resync_required")
        finally:
            await ws.close()

    async def test_unknown_origin_is_rejected_at_the_handshake(self):
        import websockets
        with self.assertRaises(Exception):
            await websockets.connect(f"ws://127.0.0.1:{self.port}", origin="https://evil.example")

    async def test_health_endpoint_answers_plain_http_without_upgrading(self):
        import urllib.request

        def fetch() -> dict:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/realtime/health", timeout=2) as resp:
                return json.loads(resp.read())

        # Off the event loop: the server needs its own loop free to service this request.
        body = await asyncio.to_thread(fetch)
        self.assertEqual(body["status"], "ok")
        self.assertIn("connections_active", body["metrics"])

    async def test_unknown_action_gets_a_control_error_not_a_crash(self):
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "bogus"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            self.assertEqual(reply["type"], "_control.error")
        finally:
            await ws.close()

    async def test_ping_gets_a_pong(self):
        ws = await self._connect()
        try:
            await ws.send(json.dumps({"action": "ping"}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
            self.assertEqual(reply["type"], "_control.pong")
        finally:
            await ws.close()


if __name__ == "__main__":
    unittest.main()
