# Canary provisioning and activation

Authorized scope: additive research tables; three offline strategy registrations;
dedicated research-only credentials; standalone GitOps observer. No broker orders,
canonical publication, existing strategy rule changes, or history deletion.

## Provision first

Migration 063 is now a real source migration, including creation of the NOLOGIN
observer permission group. First application requires PostgreSQL CREATEROLE.
Use the operator provisioning script against the reviewed local kubeconfig while
the observer is absent or at zero replicas. It reads the actual DB endpoint from
the existing resolver, creates/reuses only its labeled credential Secret, and
applies the exact migration under the normal migration advisory lock/checksum
ledger. It uses the PostgreSQL pod's local administrator via psql for role creation.
It then provisions a dedicated non-admin login, grants only the observer group,
and verifies the login can access research tables but cannot insert canonical signals.

The script performs production writes. Review its source and SQL before running.
Passwords are generated locally, passed through stdin only, never printed, and
not stored in Git or the report. The Kubernetes Secret preserves credentials for
retry; the script never rotates a pre-existing login password. If any step fails,
the observer stays stopped. Preserve its managed Secret and sanitized failure
report, then diagnose; do not delete historical evidence.

Apply migration 063 with the operator script BEFORE merging the source provisioning
PR unless the normal migration runner has CREATEROLE. Once recorded with the same
checksum, future normal migration runs skip it. A conflicting migration slot or
identity aborts. PostgreSQL and Kubernetes operations cannot be one transaction;
a failed run can leave offline tables/registrations or a canary-owned Secret.
These are safe preparation artifacts and should be retained for retry.

## Stage and then activate through GitOps

The GitOps staging PR adds the observer at replicas=0 with a null activation time.
It pins the already published 7df3293 runtime digest and intentionally quotes the
image reference so the generic bulk release updater cannot silently repin a frozen
experiment. NetworkPolicies allow only DNS, Redis 6379 and PostgreSQL 5432; bridge,
EA, execution broker and general internet egress are absent.

After provisioning succeeds, the activation PR must set an explicit future UTC T0,
freeze the ConfigMap with immutable=true, and set replicas=1. Use the existing run
ID only if no earlier state exists for that run. Otherwise use a new run ID and a
new ConfigMap name. Startup/liveness monitor recent durable cycle activity;
readiness additionally requires healthy data processing. A degraded market-data
stream must not cause an immediate restart loop.

Verify the three strategies are visible with online=false, execution_eligible=false,
execution_mode=OFF after provisioning. These are research catalog configurations;
activation of this independent research worker does not enable an orchestrator
adapter. Verify actual post-T0 evaluation records and research outcomes separately.
Bootstrap emits no entries. Completed-candle reconstruction is not a live fill.

Rollback the observer through GitOps to replicas=0 and preserve research tables,
credentials, T0, manifest and outcome evidence. Do not roll back by deleting rows.
