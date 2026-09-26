# Control API Canonical Cutover Route Matrix

**Audit date:** 2026-09-22  
**Source baseline:** `4d665b82172ae9075c2d3929a898325dff868c7a`  
**Live endpoint probed:** `https://api.stratrelay.app`  
**Audit method:** read-only GET probes; no application or production state was changed.

The public router sends `/api/v1/signals` and its detail paths to the
Platform Signal API. At this baseline, every other `/api/v1/*` path falls
through to the legacy `control-api`. The deployed legacy image provenance is
not reproducible, so current-source descriptions below follow the prior
read-only audit and are paired with a fresh public HTTP probe.

| ROUTE | CURRENT_OWNER | CURRENT_SOURCE | CURRENT_STATUS | TARGET_OWNER | TARGET_AUTHORITY | ACTION |
|---|---|---|---|---|---|---|
| `GET /api/v1/system` | Legacy Control API | Process memory and platform config; no platform/DB health | 200, liveness-only | Platform | Deployed authority config plus PostgreSQL/JetStream health; distinguish component states | Migrate; do not report API liveness as platform health |
| `GET /api/v1/safety` | Legacy Control API | Runtime JSON state and bridge health | 200 with `SOURCE_UNAVAILABLE` | Platform | Authority config plus required canonical-source checks; disabled execution is `INACTIVE` | Migrate; fail closed on unknown authority, never infer safety from missing files |
| `GET /api/v1/strategies` | Legacy Control API | Static config plus stale runtime projections | 200, empty list | Platform | Canonical relational strategy/config registry | Migrate; if the registry is absent, return explicit `UNAVAILABLE`, not stale data |
| `GET /api/v1/strategies/{strategy_id}` | Legacy Control API | Same config/projection as list | 404 for probed strategy | Platform | Canonical relational strategy/config registry | Migrate; distinguish source unavailable from a canonical not-found |
| `GET /api/v1/signals` | Platform Signal API | PostgreSQL `strategy.entry_signals` | 200, `source=canonical_postgres` | Platform | Canonical PostgreSQL, schema 012 | Preserve unchanged |
| `GET /api/v1/signals/{signal_id}` | Platform Signal API | PostgreSQL `strategy.entry_signals` | 404 for probe ID; route active | Platform | Canonical PostgreSQL, schema 012 | Preserve unchanged |
| `GET /api/v1/events` | Legacy Control API | `runtime/orchestration/events.jsonl` | 200, legacy event rows | Platform | PostgreSQL `platform.outbox_events` for durable domain events | Migrate; do not fall back to JSONL |
| `GET /api/v1/events/{event_id}` | Legacy Control API | Same event JSONL | 404 for probe ID | Platform | PostgreSQL `platform.outbox_events` | Migrate with canonical detail lookup |
| `GET /api/v1/audit` | Legacy Control API | Alias of `events.jsonl`, not an audit log | 200, same legacy rows as `/events` | Platform | No canonical administrative audit source exists | Return explicit `UNAVAILABLE`; do not relabel outbox events as audit facts |
| `GET /api/v1/audit/{event_id}` | Legacy Control API | Alias of `events.jsonl` | 404 for probe ID | Platform | No canonical administrative audit source exists | Return explicit `UNAVAILABLE` |
| `GET /api/v1/executions` | Legacy Control API | Execution JSONL files | 503 `SOURCE_UNAVAILABLE` | Platform | PostgreSQL execution workflow records plus execution authority config | Migrate; show disabled feature as `INACTIVE`, not source failure |
| `GET /api/v1/executions/metrics` | Legacy Control API | Counts derived from execution JSONL files | 503 `SOURCE_UNAVAILABLE` | Platform | PostgreSQL execution workflow records | Migrate; return canonical counts and explicit inactive state when disabled |
| `GET /api/v1/executions/{id}` | Legacy Control API | Execution JSONL files | 503 before ID lookup | Platform | PostgreSQL execution workflow records | Migrate; canonical not-found when source is healthy, otherwise unavailable |
| `GET /api/v1/broker/account` | Legacy Control API | Read-only bridge call configured to an unavailable endpoint | 200, degraded/null | Bridge (read via Platform facade) | MT5 account read through 22347 only | Explicitly unavailable until the read-only bridge is reachable; never use 22348 |
| `GET /api/v1/broker/positions` | Legacy Control API | Read-only bridge call configured to an unavailable endpoint | 200, degraded/null | Bridge (read via Platform facade) | MT5 positions through 22347 only | Explicitly unavailable until reachable; never fabricate positions |
| `GET /api/v1/broker/pending-orders` | Legacy Control API | Read-only bridge call configured to an unavailable endpoint | 200, degraded/null | Bridge (read via Platform facade) | MT5 orders through 22347 only | Explicitly unavailable until reachable |
| `GET /api/v1/broker/history-orders` | Legacy Control API | Read-only bridge history call configured to an unavailable endpoint | 200, degraded/null | Bridge (read via Platform facade) | MT5 history through 22347 only | Explicitly unavailable until reachable |
| `GET /api/v1/broker/deals` | Legacy Control API | Read-only bridge history call configured to an unavailable endpoint | 200, degraded/null | Bridge (read via Platform facade) | MT5 history through 22347 only | Explicitly unavailable until reachable |
| `GET /api/v1/broker/symbols` | Legacy Control API | Read-only bridge call configured to an unavailable endpoint | 200, degraded/null | Bridge (read via Platform facade) | MT5 symbols through 22347 only | Explicitly unavailable until reachable |
| `GET /api/v1/broker/exposure` | Legacy Control API | Alias over bridge positions (`derived=false`) | 200, degraded/null | Bridge (read via Platform facade) | MT5 positions through 22347 only | Preserve alias; explicitly unavailable until reachable |
| `GET /api/v1/exposure` | Legacy Control API | Alias over bridge positions | 200, degraded/null | Bridge (read via Platform facade) | MT5 positions through 22347 only | Preserve compatibility alias; explicitly unavailable until reachable |
| `GET /api/v1/connections` | Legacy Control API | Health probes of both configured bridge endpoints | 200, degraded; both unknown | Platform + Bridge | PostgreSQL/JetStream platform health; 22347 bridge health; execution 22348 is not required while disabled | Migrate; report data bridge unavailable and execution `INACTIVE` separately |
| `GET /api/v1/reports` | Legacy Control API | No report registry; hard-coded empty/degraded response | 200, degraded | Platform | No canonical report registry exists | Explicitly unavailable; never claim a clean empty report collection |
| `GET /api/v1/reports/{report_id}` | Legacy Control API | No report registry | 503 `SOURCE_UNAVAILABLE` | Platform | No canonical report registry exists | Explicitly unavailable; retain not-found only once a registry exists |

## Public routes consumed by Console but not deployed at audit time

The Console source also calls these strategy-observability paths. Live probes
returned 404 for all three, matching the previous audit's finding that they
were source-only rather than deployed capabilities. The cutover must not
reactivate their old filesystem/history implementations.

| ROUTE | CURRENT_OWNER | CURRENT_SOURCE | CURRENT_STATUS | TARGET_OWNER | TARGET_AUTHORITY | ACTION |
|---|---|---|---|---|---|---|
| `GET /api/v1/strategies/{id}/report` | Not deployed; legacy source-only | Strategy runtime report/history | 404 | Platform | No canonical report registry or migrated runtime history | Keep explicitly unavailable/retired; never read historical runtime files |
| `GET /api/v1/strategies/{id}/instances` | Not deployed; legacy source-only | Runtime instance/history projections | 404 | Platform | Canonical runtime-instance records (currently no rows) | Keep explicitly unavailable until canonical records exist |
| `GET /api/v1/strategies/{id}/shadow` | Not deployed; legacy source-only | Shadow/runtime JSONL projections | 404 | Platform | No canonical shadow report source is available | Keep explicitly unavailable/retired |

## Freshness/source checks made for the matrix

- Public GET probes returned 200 for 16 patterns (including eight
  broker/exposure routes that were degraded), 404 for four probed detail IDs,
  and 503 for the three execution routes plus report detail.
- PostgreSQL currently has schema version 012, 9 canonical EntrySignals,
  9 strategy candidates for `CONTEXT_STRUCTURE_RETRACE_V1`, no rows in
  `platform.strategy_versions` or `platform.configuration_versions`, 18
  published outbox rows, and zero execution intents/results.
- `platform.runtime_instances` currently has no rows; do not claim a live
  orchestrator heartbeat from that table.
- A read-only `/health` probe to `192.168.1.166:22347` from the Kubernetes
  workload network timed out. Until an authorized read-only path is proven,
  bridge-owned API data must be reported explicitly unavailable.
- Current configured mode values are `ORCHESTRATOR_MODE=PRIMARY`,
  `SIGNAL_AUTHORITY_MODE=DB_PRIMARY`, both DB/JetStream primary flags true,
  and `EXECUTION_AUTHORITY_MODE=DISABLED`.

Unknown `/api/v1/*` paths will be sent to the Platform API after cutover and
receive its explicit 404 response; they will no longer fall through to the
legacy Control API.
