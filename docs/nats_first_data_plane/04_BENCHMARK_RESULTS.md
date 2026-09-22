# Local benchmark: DB-first vs NATS-first

**These are not production latency numbers.** Everything below runs in a single Python process
against `dataplane/fakes.py`'s in-memory `FakeConnection`/`FailingJetStream` - there is no real
PostgreSQL, no real NATS server, no real network hop, no real disk fsync anywhere in this
sandbox (verified: no reachable `localhost:5432`/`localhost:4222`, no `psycopg`/`nats-py`
installed). The purpose is **architectural comparison** - which coupling exists between which
steps, and how a slowdown in one component propagates (or doesn't) to the others - not an
estimate of real-world millisecond figures. Full machine-readable output is reproducible with
`python3 -m dataplane.benchmark` (prints the human-readable report, then the same data as JSON);
the JSON is not checked into this repo (matches the existing `*_results.json` gitignore
convention). The human-readable summary from that same run is reproduced below.

## Methodology

`dataplane/benchmark.py`, run via `python3 -m dataplane.benchmark`. 60 iterations per path per
scenario (except `projector_backlog`'s outage-recovery variant, which runs 120 for NATS-first -
see below). Each iteration builds a fresh, distinct `signal_id` (no dedup hits skewing timings).
Both paths run against separately-instantiated fakes with matched injected per-statement /
per-publish latency functions, so neither path gets an accidental unfair advantage from shared
mutable state or warm caches. Fresh `FakeConnection`/`FailingJetStream` per scenario, not shared
across scenarios.

For DB-first, the relay step (`OutboxRelay`-equivalent: read unpublished outbox rows -> publish
to JetStream -> mark published) is run inline using the exact SQL statements
`infrastructure/messaging/outbox_relay.py` issues, rather than re-driving `publish_batch()`'s
`SKIP LOCKED`/lease SQL against the fake (see `dataplane/benchmark.py`'s module docstring for
why: that concurrency-control SQL is already separately unit-tested and is orthogonal to a
single-worker latency comparison).

Five scenarios, matching the mission's list:

| Scenario | What it models |
|---|---|
| `warm_steady_state` | Nominal case: low, steady per-statement/publish latency. |
| `postgres_contention` | Occasional lock-wait spikes on PostgreSQL statements (simulated `FOR UPDATE` contention: 20% of statements get an extra ~8ms), JetStream unaffected. |
| `postgres_slowdown` | Sustained elevated PostgreSQL statement latency (~6ms/statement, e.g. degraded storage or replica lag), JetStream unaffected. |
| `projector_backlog` | The relay (DB-first) / projector (NATS-first) is deferred and drained as one batch after all 60 signals are accepted, modeling a consumer that has fallen behind. |
| `nats_restart_recovery` | JetStream unavailable for the first 30 of 60 iterations, then recovers; PostgreSQL is healthy throughout. |

## Results

Selected figures below
(`bus_publish_ack_ms` = time from publish-initiated to durable bus acceptance;
`end_to_end_projection_ms` = decision to PostgreSQL commit):

| Scenario | Path | bus_publish_ack_ms p50 / p99 | end_to_end_projection_ms p50 / p99 |
|---|---|---|---|
| warm_steady_state | DB_FIRST | 5.7 / 11.8 | 6.4 / 13.7 |
| warm_steady_state | NATS_FIRST | 1.1 / 2.3 | 11.6 / 25.0 |
| postgres_contention | DB_FIRST | 25.6 / 51.0 | 25.3 / 48.8 |
| postgres_contention | NATS_FIRST | 1.1 / 3.4 | 36.3 / 117.8 |
| postgres_slowdown | DB_FIRST | 83.9 / 94.2 | 78.3 / 90.0 |
| postgres_slowdown | NATS_FIRST | 1.1 / 2.1 | 95.6 / 156.9 |
| projector_backlog | DB_FIRST | 344.9 / 600.1 | 9.0 / 22.2 |
| projector_backlog | NATS_FIRST | 1.3 / 3.1 | 506.2 / 648.3 |
| nats_restart_recovery | DB_FIRST | 191.0 / 632.2 | 9.5 / 15.2 |
| nats_restart_recovery | NATS_FIRST | 1.1 / 2.4 | 12.8 / 21.0 |

## What this shows (and does not show)

**Bus acceptance latency is decoupled from PostgreSQL health in NATS-first, coupled to it in
DB-first.** This is the headline architectural point, and it holds across every scenario:
`NATS_FIRST:bus_publish_ack_ms` stays in the ~1-3ms range *regardless* of whether PostgreSQL is
contended, slow, or backlogged, because bus acceptance never waits on a PostgreSQL statement.
`DB_FIRST:bus_publish_ack_ms` tracks PostgreSQL latency directly (5.7ms warm -> 83.9ms under
sustained slowdown -> 344.9ms under backlog), because bus acceptance *is* downstream of a
PostgreSQL commit plus a subsequent relay publish.

**The cost doesn't disappear in NATS-first - it moves to the PostgreSQL projection.** Under
`postgres_slowdown`, NATS-first's `end_to_end_projection_ms` (95.6ms p50) is actually *higher*
than DB-first's (78.3ms p50) for this run, because NATS-first pays the same PostgreSQL cost
*plus* the extra hop through the projector's own `claim_inbox`/`mark_inbox_processed` inbox
bookkeeping, which DB-first's single transaction doesn't need. NATS-first trades "reporting
latency is coupled to real-time acceptance" for "reporting latency is its own, separately
degradable thing" - it does not make PostgreSQL writes faster.

**`projector_backlog` is the sharpest contrast.** When the relay/projector falls behind by a
full batch of 60 signals: DB-first's bus acceptance for *every one* of those 60 signals is
delayed by the backlog (p50 344.9ms, p99 600.1ms) - because in DB-first, "accepted onto the real
-time bus" cannot happen until the relay gets to that row. NATS-first's bus acceptance for the
same 60 signals is untouched (p50 1.3ms, same as steady state) - only its PostgreSQL projection
is delayed (p50 506.2ms). This is the direct, measured version of failure scenario G
("PostgreSQL lags substantially -> subscriber delivery and real-time consumers remain
independent") and is exactly the property the architecture is designed to provide.

**`nats_restart_recovery` shows the asymmetric failure mode.** During the simulated JetStream
outage (first 30 iterations), DB-first's PostgreSQL commits all still succeed immediately
(nothing in that transaction touches JetStream) - the cost only shows up later, once the relay
catches up post-recovery, pulling DB-first's overall `bus_publish_ack_ms` distribution up (p50
191.0ms, reflecting the blend of unaffected and outage-delayed iterations). NATS-first instead
**rejects** those 30 publishes outright (`RealtimePublicationFailed`, never silently accepted -
this is why NATS-first's benchmark loop retries them with the identical `signal_id` after
recovery, which is also why `projector_backlog`'s NATS-first `n=120` samples exist: 60 initial +
60 retried after a deferred-projection variant. `nats_restart_recovery` itself settles back to
steady-state numbers after retry because none of its 30 outage iterations are counted as
"accepted" until they succeed). This is the concrete difference between "durable but
delayed" (DB-first under a JetStream outage - the signal already exists in PostgreSQL) and
"not accepted yet, safe to retry" (NATS-first under a JetStream outage - nothing exists anywhere
yet, but the deterministic `signal_id` makes a retry harmless).

**What this does NOT show:** absolute production latency (see the caveat at the top), behavior
under real concurrent multi-worker load (`OutboxRelay`'s `SKIP LOCKED` concurrency semantics
aren't re-exercised here - already covered by `tests/test_migration_substrate.py`), real NATS
JetStream's own internal replication/fsync latency, or real PostgreSQL's connection-pool/lock
behavior under genuine concurrent writers. A follow-up against real infrastructure (Stage 2 of
`03_MIGRATION_PATH.md`) would be needed before any production capacity-planning decision.
