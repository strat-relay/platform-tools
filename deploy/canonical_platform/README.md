# Canonical platform runtime infrastructure

These manifests provision shared `trading-platform` PostgreSQL and NATS
JetStream infrastructure. They are separate from `deploy/p2_shadow/`; never
promote or copy data from the P2 shadow services.

The StatefulSets use the cluster's `local-path` storage class. Their PVCs
survive pod restarts, but this single-node local storage is not node-loss
resilient. Maintain off-node backups before treating the cluster as a
production disaster-recovery boundary.

## Secret bootstrap

Create secrets separately from source control. Use fresh randomly generated
credentials; do not print, check in, or place them in ConfigMaps. The
`trading-postgres-auth` Secret must contain `POSTGRES_ADMIN_USER`,
`POSTGRES_ADMIN_PASSWORD`, and `TRADING_APP_PASSWORD`. The `trading-nats-auth`
Secret must contain `NATS_USER` and `NATS_PASSWORD`. The runtime endpoint Secret
must contain `TRADING_POSTGRES_DSN`, `NATS_URL`, `P2_NATS_USER`, and
`P2_NATS_PASSWORD` (the latter two names are the current NATS client interface).

Use an explicit DSN of the form
`postgresql://trading_app:<generated-hex-password>@trading-postgres.trading.svc.cluster.local:5432/trading_platform?sslmode=disable`
and NATS URL
`nats://trading-nats.trading.svc.cluster.local:4222`, with `P2_NATS_USER` and
`P2_NATS_PASSWORD` supplied separately from the Secret as expected by the
current NATS client. The generated hex passwords are URL-safe. The PostgreSQL runtime role owns the
database but is not a PostgreSQL superuser; the separate bootstrap admin
credential is used only by the server's first-init procedure.

## Apply and initialize

1. Verify namespace quota and node storage before applying. The current resource
   requests are sized so the two permanent pods fit the existing quota. If the
   independent relay is to be activatable without removing a healthy runtime,
   apply `quota-relay-capacity.yaml` to add only one pod slot and 250m CPU limit
   headroom; leave all other quota bounds unchanged.
2. Create the three Secrets described above using your approved secret
   generation/handling procedure.
3. Apply `runtime.yaml`.
4. Wait for both StatefulSets and PVCs to become Ready/Bound.
5. Apply migrations 001–012 to the empty `trading_platform` database using the
   explicit runtime DSN and the repository migration runner. Do not import
   JSONL or shadow data.
6. Create `TRADING_CORE` from the versioned subject list in
   `infrastructure/messaging/contracts.py`; do not create V2 execution streams.
7. Run `outbox-relay-prepared.yaml` only after replacing the placeholder image
   with a verified immutable runtime image. It is intentionally configured at
   zero replicas. DB_PRIMARY configuration is prepared separately and must not
   be attached to the live orchestrator before the authorized T0 cutover.

At the later T0 transition, create a separate `trading-signal-cutoff` Secret
with `SIGNAL_CUTOFF_ID` and `SIGNAL_CUTOFF_UTC` from the persisted cutoff record.
The prepared patch `orchestrator-db-primary-env-prepared.patch.yaml` injects
the runtime endpoint Secret, the DB_PRIMARY flags, and that cutoff Secret. The
prepared ConfigMap also pins `ORCHESTRATOR_MODE=PRIMARY` and
`EXECUTION_AUTHORITY_MODE=DISABLED`. It is deliberately not applied in this
task. Before using it at T0, inspect the
live Deployment's environment and merge the `envFrom` references without
discarding any existing required environment entries. Do not create the cutoff
Secret or attach the prepared ConfigMap before T0.

The current Context/orchestrator path requires no strategy definition rows in
PostgreSQL: the selected strategy/version are carried by the signal contract,
and its existing platform JSON registry remains the runtime input for now.
Migration 003 inserts two static rows in `audit.execution_safety_invariants`;
those are safety definitions, not runtime history. No other configuration seed
is required for the infrastructure bring-up.
