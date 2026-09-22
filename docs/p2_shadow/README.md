# P2.1 isolated shadow substrate

This deployment tails the existing authoritative `signals.jsonl` from the
`mt5-native-bridge-runtime` PVC mounted **read-only**. It exercises the P2-A1
canonical ingest, PostgreSQL transactional outbox, JetStream durable consumer,
and inbox deduplication path. It has no strategy, publication, execution, or
broker authority. Both `SIGNAL_DB_PRIMARY_ENABLED` and
`SIGNAL_JETSTREAM_PRIMARY_ENABLED` are explicitly `false` and startup fails if
either is true.

The workload is isolated in the existing `trading` namespace because the
source PVC is namespace-scoped. Its dedicated services are ClusterIP-only;
the P2 NetworkPolicy restricts P2 pod ingress to the worker and egress to its
own PostgreSQL, NATS, DNS, and HTTPS (used only by the one-time dependency
bootstrap init container). No bridge, execution, customer-distribution, or
Control API endpoint is in the worker's network path. The runtime PVC mount is
read-only. PostgreSQL, JetStream, and evidence/checkpoint data each have their
own persistent volume claim.

## Footprint

| Component | Request | Limit | Persistent storage |
|---|---:|---:|---:|
| PostgreSQL | 100m CPU / 256Mi | 500m / 512Mi | 2Gi |
| NATS + JetStream | 50m / 96Mi | 250m / 256Mi | 1Gi |
| P2 worker | 75m / 128Mi | 300m / 384Mi | 1Gi evidence PVC |
| Worker init containers | 50–100m / 96–128Mi each | 250–350m / 256–512Mi | shared evidence PVC |

PostgreSQL and NATS are single-replica shadow services, not HA production
services. They are pinned by image digest. Credentials are generated into a
Kubernetes Secret at deployment and are not stored in this repository.

## Startup and evidence semantics

Deployment runs the live-runtime read-only preflight first. The worker then
applies repository migrations to an empty dedicated database through 011,
creates a restricted application role, and performs a one-time historical
bootstrap. Bootstrap events and the controlled JetStream dedupe/redelivery
proof are classified `NON_LIVE_BOOTSTRAP_PLUMBING_PROOF`; they do not count as
live signals or evidence-window observations.

Before the live process tails append activity, it catches up complete source
records and waits for the outbox and durable inbox to drain. It then writes an
immutable `/data/evidence-window.json` marker with the UTC start timestamp,
source device/inode/size/hash and byte cursor, last prior signal id, runner
image/generation/config provenance, P2-A1 and worker commits, migration list,
JetStream configuration identity, and both false authority flags. Records
before the boundary remain `BOOTSTRAP`; appended records are `LIVE`. Replayed
records after source rotation are explicitly `ROTATION_RECOVERY`.

P2.1 is not complete at deployment. The approved evidence minimum remains at
least 10 trading days including a weekend, 30 natural signals over at least 3
symbols, five consecutive clean daily quiesced reconciliations, three natural
P2-shadow restarts, durable/redelivery evidence, zero duplicate domain
effects, and provenance proof. If 30 natural signals are not observed within
30 trading days, stop and report the observed rate. These thresholds do not
authorize signal-authority cutover. No creation-lag or observation-start-lag
eligibility bound is introduced; OD-A7-4 remains open.

## Read-only inspection

Run `scripts/p2_shadow/report.sh` to list only labeled shadow Kubernetes
resources and print the persistent worker status, bootstrap proof, and
immutable evidence marker. It does not change resources. Reconciliation
findings are retained in `platform.reconciliation_runs` and
`platform.reconciliation_findings`; status includes the latest mismatch
classes, source cursor, signal counts by evidence class, outbox state/retries,
JetStream state, inbox counts, lag distributions, and restart counters.

Continuous reconciliation uses canonical `signal_emitted_at` as source/event
time and a configurable `P2_RECONCILIATION_DELTA_SECONDS` (default 32 seconds,
the A7 proposal of two 1-second tailer polls plus 30 seconds). `decision_time`
is a reference fill time and does not grant processing grace. A recent
legacy-only record is `EXPECTED_LAG` and keeps the run non-clean until
resolved; after Delta, or when the source timestamp is missing/invalid, it is
the blocking `MISSING_DATABASE` class. Quiesced comparisons get no extension
beyond Delta. `KNOWN_LEGACY_ANOMALY` remains unreachable because no explicit,
evidence-backed classifier exists.

## Stop / rollback

Stopping only the shadow path does not require an authority change. To stop
collection while retaining all data for investigation, run:

```sh
KUBECONFIG=/Users/caleb/Downloads/local.yaml \
  P2_CONTEXT=local P2_NAMESPACE=trading \
  scripts/p2_shadow/rollback.sh
```

The script scales only `p2-signal-shadow-worker` and its dedicated PostgreSQL
and NATS StatefulSets to zero; PVCs, marker, and database remain. It does not
touch `context-paper`, `orchestrator-shadow`, the legacy signal file, 22347,
the console, or authority flags. Do not delete PVCs as part of routine
rollback: doing so destroys evidence. Reattachment requires operator review
of the preserved marker and checkpoint before scaling services back up.

The deployment script is `scripts/p2_shadow/deploy.sh`. It refuses to deploy
from a dirty worktree and runs preflight before applying shadow resources.
