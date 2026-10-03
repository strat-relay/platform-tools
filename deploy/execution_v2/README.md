# execution_v2 deployment artifacts

**Not applied.** Prepared for an independent Codex re-audit and eventual, separately-authorized
activation (mission `CLAUDE-STRATRELAY-V2-EXECUTION-AUDIT-REMEDIATION`, sections 15/18). Nothing
in this directory has been run against the live cluster.

## Files

| File | Purpose |
|---|---|
| `migration-job.yaml` | One-off `python -m postgres.migrate` Job; applies `postgres/migrations/016_execution_v2_foundation.sql` (additive only, renumbered from 015 after reconciling with production's `015_entry_signal_outcomes.sql`) along with any other pending migration. |
| `workload.yaml` | The platform-side runtime `Deployment` + `Service`. **`replicas: 0` by default.** |
| `risk-state-workload.yaml` | The single Redis risk-state collector `Deployment`; reads the broker read bridge and writes the account-scoped Redis snapshot. |
| `network-policy.yaml` | Ingress/egress allow-list. No rule permits egress to the MT5 bridge host, or to any bridge fence endpoint, at all - see below. |
| `resource-quota-patch.README.md` | Explains why no fabricated `ResourceQuota` numbers are included (no live-cluster read has been performed). |

**Not included here, and NOT part of this mission:** any deployment artifact for the real
bridge-side fence service itself (`mt5_bridge_fence/`). That code is designed to be portable to
the actual `mt5-native-bridge` process (a separate repository) but has never been deployed
anywhere - see `docs/v2_execution/README.md` "Before activation." Deploying it, and adding the
corresponding `NetworkPolicy` egress rule this directory's `workload.yaml` would then need, is
later, separate, explicitly-authorized work.

## Required configuration before scaling above `replicas: 0`

All of the following are read by `execution_v2/runtime/config.py::RuntimeConfig.from_env()`,
which **fails closed** (raises `RuntimeConfigError`, the process never starts) if any required
value is missing or invalid:

| Env var | Required | Notes |
|---|---|---|
| `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` or `TRADING_POSTGRES_DSN` | yes | Same convention as every other runtime; supplied via the existing `trading-runtime-endpoints` Secret. |
| `NATS_URL` | yes | Same `trading-runtime-endpoints` Secret. |
| `V2_EXECUTION_ACCOUNT_ID` | yes | The one personal account this deployment is allowed to act for. `workload.yaml` ships `REPLACE_BEFORE_ACTIVATION` as a deliberately invalid placeholder. |
| `V2_BRIDGE_FENCE_URL` | **yes, no default** | The real bridge's fence-validation HTTP endpoint (`execution_v2.runtime.bridge_client.HttpBridgeFenceClient`'s target). `workload.yaml` ships an invalid placeholder value. Must never contain `:22348`. There is no fallback of any kind - a deployment with this unset (or targeting 22348) refuses to start. |
| `V2_FENCE_SIGNING_KEY` | yes, ≥32 bytes | The HMAC key shared with the bridge's independent fence verification. **Never commit this to Git.** Supply via a dedicated `execution-v2-fence-signing-key` Secret (referenced but not created by `workload.yaml`). The same key material must also be provisioned to the real bridge process out-of-band (never over the platform's own request path) once it exists. Rotate by minting a new key, adding it under a new `V2_FENCE_KEY_ID`, and only removing the old key once no outstanding grant/authorization can still reference it. |
| `V2_FENCE_KEY_ID` | no (default `v2-fence-key-1`) | Must match whatever key id the bridge-side verifier expects. |
| `EXECUTION_AUTHORITY_MODE` | no (default `DISABLED`) | **Deliberately absent from `workload.yaml`.** Do not add it there; set it via a separate, explicitly-authorized change when activation is approved. |
| `RISK_CONTEXT_SOURCE` | no (default `BRIDGE`) | Set to `REDIS` only after the collector reports a healthy, fresh snapshot. Redis mode never falls back to synchronous bridge reads. |
| `RISK_REDIS_URL` | required when `RISK_CONTEXT_SOURCE=REDIS` | Account-scoped Redis hot-state URL, supplied from the `trading-redis-auth` Secret. |
| `V2_EXECUTION_BRIDGE_MODE` | no (default `demo`) | `demo` \| `real` - a fence *resource namespace* prefix only, never a live-vs-simulated code switch. |
| `V2_EXECUTION_RISK_POLICY_PATH` | no | Defaults to the shipped `orchestration/config/v2_execution_risk_policy.json` (`enabled: false`). Point at an operator-approved, reviewed file to allow any signal through; see that file's own `_comment`. |
| `POD_NAME` | yes (Kubernetes supplies via `fieldRef`) | The ownership-fencing holder identity; must also be pre-registered in `platform.runtime_instances` (or `service.py::register_runtime_instance` does this automatically on startup) before a lease can be acquired. |

## Health/readiness behavior

- `GET /healthz` - liveness: process is up.
- `GET /readyz` - readiness: Postgres connected, NATS connected, the durable entry-signal
  consumer is subscribed. 503 until all three are true. Reaching the bridge fence endpoint is
  deliberately NOT part of readiness - a temporarily unreachable bridge should not take this pod
  out of rotation for signal ingestion; individual `process_signal` calls surface
  `BridgeUnreachable` on their own and are safely retried by JetStream redelivery.
- `GET /executionz` - **always-on, truthful state**, read directly from the running config, never
  hard-coded: `{"execution_authority_mode": "...", "account_id": "...", "broker_writes": bool}`.
  This is the fastest way for an operator (or Codex's audit) to confirm a running pod's actual
  authority state without reading logs or environment variables.

## Rollback

1. Scale `execution-v2-runtime` back to `replicas: 0` (or delete the Deployment/Service; both are
   safe - no in-flight broker state exists to reconcile on the *platform* side since this pod
   never held a real broker connection itself; any in-flight state lives on the bridge's own
   durable store, which this rollback does not touch).
2. The migration (016) is additive-only and does not need to be rolled back to make the system
   safe; its tables (`execution_v2.*`) and function (`platform.assert_generation`) are inert if
   nothing writes to them. Down-migration is out of scope for this ship-first slice, matching
   every prior migration in this repository's convention (no `.down.sql` files exist for any
   migration in this repository).
3. If the fence signing key needs to be revoked (suspected compromise), delete the
   `execution-v2-fence-signing-key` Secret, remove the corresponding key from wherever the real
   bridge process holds its own copy, and remove the entry from `FenceAuthority` construction;
   every currently-outstanding grant/authorization becomes unverifiable and the bridge
   independently rejects all of them (`InvalidSignature`) - fail-closed by construction, not by a
   separate revocation list.
