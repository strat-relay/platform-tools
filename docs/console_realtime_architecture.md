# Console realtime WebSocket architecture

Mission `CLAUDE-V1.3-CONSOLE-REALTIME-WEBSOCKET-ARCHITECTURE`. Backend:
`trading-platform` commit `c97b4ff` (branch `console-realtime-websocket-architecture`, based on
`6c4f55e`, the exact commit that was live for `platform-signals-api`/`platform-control-api` at
mission start). Frontend: `trading-ops-console` commit `ca07336` (same branch name, based on
`main`@`9394851`). Both deployed and verified live in production.

## 1. Audit: Console data sources at mission start

| Source | REST endpoint | Polling before this mission | Manual refresh | Canonical backend | Classification | After this mission |
|---|---|---|---|---|---|---|
| Signals | `/api/v1/signals` | Wired but disabled (`REFRESH_INTERVALS_MS.signals = null`) | Yes | `strategy.entry_signals` (PostgreSQL) | REALTIME (real event exists) | **REALTIME** via `signal.entry.created.v1` |
| Signal outcomes | (same, `terminal_state` field) | none | Yes (page refetch) | `strategy.entry_signals.terminal_state` | REALTIME (no event, needs bounded refresh) | **REALTIME** via bounded poll → `signal.outcome_changed` |
| System status | `/api/v1/system` | Wired but disabled | Yes | `PlatformControlRepository.platform_status()` | SNAPSHOT_ONLY + one REALTIME sub-field | Orchestrator up/down → **REALTIME** (`system.status_changed`); rest stays SNAPSHOT_ONLY (manual refresh) |
| Safety | `/api/v1/safety` | Wired but disabled | Yes | execution/orchestrator config | SNAPSHOT_ONLY | Unchanged (no event source; re-fetched alongside `system` on push - see below) |
| Strategies | `/api/v1/strategies` | Disabled | Yes | strategy config + evaluations | SNAPSHOT_ONLY | Unchanged - out of scope this pass (no canonical event; would need its own bounded-refresh design, not attempted here to keep this change reviewable) |
| Events | `/api/v1/events` | Disabled | Yes | `platform.outbox_events`/`inbox_events` | POLLING_FALLBACK_REQUIRED (too high-frequency for a "resource" channel without dedicated design) | Unchanged this pass |
| Audit | `/api/v1/audit` | Disabled | Yes | audit log | SNAPSHOT_ONLY | Unchanged |
| Execution (`/execution/*`) | `/api/v1/executions`, `/api/v1/executions/metrics` | Disabled | Yes | `execution.intents`/`execution.results` | SNAPSHOT_ONLY | Unchanged - execution/risk is explicitly out of scope for this mission |
| Broker account/positions | `/api/v1/broker/*` | N/A (never called - short-circuits client-side) | N/A | permanently `503 SOURCE_UNAVAILABLE` | UNAVAILABLE_CAPABILITY | Unchanged (correctly modeled already; see section 12 below) |
| Reports | `/api/v1/reports` | Disabled | Yes | permanently `503` (no registry) | UNAVAILABLE_CAPABILITY | Unchanged |
| Connections | `/api/v1/connections` | Wired but disabled | Yes | static config | MANUAL_REFRESH | Unchanged - no realtime source exists or was invented |
| ManagedTrades | `/api/v1/managed-trades` | Disabled | Yes | `trade_management.managed_trade` | REALTIME (no event, needs bounded refresh) | **REALTIME** via bounded poll → `managed_trade.created` |
| TradeObservations | (embedded in managed-trade detail) | Disabled | Yes | `trade_management.trade_observation` | REALTIME (real event exists) | **REALTIME** via `trade.observation.recorded.v1` |
| TradeManagerDecisions | `/api/v1/managed-trades/:id/decisions` | Disabled | Yes | `trade_management.trade_manager_decision` | REALTIME (no event, needs bounded refresh) | **REALTIME** via bounded poll → `trade_manager_decision.created` |
| PublicationDecisions | (embedded) | Disabled | Yes | `trade_management.publication_decision` | REALTIME (no event, needs bounded refresh) | **REALTIME** via bounded poll → `publication_decision.created` |
| ENTRY_ONLY outcomes | (part of Signals) | Disabled | Yes | `strategy.entry_signals` | covered by "Signal outcomes" above | covered above |

Every endpoint in the mission's "at minimum inspect" list was checked directly against its
actual route handler (`platform_api/control.py`, `platform_api/signals.py`) - not assumed from
naming.

## 2-3. Architecture and transport decision

```
NATS JetStream (TRADING_CORE, TRADING_OBSERVATION)   PostgreSQL (read-only)
              \                                          /
               v                                        v
        platform_api.realtime  (new process, its own pod: platform-realtime-api)
          - NatsSignalSource / NatsObservationSource: ephemeral, ONE consumer each,
            never durable, never shared with any P4/signal domain consumer
          - BoundedChangePoller: internal 5s watermark queries, shared by every browser
          - RealtimeHub: per-resource sequence + ring buffer + per-connection bounded queue
                                        |
                                        v
                         authenticated (Origin-checked) WebSocket
                          wss://api.stratrelay.app/api/v1/realtime
                     (platform-api-router nginx proxies the upgrade;
                      cloudflared/Cloudflare Tunnel terminates TLS as it
                      already does for every other /api/v1/* route)
                                        |
                                        v
                      Console: src/realtime/RealtimeClient.ts
                       (one socket, multiplexed per-resource subscriptions)
                                        |
                    +-------------------+-------------------+
                    v                   v                   v
              SignalsPage      TradeManagerPage      AppStatusContext
           (debounced refetch)  (debounced refetch)   (push-triggered
                                                        bounded refetch)
```

**REALTIME_TRANSPORT=WebSocket (RFC 6455), native, no socket.io.**
**BACKEND_IMPLEMENTATION=`websockets` (Python, asyncio) — a small, focused, widely-used RFC 6455
implementation, not an application framework. The existing stack (`platform_api`) is a bare
stdlib `http.server.ThreadingHTTPServer` with no async/WebSocket support at all, so there was no
"already-supported" abstraction to prefer over this. Hand-rolling the WebSocket handshake/frame
masking/ping-pong ourselves was considered and rejected: that is exactly the kind of
security/correctness-sensitive protocol code a small, audited library does far more safely.**
**FRONTEND_IMPLEMENTATION=native browser `WebSocket`, wrapped in a hand-written client
(`src/realtime/RealtimeClient.ts`) — no new npm dependency; the existing stack has no
"already-supported" realtime abstraction either (no react-query/SWR/socket.io present).**

The browser never touches NATS: `platform_api.realtime` is the only thing that ever calls
`nats.connect(...)`, and its own JetStream subscriptions are ephemeral, read-only, and separate
from every domain consumer (`trade_management/runtime/*`, the signal path) - a slow or
disconnected realtime service can never delay or block real domain processing, and vice versa.

## 4. Event contract

Schema `console-realtime.v1`, defined in `platform_api/realtime_envelope.py` (backend) and
mirrored by hand in `src/realtime/types.ts` (frontend - no shared schema package between the two
repos, so these two files must be kept in sync manually on any future change):

```json
{
  "schema": "console-realtime.v1",
  "eventId": "evt-...",
  "type": "trade_observation.created",
  "occurredAt": "2026-09-23T09:59:40.062Z",
  "resource": "trade-management",
  "resourceId": "MT_...",
  "sequence": 5743,
  "payload": { "...": "typed, minimal, resource-specific" }
}
```

Supported `type` values (exhaustive, see `EVENT_TYPES` in `realtime_envelope.py`):
`signal.created`, `signal.outcome_changed`, `managed_trade.created`, `trade_observation.created`,
`trade_manager_decision.created`, `publication_decision.created`, `system.status_changed`.

`eventId` is stable: for a NATS-sourced event it is the domain event's own `event_id`; for a
bounded-refresh-sourced event it is a deterministic hash of `(resourceId, type, occurredAt)`, so
observing the same underlying change twice never mints two different ids.

Every `payload` is hand-built from a typed projection of the source row/event - never a raw
database row, never a raw NATS envelope dump (see each builder function in
`realtime_sources.py`).

## 5. Snapshot + stream consistency

**Chosen strategy: connect + subscribe + snapshot + reconcile**, one of the mission's explicitly
acceptable approaches (`platform_api/realtime_hub.py::subscribe()`'s docstring has the full
reasoning). On `subscribe`, the server replies with the resource's CURRENT sequence number *at
that instant* (`_control.subscribed {resource, sequence: N}`). The client then fetches its REST
snapshot. Because a sequence is only ever assigned once the underlying change is already durably
committed at its source of truth, the snapshot is guaranteed to reflect everything up through
sequence N. The client applies the snapshot, then replays every buffered/live event via an
**idempotent upsert-by-resourceId merge** (not append) - so an event whose data the snapshot
already contains is a harmless no-op overwrite, not a duplicate. This sidesteps needing exact
non-overlapping boundaries between "in the snapshot" and "arrived after": over-applying is
always safe; under-applying (dropping an event) is the only failure mode that matters, and the
hub's ring buffer + resync protocol (section 6) is what prevents that.

In this pass, "reconcile" concretely means "trigger the same REST refetch the page already had"
(`useDebouncedCallback`-wrapped `refetch()`), not a hand-written per-field patch - see section 9.

## 6-7. Reconnect / resume / duplicate handling

`src/realtime/RealtimeClient.ts`: states `CONNECTING → CONNECTED`, `CONNECTED → RECONNECTING`
on any close/error, escalating to `DEGRADED` after 3 consecutive failures (still retrying -
DEGRADED is a severity signal, not a stop), back to `CONNECTED` on the next successful open.
Bounded exponential backoff with full jitter (`500ms * 2^(failures-1)`, capped at 30s, uniform
jitter over `[0, delay]`). `disconnect()` is the only terminal path to `DISCONNECTED`.

On reconnect, every previously-subscribed resource is re-subscribed with `since: {resource:
lastSeenSequence}`. The hub's ring buffer (500 events/resource) either replays exactly what was
missed, or - if the gap exceeds the buffer, e.g. after a server restart or a long disconnect -
responds `_control.resync_required`, which the client forwards to whichever page's
`onResync` handler is registered, and that page re-fetches its REST snapshot (never silently
assumes nothing happened).

Duplicate delivery is assumed, not prevented at the protocol level: the client drops an exact
`eventId` repeat, and separately drops anything at or below the last-applied sequence for that
resource (covers reordered/duplicate resume replay). Proven in
`src/realtime/RealtimeClient.test.ts` against a real (fake-socket) client, not just asserted.

## 8. Subscriptions

Three resources only: `signals`, `trade-management`, `system` - deliberately coarse (all four
Trade Manager sub-resources share one channel, distinguished by `type`) rather than one
subscription per entity, keeping the protocol bounded and simple. `subscribe`/`unsubscribe` are
reference-counted client-side (`RealtimeClient.subscribe()`): two components on the same page
both watching `trade-management` share one server-side subscription.

## 9-10. Signals and Trade Manager

Both pages call `useRealtimeResource(resource, onEvent, onResync)` and, on any relevant event
type, call a 400ms-debounced `refetch()` of their existing `useApiQuery`-managed REST call - a
burst of N realtime events collapses into at most one REST round-trip, never N. This was chosen
over a hand-written client-side patch because both endpoints already own filtering/pagination
server-side correctly; re-asking them is simpler and cannot drift from what the server would
actually return for the current filter/page.

## 11. System / safety / connection status

Deliberately narrow: only the orchestrator's own RUNNING/not-RUNNING transition is realtime
(`_poll_system_status` in `realtime_sources.py`) - `platform.outbox_events`/`inbox_events` counts
were explicitly excluded because they change on nearly every signal/observation and would make
"system" indistinguishable noise. `/api/v1/safety` and the rest of `/api/v1/system` remain
snapshot-on-demand (mission section 11's own "document a bounded fallback" - manual refresh IS
that fallback here; there is no separate bounded interval poller left running for it).

**Realtime connection health is a field on `AppStatus` (`realtime: RealtimeStatus`), computed at
render time from `useRealtimeStatus()`, structurally independent from `degradedSources`/
`connection`/`executionEngineStatus` (which come only from the REST `/api/v1/system` fetch).**
Proven in `src/realtime/broker-isolation.test.ts` that these two sources can never be conflated,
and that `BrokerPage.tsx` cannot even import the realtime module - so a WebSocket reconnect can
never reset the rest of the Console, and a broker-unavailable response can never affect the
realtime connection.

## 12. Broker capability unavailable

Already correctly modeled before this mission: `BrokerPage.tsx` never calls the network for
broker data at all - it short-circuits with a client-side `CapabilityUnavailableError`, which
`useApiQuery` classifies as `errorKind: "unavailable"` and renders via `UnavailableFeatureState`,
never as a hard error or a stream reset. This mission's contribution was verifying (not
re-fixing) this isolation, and structurally proving it can never regress
(`broker-isolation.test.ts`).

## 13. Polling removal

```
POLLERS_BEFORE=0 active (all REFRESH_INTERVALS_MS entries were already wired to null by an
  earlier, separate commit - "Disable automatic Console polling" - before this mission started;
  confirmed by reading every setInterval/refreshIntervalMs reference in the Console source)
POLLERS_REMOVED=0 (nothing left ACTIVE to remove; the null-wired dead code for overview/safety
  was replaced with a genuine realtime push instead of being deleted outright)
POLLERS_REMAINING=0 intervals; 7 explicit MANUAL_REFRESH-only resources
REMAINING_POLLER_JUSTIFICATIONS=[
  "connections: no canonical realtime source; static config, changes only on redeploy",
  "brokerPositions/brokerExposure: permanently 503 (capability unavailable), polling a dead
   endpoint would be pure waste - manual refresh only",
  "reports: permanently 503 (no canonical registry) - same reasoning",
  "executions/strategies/events/audit: have a canonical backend but no canonical NATS event and
   were not given a bounded poller in this pass, to keep the change reviewable and scoped to the
   resources the mission named explicitly (Signals, Trade Manager, system status) - a real,
   disclosed limitation, not an oversight (see 'Deferred' below)",
]
```

## 14. Backend event source honesty

Documented exhaustively in `platform_api/realtime_sources.py`'s module docstring: exactly two
canonical NATS events exist (`signal.entry.created.v1`, `trade.observation.recorded.v1`),
confirmed by reading every publisher call site end to end, not assumed. Everything else realtime
covers uses the sanctioned "projection/change notification" alternative
(`BoundedChangePoller`), never an invented domain event.

## 15. Backpressure

`RealtimeHub`: bounded (500) per-connection `asyncio.Queue`; a full queue drops for that
connection only (`ConnectionHandle.offer()` returns False, hub increments
`events_dropped_total`, logs once) - `publish()` never blocks, never raises, regardless of how
many connections are behind. Proven in `test_platform_realtime_hub.py`'s
`BackpressureTests` that one slow connection's full queue never affects a healthy connection's
own delivery. No coalescing beyond the bounded-poller's own per-tick de-duplication in this
pass - a real limitation if trade observation volume grows much higher (see "Deferred" below).

## 16. Security

`AUTHORIZATION_ENFORCED=false` beyond Origin. This matches, not exceeds, the existing REST
posture: `platform_api/signals.py`'s `create_server()` was read end to end and implements CORS
Origin-allowlist checking only - no bearer token, no session, no API key anywhere in the current
REST server. The realtime WebSocket handshake enforces the identical origin allowlist
(`websockets.serve(..., origins=...)`, same `PLATFORM_API_CORS_ORIGINS` env var). Extending real
authN/Z beyond this existing posture was explicitly out of scope for this mission and is flagged
here rather than silently assumed solved.

`ORIGIN_VALIDATION=true` (enforced at the WebSocket handshake by the `websockets` library
itself, `test_unknown_origin_is_rejected_at_the_handshake` proves a non-allowlisted Origin is
refused before any data is exchanged).

`SENSITIVE_DATA_EXPOSED=false`: no NATS credentials, no PostgreSQL credentials, no broker
credentials, no execution fencing secret, no raw internal event payload is ever sent to the
browser - every payload is a hand-built, typed, minimal projection (section 4).

## 17. Multiple tabs/clients

`RealtimeHub` is the single shared fan-out point per server process; `NatsObservationSource`/
`NatsSignalSource` each open exactly ONE ephemeral JetStream subscription regardless of how many
browsers are connected. `BoundedChangePoller` runs exactly once per process, shared. A
disconnected browser's `ConnectionHandle` is simply unregistered
(`RealtimeHub.unregister()`) - it never creates or destroys any NATS/Postgres resource, and
cannot affect any other connection.

## 18. Observability

`HubMetrics` (`platform_api/realtime_hub.py`): `connections_active`, `connections_total`,
`disconnections_total`, `events_published_total`, `events_delivered_total`,
`events_dropped_total`, `resyncs_required_total`, `slow_consumer_disconnects_total` (tracked;
disconnect-on-persistent-backpressure itself is not yet implemented - see "Deferred"). Exposed
read-only, unauthenticated-but-non-sensitive, at `GET /realtime/health` (no WS upgrade needed).
Verified live: `{"status": "ok", "metrics": {"connections_active": 0, ...,
"events_published_total": 2887, ...}}` minutes after deployment, against real production
traffic. Frontend: `useRealtimeStatus()` exposes `state`/`consecutiveFailures`/`lastConnectedAt`/
`lastError` - not yet surfaced in a dedicated UI element (TopBar/StatusBadge integration was not
done in this pass; the data is available via `useAppStatus().realtime` for a future page to
render). Logging is per-connect/disconnect/drop, never per-message.

## 19. Tests

Backend (`trading-platform`, `tests/test_platform_realtime_*.py`, 40 tests): hub sequencing/
resume/gap-detection/backpressure-isolation, source translation honesty (NATS events translated
correctly; bounded-poller change detection; the same underlying change never gets two different
derived eventIds), a real end-to-end `websockets` client/server pair (subscribe/resume/resync/
unknown-origin-rejected/health-endpoint/ping-pong/unknown-action-handled), system-status
transition detection. Frontend (`trading-ops-console`, 19 tests): connect/dedup/backoff/DEGRADED
escalation (caught and fixed a real bug)/resume/resync/unsubscribe against a fake WebSocket +
fake timers, plus the broker-isolation architecture proof.

**Explicitly not covered by an automated test in this pass** (mission section 19's checklist,
honestly enumerated): "unauthorized WebSocket connection rejected" IS covered (backend). "manual
refresh still works where provided" was not re-tested (pre-existing, unmodified `useApiQuery`
behavior - out of scope to re-prove). "no redundant REST polling remains for migrated resources"
is proven structurally (section 13's grep-based accounting) rather than by a dedicated runtime
test. Live production verification (a real WebSocket client receiving a real
`trade_manager_decision.created` event within seconds of connecting to
`wss://api.stratrelay.app/api/v1/realtime`) stands in for an automated "new signal appears
without polling" / "Trade Manager events update relevant Console state" integration test against
the deployed system - this was proven live rather than in an automated CI-style test, and should
be treated as level-2 (real-system-isolated-verification) proof, not a repeatable automated test.

## Deployment

- `platform-realtime-api`: new Deployment/Service (`deploy/platform_realtime_api/`), `Recreate`
  strategy (the namespace's `trading-conservative` ResourceQuota cannot admit a RollingUpdate
  surge pod - same constraint documented in the P4 broker-symbol-fix mission). New
  NetworkPolicies mirroring the existing `trade-management-*` pattern exactly.
- `platform-api-router`: rebuilt with the new `/api/v1/realtime` WebSocket-upgrade proxy
  location; its egress policy extended to reach `platform-realtime-api:22352`.
- `trading-ops-console`: rebuilt with the realtime client baked into the production bundle
  (confirmed by grepping the served JS for `wss:`/`/realtime`/`_control.subscribed` post-deploy).
- Registry push used `host.docker.internal:5001` (not `localhost:5001`, which silently lands in
  Docker Desktop's own unrelated local registry from this Mac - see memory
  `registry-push-host-docker-internal`), deploy references use `localhost:5001` (what the k3s
  node's containerd actually resolves).

## Deferred / known limitations (disclosed, not hidden)

- No coalescing beyond the bounded-poller's natural batching; if trade-observation volume grows
  substantially, per-event (not just per-drop) coalescing may become worth adding.
- No disconnect-on-persistent-backpressure: a chronically slow connection keeps dropping events
  forever rather than eventually being disconnected and told to resync. The metric exists
  (`slow_consumer_disconnects_total`) but the behavior it names is not yet implemented.
- Realtime connection status is exposed via `useAppStatus().realtime` but has no dedicated UI
  element yet (e.g. a TopBar indicator) - the data plumbing is complete; the visual is not.
  This does not fabricate a claim: `RECONNECT_IMPLEMENTED=true` describes the client's own
  behavior, not a UI affordance.
- Strategies/Events/Audit/Executions were audited (section 1) and explicitly left
  REST-snapshot-only - no bounded poller was added for them, to keep this change's blast radius
  matched to what the mission named explicitly.
