# Durable library event transport

This implementation is opt-in. No deployment, registration, stream creation,
credential change or migration application is performed by preparing this code.
It builds on next-open quote capture from PR 229. Legacy polling defaults remain
available for existing frozen runs. The collector still obtains data through
its existing shared MT5 read bridge; this change removes observer/orchestrator
polling from the library event path, not the upstream EA/bridge polling protocol.

## Runtime flow

1. The shared collector validates a snapshot, then commits immutable market
   evidence and a source sequence/outbox intent before advancing the Redis cache.
   Genuine hot quotes append a small event referring to the latest committed
   snapshot. Quote events never stand in for historical candle-open prices.
2. A dedicated relay uses PostgreSQL LISTEN/NOTIFY to wake on committed outbox
   inserts. It publishes an event-ID reference to JetStream and records success
   after durable acceptance. A five-second timeout only reconciles lost wakeups.
3. An observer durable consumer subscribes to its configured provider symbols,
   reads the referenced immutable evidence from PostgreSQL, and invokes the same
   ForwardObserver/evaluators. It never reads a newer Redis snapshot during replay.
4. Checkpoint, evaluation, consumer inbox/cursor and accepted-entry notification
   intent commit together. Only then does the consumer acknowledge delivery.
5. An orchestrator durable consumer subscribes to that run's research-entry
   notifications. It checks live managed lifecycle, parent enablement, OFF and
   execution-ineligible policy, frozen parameters, run binding and active
   memberships, then invokes the existing CanonicalSignalPublisher. Existing
   canonical outbox publication and theoretical outcome resolution follow.

No research transport process calls broker execution or account sizing. Research
notifications are on a separate stream from execution. Canonical research signal
publication still carries research_only=true and execution_eligible=false.

## Ownership and delivery

| Subject | Stream | Delivery scope |
| --- | --- | --- |
| `market.evidence.committed.v1.<provider-UTF8-hex>` | `MARKET_EVIDENCE` | Each observer filters its configured provider symbols |
| `research.library.entry.observed.v1.<run-id>` | `LIBRARY_RESEARCH` | Each orchestrator filters its immutable run ID |

The schema stores the base event type and immutable payload; routing tokens are
derived from that payload. Notifications contain only an event ID. Full candles,
opening evidence, quote prices and source timestamps stay in immutable database
evidence. The relay verifies payload hashes before publishing. Existing TRADING_CORE,
EXECUTION and SIGNAL_REALTIME authority/subject sets are unchanged.

New streams use file storage, bounded age and a 256 MiB byte limit each, with
discard-new backpressure. These are explicit canary limits to review against the
actual universe and observed volume, not a production sizing claim. Consumers
use explicit acknowledgement, one in-flight message, unlimited redelivery and
processing heartbeats. Existing durable/stream ownership drift fails startup;
there is no automatic rewind or stream rewrite.

PostgreSQL source sequences serialize market effects per provider. A missing
notification prefix is recovered in batches of at most 100 from retained DB
evidence. Missing source history blocks consumption instead of skipping data.
Lost publish/ack responses can redeliver but cannot create a second entry.
If canonical publication commits before its inbox marker, retry verifies the
existing signal identity without changing its original classification/price.

Delayed market replay never calls the prospective candle-open path. If an entry
notification reaches dispatch outside the opening age window, it is published
only as retrospective research evidence with ENTRY_DISPATCH_WINDOW_EXPIRED.
No reference entry timestamp is rewritten to make delayed evidence appear fresh.
H1/M15 watermark buffering and exact next-open checks remain in the shared engine.
Once an opportunity is settled, ordinary quotes do not re-evaluate that strategy
until a new snapshot arrives. Missing/stale quotes leave the stream checkpoint
unchanged so a later valid quote may resolve it within the original entry window.

## Migration and credentials

Migration `070_library_event_stream.sql` adds evidence, source heads, outbox,
inbox/cursors, a transaction-notification trigger and evidence immutability
triggers. It changes no strategy registry, privileges, Secrets or execution policy.
Confirm the migration slot remains free before merge and apply through the
approved admin/migration path. No service self-migrates or grants privileges.

Each process receives its own approved `LIBRARY_EVENT_DSN`, explicit
`LIBRARY_EVENT_DATABASE` (default `trading`), `NATS_URL` and scoped NATS auth
(`P2_NATS_USER` / `P2_NATS_PASSWORD` where configured). Connection startup verifies
the primary database identity and required schema without logging credentials.
Provision least-privilege roles and audit their effective grants before activation:

- Collector: insert/read immutable snapshots/outbox and insert/update/read source heads.
- Relay: read outbox and update publication bookkeeping only; publish only the two research stream namespaces.
- Observer: read source evidence; write its research checkpoints/evaluations/inbox/cursors and notification intents; consume only its market filters. No canonical or broker writes.
- Orchestrator: existing canonical publication privileges, managed catalog/membership reads and research inbox writes; consume only its run's research notifications. No broker writes.
- Stream provisioning: separate operator capability, not granted to normal observer processes.

The database protects snapshot/outbox payloads against UPDATE. Retention cleanup
is operator-controlled: retain source evidence/outbox rows needed by all durable
cursors and dependent snapshots. No automatic pruning is enabled. Monitor DB growth
and consumer lag before broadening the canary. Consumer inbox/outbox FK dependencies
must be respected during reviewed archival; never delete a missing prefix to clear lag.

## Prepare a cutover

Build a verified immutable image from merged source. Deploy the legacy
orchestrator image containing the publication_transport=JETSTREAM skip guard
before switching any managed binding; an older scanner does not understand that
ownership marker. Preserve and stop the affected old observer/event consumers,
and explicitly set the affected managed instances offline through the approved
control path. Leave other strategies running.

Generate a candidate bundle from that exact released source:

```bash
PYTHONPATH=. python -m research.library_event_plan \
  --run-id '<fresh-run-id>' --activation-time '<future-UTC-epoch>' \
  --image 'ghcr.io/strat-relay/trading-platform-runtime@sha256:<verified-digest>' \
  --key fvg_retracement --output '<new-output-directory>'
```

The planner writes run.json, readiness.json, activation.sql and quiesce.sql only.
T0 must be at least ten minutes ahead. Activation is one transaction, rejects an
elapsed T0 or an existing run, requires existing frozen OFF/ineligible research
instances to be offline, seeds an attested empty checkpoint, and rebinds only
the requested managed instances. It preserves old run state/events/signals and
never rewrites frozen definitions or parameter sets. Verify existing instrument
memberships and provider mappings match the manifest before applying SQL.

Enable collector emission with MARKET_DATA_EVENT_MODE=JETSTREAM_OUTBOX only after
its dedicated DB path is provisioned. Explicitly provision the bounded streams:

```bash
LIBRARY_EVENT_TRANSPORT=JETSTREAM python -m research.library_event_service provision-streams
```

Then prepare isolated GitOps processes with the released image and reviewed credentials:

```bash
python -m research.library_event_service relay
python -m research.library_event_service observer --manifest /config/run.json
python -m research.library_event_service orchestrator --manifest /config/run.json
```

All roles require LIBRARY_EVENT_TRANSPORT=JETSTREAM. Observer and orchestrator
require EXECUTION_AUTHORITY_MODE=DISABLED. The event orchestrator additionally
requires the existing DB_PRIMARY signal authority and explicit SIGNAL_CUTOFF_ID /
SIGNAL_CUTOFF_UTC. This does not enable the dormant NATS-first signal authority.
Manifests require transport=JETSTREAM, next-open mode and finite quote freshness.
The polling runner rejects JETSTREAM manifests.

Review NetworkPolicies for each role: DB access plus NATS TCP 4222 for relay and
consumers, DNS, and the existing collector read/cache paths. These event observers
need no Redis, bridge or broker egress. Add matching PostgreSQL/NATS ingress for
the actual pod labels; source egress alone is insufficient.

## Verification and rollback

Run a small research canary first. Require an attested new run, correct image
IDs, all configured provider streams observed, bounded stream settings, healthy
consumer lag, no redelivery storm, correct provider sequences and zero broker
writes. Demonstrate an accepted entry before the entry candle closes, frozen bid/
ask with provider/receipt/observation timestamps and the correct tentative side.
Trace source collection, commit, notification acceptance, evaluation, canonical
persistence and outbox publication. A quiet market may produce no entries; use
persisted evaluations and source progress for health, not signal count alone.

`library_stream_message_committed`, `library_stream_message_blocked` and
`library_stream_batch_published` are transport logs; source stage timestamps flow
through the existing quote/latency tracing. Observe per-provider timestamps as
well as overall health; a quiet or closed provider must be distinguished from a
stalled feed using the existing market-session health tooling.

For quiesce, apply the candidate's run-scoped quiesce.sql and scale down only its
event observer/orchestrator through GitOps. Preserve durable cursors, evidence and
checkpoints. Do not relabel the event checkpoint as POLL or restore an old run with
a changed artifact. A return to the previous frozen image/run requires a reviewed
managed-binding restoration; any queued entries still pass current age/research
gates. Do not disable shared parents or change execution authority.

Local verification includes deterministic crash/redelivery, ordering, watermark,
quote and canonical retry tests. Real PostgreSQL checks run only with the isolated
CI test DSN; a live JetStream/cluster soak and resource/retention sizing remain
deployment gates, not claims established by unit tests.
