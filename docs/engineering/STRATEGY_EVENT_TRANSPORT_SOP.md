# Strategy event transport SOP

Every online strategy uses shared event scheduling. New strategies compose an
existing runtime; adding another independent sleep/discovery loop is not an
accepted production integration. Archived desktop/replay scripts remain offline.

## Runtime coverage

| Strategy/runtime | Market wakeup | Evidence-ready wakeup |
| --- | --- | --- |
| Context Structure Retrace V1/V2 | Shared Wakeup in both runners | After compact file state is saved; same revision has same outbox ID |
| Price Action S/R, Engulfing and future PA evaluators | Shared PA runner | Inside the checkpoint/evaluation PostgreSQL transaction |
| Library ICT/SMC, Breakout, Trend, Mean Reversion, Momentum, Price Action and other compositions | Shared generic runner, including ICT/SMC and Donchian V2 workload entry points | Inside the checkpoint/evaluation PostgreSQL transaction |
| Kojo V1/V2/V3 adapters | Orchestrator wakes and invokes existing adapter | Same orchestrator cycle; existing pending-publication files retained |
| Liquidity live adapter | Orchestrator wakes and invokes existing adapter | Same orchestrator cycle; existing authority/fencing retained |
| Standalone Liquidity live/monitor process | Shared Wakeup | Existing canonical publication path; no second publisher added |
| Canonical signal outbox relay | PostgreSQL commit notification | Existing JetStream publisher and publication bookkeeping; no timer wait on the normal path |
| Historical Wine desktop Liquidity and replay scripts | Offline only | Not production runners; do not activate these as event services |

The immutable `research.library_event_service` route is still supported. It binds
each evaluation to immutable market evidence and its own explicitly activated
manifest. The compatibility route here wakes existing evaluators to read current
canonical Redis inputs and existing authoritative checkpoints. These are distinct
contracts: a wakeup is not the market snapshot, an accepted fill, or a replay cursor.

## Commit order and authority

1. Collector validates and writes completed bars, snapshot, metadata and health to
   the existing Redis cache. Its existing immutable evidence/archive sinks retain
   their current semantics.
2. After the cache is visible, a bounded background writer appends a cache-ready
   hint to `platform.strategy_runtime_outbox`. The collector thread never waits on
   this new transport's database writes. Pending cache hints coalesce by provider;
   at most 256 providers are queued. Overflow/error is explicit telemetry.
3. PostgreSQL NOTIFY wakes `infrastructure.messaging.runtime_event_service relay`.
   It publishes the durable outbox ID to JetStream and only then marks delivery.
   A crash in between may repeat the same ID, which is safe for scheduling hints.
4. One NATS reader thread signals a generation counter. The business loop remains
   serial. Events arriving during evaluation remain pending for the next cycle;
   bursts coalesce without an unbounded consumer queue.
5. The evaluator reads current canonical inputs at actual observation time. It
   persists through its existing checkpoint/evidence path. No market data from
   the wakeup is injected into the evaluator and no decision time is backdated.
6. Separate evaluator processes enqueue evidence-ready hints after file persistence
   or in the same database transaction as evidence. These immediately wake the
   existing Signal Orchestrator and its existing canonical publisher/gates.

The orchestrator also subscribes to cache-ready hints for in-process Kojo and
Liquidity evaluation. Market wakeup alone may precede a separate Context/PA
evaluation; its later evidence-ready event closes that discovery race.

ACK means **scheduling hint accepted**. It does not mean a strategy evaluation or
broker fill committed. Lost/expired/coalesced hints are healed by startup discovery
and bounded reconciliation of the existing durable state. Canonical publication
idempotency and existing pending-publication files remain the effect boundary.
This does not claim exactly-once evaluation of every tick.

## Configuration and prerequisites

Opt in explicitly; the default `STRATEGY_EVENT_MODE=POLL` preserves current
production behavior until a reviewed cutover. It is a compatibility default, not
approval to enroll a new production strategy with timer-only scheduling.

All strategy/authority event consumers require:

- `STRATEGY_EVENT_MODE=JETSTREAM`
- unique stable `STRATEGY_EVENT_RUNTIME_ID` (1–64 alphanumeric, underscore or dash)
- `MARKET_DATA_SOURCE=REDIS`
- explicit `NATS_URL`, scoped `P2_NATS_USER`, `P2_NATS_PASSWORD`
- optional `STRATEGY_EVENT_RECONCILE_SECONDS` (default 30; allowed 1–60)

Context file-backed producers and the collector's hint writer additionally need
dedicated `STRATEGY_EVENT_DSN` and explicit `STRATEGY_EVENT_DATABASE`.
The verified deployment primary is `trading_platform`; never create another DB to
fit a generic default. The connection verifies primary/database/migration 072 and
has bounded connect/statement/lock timeouts. No admin/broker credentials fallback.

PA/library producers enqueue using their existing transaction connection: their
approved database role needs INSERT on this new table. They do not need a separate
DSN to enqueue transactionally. No strategy/catalog/execution privilege is added.

The relay needs its own dedicated DSN/database and scoped NATS publisher credential.
Run `python -m infrastructure.messaging.runtime_event_service provision` with a
separate operator credential, before starting consumers/relay. Consumers validate
stream/consumer configuration and do not create streams or rewrite conflicts.

Stream `STRATEGY_RUNTIME` owns exactly:

- `runtime.market.cache.ready.v1.*` (provider name encoded as a UTF-8 hex token)
- `runtime.strategy.evidence.ready.v1.*` (runtime ID token)

It is separate from default execution streams: file storage, discard NEW, 64 MiB,
1024-byte messages, 300-second retention. These short-lived hints are not research
history. Delivered scheduling outbox rows are pruned in bounded 1000-row batches
after one hour; pending rows and every strategy/checkpoint/signal/outcome table
remain untouched. Monitor pending growth and primary DB capacity during outages.

The existing canonical outbox relay also supports `STRATEGY_EVENT_MODE=JETSTREAM`:
migration 072 notifies `canonical_outbox_events` on INSERT after transaction commit.
The relay LISTENs on a separate connection and publishes immediately, retaining a
30-second reconciliation scan for lost notifications and expired leases. Failed
publication retries are bounded by that reconciliation interval. Its existing
publication/lease/idempotency logic and execution credentials remain unchanged.
This relay requires no new NATS stream and does not need a strategy runtime ID.

Scoped grants, provisioned through the approved mechanism (no role names assumed):

| Principal | PostgreSQL | NATS |
| --- | --- | --- |
| Collector hint writer | platform schema USAGE; INSERT on strategy_runtime_outbox | None |
| Context hint writer | Same append privilege | Subscribe/fetch/ACK own wakeup consumer |
| PA/library evidence role | Existing grants plus INSERT on strategy_runtime_outbox | Subscribe/fetch/ACK own wakeup consumer |
| Orchestrator | Existing grants unchanged | Subscribe/fetch/ACK own cache and ready consumer |
| Runtime relay | SELECT/DELETE outbox; UPDATE(published_at); schema USAGE | Publish only the two runtime subject prefixes; stream-info request and private reply inbox |
| Stream operator | None required | Provision/inspect STRATEGY_RUNTIME only |

Subscription credentials also require the exact JetStream consumer management,
information, pull and acknowledgement API subjects for their stable durable,
plus their private reply inbox. Verify allow AND deny cases with the actual NATS
account configuration. Do not reuse a global unrestricted credential or change
shared-server authentication as an implicit application rollout step.

Both sides of Kubernetes NetworkPolicy must allow the actual pods to PostgreSQL
5432, Redis 6379 where needed, NATS 4222 and DNS. Hint writers never access either
MT5 bridge. The sole collector retains the existing read-only bridge path.
Run one active replica per checkpoint/authority identity, with Recreate or the
existing single-owner fence. Sharing a durable between independent evaluators
would load-balance wakeups and is not a substitute for evaluator ownership.

## Safe rollout

1. Confirm the current primary, migration ledger, all actual running commands,
   managed identities, image IDs, checkpoint bindings and existing execution gates.
2. Build/release an immutable image from this change. Apply additive migration 072
   through the approved migration owner, then provision/audit scoped grants and
   credentials. No credential values in Git or reports.
3. Provision and verify STRATEGY_RUNTIME. Start the hint relay; verify actual
   publish acceptance and rollback/retry behavior. Enable the collector's new
   `STRATEGY_EVENT_MODE` separately from `MARKET_DATA_EVENT_MODE`: this route does
   not require enabling the paused immutable library route.
4. Verify `md:health.strategy_event_health` reports recent successful writes, and
   verify end-to-end receipt from an actual post-cache event. Pod Ready alone is
   insufficient. Transport failure must not stop shared collector progress.
5. Switch the existing orchestrator's scheduling mode in a reviewed GitOps patch;
   preserve its authority, policies, cutoff and execution configuration exactly.
   Never deploy a second canonical publisher for the same managed binding.
   Enable the same event-mode flag on the existing signal outbox relay after the
   migration is verified, to remove the final publication timer as well.
6. Switch Context scheduling after identity/rule regression and checkpoint audit.
   The decision-code fingerprint remains unchanged. Preserve all existing state.
7. Library artifacts hash their runner source. PA experiments can also have frozen
   artifacts. Use fresh, explicitly registered OFF/ineligible run/instance bindings
   and future T0 when their contract requires it. Do not reset state, overwrite a
   binding, or bulk-repin a frozen observer to make it start. Cover every library
   manifest (original canary, ICT/SMC, Donchian V2), not just three recipe names.
8. Measure actual post-cutover correlation traces across multiple candle boundaries:
   cache completion, hint enqueue/commit, NATS acceptance/receipt, dispatch,
   evaluation, evidence commit, canonical insertion and publication. Keep provider
   data acquisition latency separate. Verify broker writes and research eligibility
   remain unchanged. Do not claim a p95 objective without live measurements.

On rollback, set scheduling mode back to POLL only on the existing compatible run
image/binding; stop the new relay when no consumers need it. Do not delete evidence,
rewind checkpoints, or reactivate superseded publishers. The previously paused
immutable library activation stays paused until its independent gates pass.

## New strategy acceptance

Reuse the generic library, PA or in-process adapter runtime. Any new independent
runtime must use Wakeup and persist-before-ready ordering, pass the transport
tests, declare its subscriptions and scoped privileges, and be included in the
operational runner coverage test. A research strategy remains OFF/ineligible;
transport enrollment does not graduate it or change fill/outcome semantics.

Local and CI tests exercise race-free generation wakeups, coalescing, disconnect
failure, notification idempotency, post-cache ordering, isolated real JetStream
delivery/restart, and PostgreSQL rollback/immutability when a test DSN is present.
None of these tests establishes live production readiness.
