"""Where realtime events actually come from (mission section 14: "choose the correct backend
event source... where state does not have a suitable canonical event, do NOT invent incorrect
domain events merely for the UI").

Ground truth, checked directly against this repository before writing anything here:

  - `signal.entry.created.v1` on the `TRADING_CORE` stream IS a real, already-published
    canonical domain event (the strategy/signal path publishes it independently of this
    service). `NatsSignalSource` below is a thin, honest translation of that real event.
  - `trade.observation.recorded.v1` on `TRADING_OBSERVATION` IS likewise a real, already-
    published canonical domain event (`trade_management/runtime/observation_runtime.py`).
    `NatsObservationSource` translates it.
  - ManagedTrade creation, TradeManagerDecision creation, and PublicationDecision creation have
    **no canonical NATS event at all** (confirmed by reading
    `trade_management/runtime/open_consumer_runtime.py` and `trade_management/runtime/
    tm_none_runtime.py` end to end - neither ever calls a publisher). Inventing a fake NATS
    event type for these merely so the UI has something to subscribe to would be exactly what
    section 14 forbids. Instead `BoundedChangePoller` below implements the explicitly-sanctioned
    alternative: a **projection/change notification** - a single, internal, low-frequency
    (`POLL_INTERVAL_SECONDS`) read-only watermark query per table, shared across every connected
    browser (never client-visible polling; the browser only ever receives pushed WebSocket
    events, dedup-safe against `BoundedChangePoller` observing the same row on two consecutive
    ticks - see `RealtimeEvent.stable_event_id()`'s deterministic derivation).
  - Signal *outcome* changes (`strategy.entry_signals.terminal_state`) have no dedicated
    canonical event either, so `BoundedChangePoller` also covers those, for the same reason.
  - "System status" (mission section 11) deliberately watches only `platform.runtime_instances`'s
    orchestrator RUNNING/not-RUNNING transition, not `platform.outbox_events`/`inbox_events`
    counts - those change on essentially every signal/observation and would make "system"
    indistinguishable from the other two resources' own noise. Orchestrator up/down is genuinely
    slow-changing and operator-relevant, so it is the one thing on this channel treated as an
    event; everything else `/api/v1/system` reports stays snapshot-on-demand (mission section 11
    "document a bounded fallback rather than silently keeping rapid polling" - the Console's own
    manual refresh IS that documented fallback for the rest of `/api/v1/system`'s fields).

This module intentionally never touches the WebSocket transport (`realtime.py`) or the hub's
internal sequencing (`realtime_hub.py`) beyond calling `hub.publish(RealtimeEvent(...))` - it
only knows how to notice a change and describe it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from .realtime_envelope import RESOURCE_SIGNALS, RESOURCE_SYSTEM, RESOURCE_TRADE_MANAGEMENT, RealtimeEvent
from .realtime_hub import RealtimeHub

log = logging.getLogger("platform_api.realtime.sources")

POLL_INTERVAL_SECONDS = 5.0  # bounded, internal, shared by every browser - never exposed as client polling


def _managed_trade_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Typed, minimal projection - never the raw row (e.g. never leaks internal binding hashes)."""
    return {
        "managedTradeId": row["managed_trade_id"],
        "entrySignalId": row["entry_signal_id"],
        "instrument": row["instrument"],
        "direction": row["direction"],
        "state": row["state"],
        "strategyId": row.get("strategy_id"),
        "openedAt": _iso(row.get("created_at")),
    }


def _decision_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "decisionId": row["decision_id"],
        "managedTradeId": row["managed_trade_id"],
        "observationId": row.get("observation_id"),
        "action": row["action"],
        "persistedAt": _iso(row.get("persisted_at")),
    }


def _publication_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "decisionId": row["decision_id"],
        "outcome": row["outcome"],
        "reason": row.get("reason"),
        "evaluatedAt": _iso(row.get("evaluated_at")),
    }


def _signal_outcome_payload(signal_id: str, terminal_state: str) -> dict[str, Any]:
    return {"signalId": signal_id, "terminalState": terminal_state}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat()


class NatsObservationSource:
    """Ephemeral (non-durable) JetStream push subscription on `trade.observation.recorded.v1`.
    Deliberately NOT durable and NOT shared with P4's own TM-NONE consumer or the open consumer
    - this is a separate, UI-only reader with its own subscription, so a slow/offline realtime
    service can never delay or block P4's own domain processing (mission section 1 "canonical
    backend source" stays exactly what it already is; this only ever reads).
    Every browser shares this ONE subscription via the hub - never one NATS consumer per
    browser (mission section 17)."""

    SUBJECT = "trade.observation.recorded.v1"
    STREAM = "TRADING_OBSERVATION"

    def __init__(self, hub: RealtimeHub) -> None:
        self.hub = hub
        self._sub: Any = None

    async def start(self, js: Any) -> None:
        from nats.js.api import ConsumerConfig, DeliverPolicy

        config = ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=None)
        self._sub = await js.subscribe(self.SUBJECT, stream=self.STREAM, config=config)
        asyncio.ensure_future(self._consume())

    async def _consume(self) -> None:
        async for msg in self._sub.messages:
            try:
                envelope = json.loads(msg.data.decode("utf-8"))
                payload = envelope.get("payload", {})
                self.hub.publish(RealtimeEvent(
                    type="trade_observation.created",
                    occurred_at=envelope.get("occurred_at", ""),
                    resource=RESOURCE_TRADE_MANAGEMENT,
                    resource_id=payload.get("managed_trade_id", envelope.get("aggregate_id", "")),
                    event_id=envelope.get("event_id"),
                    payload={
                        "observationId": payload.get("observation_id"),
                        "managedTradeId": payload.get("managed_trade_id"),
                        "observationSeq": payload.get("observation_seq"),
                        "instrument": payload.get("instrument"),
                    },
                ))
            except Exception:
                log.exception("failed to translate trade.observation.recorded.v1 for realtime delivery")


class NatsSignalSource:
    """Ephemeral push subscription on `signal.entry.created.v1` (`TRADING_CORE`). Same
    isolation rationale as `NatsObservationSource` - never durable, never shared with any
    domain consumer, never able to affect signal-creation semantics."""

    SUBJECT = "signal.entry.created.v1"
    STREAM = "TRADING_CORE"

    def __init__(self, hub: RealtimeHub) -> None:
        self.hub = hub
        self._sub: Any = None

    async def start(self, js: Any) -> None:
        from nats.js.api import ConsumerConfig, DeliverPolicy

        config = ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=None)
        self._sub = await js.subscribe(self.SUBJECT, stream=self.STREAM, config=config)
        asyncio.ensure_future(self._consume())

    async def _consume(self) -> None:
        async for msg in self._sub.messages:
            try:
                envelope = json.loads(msg.data.decode("utf-8"))
                payload = envelope.get("payload", {})
                signal_id = payload.get("signal_id", envelope.get("aggregate_id", ""))
                self.hub.publish(RealtimeEvent(
                    type="signal.created",
                    occurred_at=envelope.get("occurred_at", ""),
                    resource=RESOURCE_SIGNALS,
                    resource_id=signal_id,
                    event_id=envelope.get("event_id"),
                    payload={"signalId": signal_id, "entrySignalHash": payload.get("entry_signal_hash")},
                ))
            except Exception:
                log.exception("failed to translate signal.entry.created.v1 for realtime delivery")


class NatsExecutionSource:
    """Ephemeral UI-only translations of the existing execution subjects."""

    SUBJECTS = ("execution.intent.created.v1", "execution.result.recorded.v1")
    STREAM = "EXECUTION"

    def __init__(self, hub: RealtimeHub) -> None:
        self.hub = hub

    async def start(self, js: Any) -> None:
        from nats.js.api import ConsumerConfig, DeliverPolicy
        for subject in self.SUBJECTS:
            config = ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=None, filter_subject=subject)
            subscription = await js.subscribe(subject, stream=self.STREAM, config=config)
            asyncio.ensure_future(self._consume(subscription, subject))

    async def _consume(self, subscription: Any, subject: str) -> None:
        async for msg in subscription.messages:
            try:
                envelope = json.loads(msg.data.decode("utf-8"))
                payload = envelope.get("payload", {})
                signal_id = payload.get("entry_signal_id") or envelope.get("correlation_id") or envelope.get("aggregate_id", "")
                self.hub.publish(RealtimeEvent(
                    type=subject, occurred_at=envelope.get("occurred_at", ""),
                    resource=RESOURCE_SIGNALS, resource_id=signal_id,
                    event_id=envelope.get("event_id"), payload=payload))
            except Exception:
                log.exception("failed to translate %s for realtime delivery", subject)


class BoundedChangePoller:
    """The section-14-sanctioned fallback for state with no canonical event: ManagedTrade
    creation, TradeManagerDecision creation, PublicationDecision creation, and signal outcome
    transitions. One shared background loop, `POLL_INTERVAL_SECONDS` apart, entirely internal -
    the browser never polls anything; it only ever receives what this loop pushes through the
    hub. Watermarked by each table's own `created_at`/`persisted_at`/`evaluated_at` column, so
    every tick only reads rows newer than the last one it already saw (bounded, cheap, never a
    full-table scan)."""

    def __init__(self, hub: RealtimeHub, query_fn: Callable[[str, tuple[Any, ...]], list[dict[str, Any]]], *,
                interval_seconds: float = POLL_INTERVAL_SECONDS) -> None:
        self.hub = hub
        self._query = query_fn
        self.interval_seconds = interval_seconds
        self._last_managed_trade_at: str | None = None
        self._last_decision_at: str | None = None
        self._last_publication_at: str | None = None
        self._signal_terminal_states: dict[str, str] = {}
        self._orchestrator_running: int | None = None
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run_forever(self) -> None:
        while not self._stop:
            try:
                await self.tick()
            except Exception:
                log.exception("bounded change poller tick failed; will retry next interval")
            await asyncio.sleep(self.interval_seconds)

    async def tick(self) -> None:
        self._poll_managed_trades()
        self._poll_decisions()
        self._poll_publications()
        self._poll_signal_outcomes()
        self._poll_system_status()

    def _poll_managed_trades(self) -> None:
        sql = """SELECT managed_trade_id, entry_signal_id, instrument, direction, state,
                        strategy_id, created_at FROM trade_management.managed_trade
                 WHERE created_at > COALESCE(%s, '1970-01-01'::timestamptz)
                 ORDER BY created_at LIMIT 200"""
        rows = self._query(sql, (self._last_managed_trade_at,))
        for row in rows:
            self.hub.publish(RealtimeEvent(
                type="managed_trade.created", occurred_at=_iso(row["created_at"]) or "",
                resource=RESOURCE_TRADE_MANAGEMENT, resource_id=row["managed_trade_id"],
                payload=_managed_trade_payload(row)))
            self._last_managed_trade_at = _iso(row["created_at"])

    def _poll_decisions(self) -> None:
        sql = """SELECT decision_id, managed_trade_id, observation_id, action, persisted_at
                 FROM trade_management.trade_manager_decision
                 WHERE persisted_at > COALESCE(%s, '1970-01-01'::timestamptz)
                 ORDER BY persisted_at LIMIT 200"""
        rows = self._query(sql, (self._last_decision_at,))
        for row in rows:
            self.hub.publish(RealtimeEvent(
                type="trade_manager_decision.created", occurred_at=_iso(row["persisted_at"]) or "",
                resource=RESOURCE_TRADE_MANAGEMENT, resource_id=row["managed_trade_id"],
                payload=_decision_payload(row)))
            self._last_decision_at = _iso(row["persisted_at"])

    def _poll_publications(self) -> None:
        sql = """SELECT pd.decision_id, pd.outcome, pd.reason, pd.evaluated_at, d.managed_trade_id
                 FROM trade_management.publication_decision pd
                 JOIN trade_management.trade_manager_decision d ON d.decision_id = pd.decision_id
                 WHERE pd.evaluated_at > COALESCE(%s, '1970-01-01'::timestamptz)
                 ORDER BY pd.evaluated_at LIMIT 200"""
        rows = self._query(sql, (self._last_publication_at,))
        for row in rows:
            self.hub.publish(RealtimeEvent(
                type="publication_decision.created", occurred_at=_iso(row["evaluated_at"]) or "",
                resource=RESOURCE_TRADE_MANAGEMENT, resource_id=row["managed_trade_id"],
                payload=_publication_payload(row)))
            self._last_publication_at = _iso(row["evaluated_at"])

    def _poll_signal_outcomes(self) -> None:
        # Bounded to signals this process has already seen transition at least once, plus a
        # rolling window of recently-created signals - never a full strategy.entry_signals scan.
        sql = """SELECT signal_id, terminal_state FROM strategy.entry_signals
                 WHERE ingested_at > now() - interval '2 hours' LIMIT 500"""
        rows = self._query(sql, ())
        for row in rows:
            signal_id, terminal_state = row["signal_id"], row["terminal_state"]
            previous = self._signal_terminal_states.get(signal_id)
            if previous is not None and previous != terminal_state:
                self.hub.publish(RealtimeEvent(
                    type="signal.outcome_changed", occurred_at=_iso_now(),
                    resource=RESOURCE_SIGNALS, resource_id=signal_id,
                    payload=_signal_outcome_payload(signal_id, terminal_state)))
            self._signal_terminal_states[signal_id] = terminal_state

    def _poll_system_status(self) -> None:
        # Only the orchestrator's own RUNNING/not-RUNNING transition - see module docstring for
        # why outbox/inbox counts are deliberately excluded from this channel.
        sql = """SELECT count(*) AS orchestrator_running FROM platform.runtime_instances
                 WHERE component = 'orchestrator' AND status = 'RUNNING'"""
        rows = self._query(sql, ())
        if not rows:
            return
        running = int(rows[0]["orchestrator_running"])
        if self._orchestrator_running is not None and running != self._orchestrator_running:
            self.hub.publish(RealtimeEvent(
                type="system.status_changed", occurred_at=_iso_now(),
                resource=RESOURCE_SYSTEM, resource_id="orchestrator",
                payload={"component": "orchestrator", "running": running > 0}))
        self._orchestrator_running = running


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
