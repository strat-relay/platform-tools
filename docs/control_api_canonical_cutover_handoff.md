# Control API canonical cutover handoff

Date: 2026-09-22
Baseline: `4d665b82172ae9075c2d3929a898325dff868c7a`

Implementation and local contract tests are present, but production
deployment/public verification are **not complete**. The local Kubernetes
GET endpoint responds, while both server-side dry-run requests timed out
reading API responses. No image was built/pushed and no workload/router or
NetworkPolicy was changed. Do not treat the routes below as deployed until a
later rollout and public verification succeed.

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
