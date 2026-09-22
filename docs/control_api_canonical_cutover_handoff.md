# Control API canonical cutover handoff

Date: 2026-09-22
Implementation source: `c2f037491be4711a7b53879133814d0a4fedc6fd`
Deployment manifest commit: `04f18b15e66e72af4599c32cab4732aa5cf74d60`

The canonical Control API cutover is deployed and externally verified.
The API rollout initially waited for a terminating old pod to release the
namespace's full quota; it then completed without changing quota. Both
workloads use immutable image digests:

- Control API: `localhost:5001/trading-platform-control-api@sha256:49e6ea1176ca0a12ad77ad89d9e892a6a3586bfd2bc9f386972092dca8d6828f`
- Router: `localhost:5001/trading-platform-api-router@sha256:d90610a197bee0f46217613013f9b1757d79cb1631c8fcc87adabfb220de6254`

Public verification on 2026-09-22 confirmed `/system` and `/safety` report
PRIMARY / DB_PRIMARY / DISABLED, with execution INACTIVE, `real_execution_mode_active=false`,
and `broker_write_path_active=false`. `/system` reports the orchestrator
heartbeat UNKNOWN rather than fabricating ACTIVE. Public signal IDs exactly
matched a fresh read-only PostgreSQL query (9 IDs, schema 012); canonical
signal detail and PostgreSQL-backed event list/detail passed. Strategy list
and detail read the active mounted platform config. The three strategy
observability paths and audit list/detail explicitly return 503 unavailable;
execution collection/metrics return inactive with zero counts; connections
report execution INACTIVE. An unknown `/api/v1` route returns canonical 404.
The Console origin's public CORS preflight returns 204 with credentialed
allow-origin, methods, and headers. The Console's shared query panel maps
degraded/unavailable envelopes to an explicit degraded state rather than
rendering a fabricated empty result.

The runtime remains PRIMARY / DB_PRIMARY / DISABLED, port 22348 is not
listening, and the cloudflared egress policy retains its narrow TCP 22351
router permission. The platform API and router each have one ready replica;
namespace quota remains at its pre-existing 10-pod / 3600m CPU limit. No
Cloudflare origin, authority mode, P4 state, or broker state was changed.
The legacy `control-api` deployment remains ready but is no longer selected
for public `/api/v1/*` routes; its process retirement remains deferred. No
canonical optimization register is present in this lineage, so Control API
items and legacy retirement are handed off for later register reconciliation.

This cutover changes route authority, not trading behavior. The existing
`platform-signals-api` pod now also serves the Platform Control API; a
`platform-control-api` Service alias selects that same pod. This avoids
consuming another namespace pod slot. Signal handlers remain dispatched to
the pre-existing canonical signal implementation.

| Public route pattern | Route target | Authority | Expected result |
|---|---|---|---|
| `/api/v1/signals`, `/api/v1/signals/*` | `platform-signals-api:22350` | PostgreSQL `strategy.entry_signals`, schema 012 | Existing response and provenance preserved |
| `/api/v1/system` | `platform-control-api:22350` | Mode ConfigMap plus read-only PostgreSQL | 200; explicit component statuses |
| `/api/v1/safety` | `platform-control-api:22350` | Mode ConfigMap plus read-only PostgreSQL | 200 SAFE for PRIMARY/DB_PRIMARY/DISABLED; 503 if canonical source unavailable; fail-closed BLOCKED if authority config differs |
| `/api/v1/strategies`, `/api/v1/strategies/{id}` | `platform-control-api:22350` | Current mounted platform strategy configuration | 200 for list/existing strategy, 404 for canonical config miss, 503 if config unavailable |
| `/api/v1/strategies/{id}/report`, `/instances`, `/shadow` | `platform-control-api:22350` | No canonical observability/history source | 503 explicitly; no historical-file fallback |
| `/api/v1/events`, `/api/v1/events/{id}` | `platform-control-api:22350` | PostgreSQL `platform.outbox_events` | 200 canonical rows; 404 canonical detail miss; 503 on source failure |
| `/api/v1/audit`, `/api/v1/audit/{id}` | `platform-control-api:22350` | No canonical administrative audit source | 503 explicitly |
| `/api/v1/executions`, `/api/v1/executions/{id}`, `/metrics` | `platform-control-api:22350` | PostgreSQL `execution.intents` and `execution.results` | 200 with INACTIVE while authority is DISABLED; canonical 404 detail miss; 503 on database failure |
| `/api/v1/broker/*`, `/api/v1/exposure` | `platform-control-api:22350` | Bridge-owned facts; read-only bridge unavailable from this workload | 503 explicitly; never read 22348 or fabricate broker state |
| `/api/v1/connections` | `platform-control-api:22350` | PostgreSQL reachability plus bridge availability | 200 partial/degraded; data bridge UNAVAILABLE and execution channel INACTIVE |
| `/api/v1/reports`, `/api/v1/reports/{id}` | `platform-control-api:22350` | No canonical report registry | 503 explicitly |
| Other `/api/v1/*` | `platform-control-api:22350` | No route contract | 404; no legacy fallback |
| Non-API paths | Legacy origin, unchanged | Existing behavior | Unchanged |

The platform API pod already has narrowly scoped ingress from the router and
egress to PostgreSQL. The router's existing egress rule selects that pod on
TCP 22350, so the Service alias requires no NetworkPolicy widening. There is
no connection to port 22348 in the implementation.

## Temporary remaining-optimization handoff

No `docs/engineering/OPTIMIZATION_REGISTER.md` existed when this work began;
this list is intentionally a handoff note, not a competing canonical
register.

- Add a canonical administrative audit source only when the owning platform
  workflow emits durable actor/action/result records.
- Add a canonical report registry before replacing the explicit unavailable
  report responses.
- Make the MT5 read-only bridge reachable through a documented, narrowly
  authorized network path before implementing broker proxy reads.
- Add live JetStream health evidence to `/system` only if an existing
  credential-safe health source is available; current API reports it UNKNOWN.
- Preserve the single-replica, no-surge rollout because the namespace quota
  is full; this trades brief API interruption for avoiding extra capacity.

P4 runtime wiring is intentionally not incorporated. Its workload/config
changes may overlap this branch's `deploy/platform_api/workload.yaml` only if
the P4 branch independently changes the same API deployment. Rebase/merge
must retain both the P4-owned runtime changes and this API-only ConfigMap
mount, env, Service alias, and route changes without enabling execution.
