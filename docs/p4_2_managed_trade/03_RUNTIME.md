# P4.2 production runtime wiring (architecture/p4-2-runtime)

Branch: `architecture/p4-2-runtime` · Worktree: `trading-platform-p4-2-runtime` ·
Source commit: `f76b817327b38fdfdf1fa0f6594dab477678abcc` (the merge of Codex's Platform Signal
API router work and the `architecture/p4-2-managed-trade` integration).

Implementation/preparation only. `deploy/trade_management/workload.yaml` is a `replicas: 0`
prepared Deployment (matching `deploy/canonical_platform/outbox-relay-prepared.yaml`'s existing
convention) - nothing in this branch is applied to the live cluster. `EXECUTION_AUTHORITY_MODE`
remains `DISABLED`; `BROKER_WRITES=0`.

## What this adds on top of `docs/p4_2_managed_trade/00_README.md`/`02_INTEGRATION.md`

The prior branches implemented and integrated the domain logic (`trade_management/`) and proved
its schema against real PostgreSQL. Nothing could actually *run* yet - there was no JetStream
subscriber, no live market-data adapter, no scheduler, no entrypoint. This branch is that
missing wiring, entirely inside a new `trade_management/runtime/` subpackage (the one place
allowed to import `nats` and `contracts.mt5_bridge.Mt5ReadClient` - the pure domain layer in
`trade_management/*.py` still imports neither, enforced by
`tests/test_trade_management_isolation.py`; `runtime/`'s own, narrower isolation rules are
enforced by `tests/test_trade_management_runtime_isolation.py`).

## Ship-first decisions and how each is implemented

**1. No backfill; explicit activation boundary.** The 9 canonical EntrySignals that existed
before this runtime's first start are never replayed. The actual enforcement mechanism is
`nats.js.api.DeliverPolicy.NEW`, used only the *first* time the `trade-mgmt-open` durable
consumer is created (`trade_management/runtime/open_consumer_runtime.py::bootstrap_open_consumer`)
- JetStream itself never delivers anything already in the stream at that instant. A second
bootstrap call (e.g. after a restart) detects the consumer already exists and never recreates
it, so the boundary can never silently move forward. `trade_management/runtime/activation.py`
persists an auditable record of when and against what stream state the boundary was established
(`platform.system_metadata`, key `trade_management.p4_activation_boundary`), first-write-wins
(`ON CONFLICT DO NOTHING`) so a redeploy can never overwrite it with a later value.

**2. TRADING_CORE unchanged.** `infrastructure/messaging/contracts.py`'s `TRADING_CORE` stream
subject derivation is now byte-identical to before P4.2 ever touched it (`x.startswith(("strategy.",
"signal."))`, no `trade.*` exception). `trade.opened.v1` and `trade.decision.made.v1` remain
registered as valid subjects (so `EventEnvelope` construction / `platform.outbox_events` inserts
in `managed_trade.py`/`tm_none.py` still work unmodified) but are assigned to **no** stream -
nothing in this vertical slice consumes either, so nothing relays them to JetStream yet; they
stay durably recorded in PostgreSQL as an audit trail only (mission section 5's "avoid
publishing them if they are not required" option, chosen over inventing a second P4 stream for
events nothing reads). `trade_management/runtime/streams.py::verify_trading_core_unchanged` is
read-only (`stream_info` only) and fails closed if `TRADING_CORE` is unreachable - it never
calls `add_stream` for it, even defensively.

**3. Retention.** `TRADING_OBSERVATION` keeps its existing 7-day `max_age` (set when the stream
was first defined, unchanged by this branch). Recorded as ship-first, not final, in
`docs/engineering/OPTIMIZATION_REGISTER.md` #29.

**4. Market data: 22347 only.** `trade_management/runtime/market_data_live.py::LiveMarketDataProvider`
wraps `contracts.mt5_bridge.Mt5ReadClient` (the same sanctioned read-only client `paper_runner.py`
already uses in production), calling only `.quote()`/`.rates()` - the client itself hard-refuses
any tool outside `READ_TOOLS` at the transport layer. `build_bridge_client()` refuses to
construct a client pointed at `:22348` (`RuntimeConfigError`/`ValueError`, both tested).

**5. One runtime workload.** `trade_management/runtime/service.py` runs the ManagedTrade opening
consumer, the observation scheduler loop, and the TM-NONE decision consumer as three concurrent
`asyncio` tasks in one process, sharing one PostgreSQL connection and one NATS/JetStream
connection - matching the existing single-connection-per-process convention already used by
`scripts/p2_shadow/worker.py` and `scripts/signal_outbox_relay.py` (`docs/engineering/OPTIMIZATION_REGISTER.md`
#32 records this as a known scaling simplification, not a defect).

## Event stream topology (exact, as shipped)

| Subject | Stream | Producer | Consumer(s) in this slice |
|---|---|---|---|
| `signal.entry.created.v1` | `TRADING_CORE` (unchanged) | P2 (outbox relay) | `trade-mgmt-open` (this branch) |
| `trade.observation.recorded.v1` | `TRADING_OBSERVATION` | the observation loop (this branch) | `trade-manager-shadow` (this branch) |
| `trade.opened.v1` | none (outbox-only) | `managed_trade.py` (unchanged) | none yet |
| `trade.decision.made.v1` | none (outbox-only) | `tm_none.py` (unchanged) | none yet |

## Observation loop

Fixed-interval poll (`P4_OBSERVATION_INTERVAL_SECONDS`, default 30s - `docs/engineering/OPTIMIZATION_REGISTER.md`
#30), one tick per interval: relay any backlog first (a publish failure or restart never loses
an observation - it stays a correctly-recorded, unpublished outbox row, picked up on the next
tick), then discover open ManagedTrades grouped by instrument (one quote/bar fetch serves every
open trade on that instrument), record + publish an observation per trade via the unmodified
`trade_management.observation.record_observation`. One instrument's provider failure is caught
and logged per-tick; it never stops observations for other open trades that tick.

## Health/readiness

Stdlib `http.server.ThreadingHTTPServer` (matching `control_api/app.py`'s own convention, no new
dependency), `/healthz` (liveness) and `/readyz` (readiness - 503 until Postgres, NATS, the
activation boundary, and all three components have started). `deploy/trade_management/workload.yaml`
wires both as container probes.

## Resource envelope and namespace quota

See `deploy/trade_management/resource-quota.yaml` for the exact computation. Summary: `pods`
10->11 (+1), `limits.cpu` 3600m->3750m (+150m, this pod's own cpu limit), `limits.memory`
4Gi->4160Mi (3904Mi existing + 256Mi this pod's own memory limit). `requests.cpu`/`requests.memory`
ceilings are left unchanged. Neither manifest is applied by this branch.

## Credentials

Not rotated, not embedded. See `docs/engineering/OPTIMIZATION_REGISTER.md`'s "Credentials"
section for the explicit A/B decision the activation agent must make before P4.2 is actually
activated.

## What remains before activation (not performed by this branch)

Apply `deploy/trade_management/resource-quota.yaml`, build and pin a verified image digest into
`deploy/trade_management/workload.yaml` (replacing `REPLACE_WITH_VERIFIED_DIGEST`), resolve
credential rotation (A or B above), apply the workload manifest, then scale `replicas` from 0 to
1 as an explicit, reviewed activation step - not part of this branch.
