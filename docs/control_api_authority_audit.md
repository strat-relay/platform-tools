# Control API Authority Audit

**Audit date:** 2026-09-22\
**Trading-platform baseline:** `d3edcbb519c66ac3643d87d4cdf1e4936663d793` (`main`)\
**Kubernetes:** context `local`, namespace `trading`\
**Scope:** read-only source/deployment audit. No application, cluster, account, or broker state was modified.

## Executive summary

The deployed Control API is a hybrid read facade that still consumes legacy runtime files and MT5 bridge endpoints. It does not read canonical PostgreSQL or JetStream anywhere in the deployed request path. Its `/signals` endpoint reads `runtime/orchestration/signals.jsonl`; the Console adapter explicitly treats that file as ground truth. This is incompatible with the current post-cutover contract, where PostgreSQL `strategy.entry_signals` is authoritative and the JSONL file is non-authoritative compatibility output.

The live Kubernetes service returned HTTP 200 with an application-level degraded error for `/safety`, identifying `/app/runtime/execution/state.json` as absent. The same class of problem appears on every legacy execution endpoint. Broker endpoints also return HTTP 200 with `degraded=true` and null data because the mounted platform config points both bridge channels at `127.0.0.1:9`. HTTP success therefore does not mean the endpoint has current or usable data.

No deployed Control API endpoint reads PostgreSQL or JetStream. The Console's real signal path is therefore non-canonical. At the audit snapshot, `/signals` returned 88 JSONL rows; PostgreSQL had 8 `strategy.entry_signals` rows after T0, while the JSONL projection had 2 timestamped rows after T0 and zero matching IDs with those 8 canonical rows. Historical rows explain some total-count difference, but the post-T0 ID mismatch confirms the endpoint cannot be relied on as a canonical signal read.

The deployed image is labeled and named as an `mt5-native-bridge` artifact, but its embedded `app.py` hash does not match the declared Git commit or any tracked `control_api/app.py` version found in the bridge checkout history. The exact build source is unresolved. The `trading-platform` baseline has newer route code, including three strategy-observability routes, but that code is not present in the running image; those paths returned 404.

**Recommendation:** split ownership (option C). The platform should own its domain/control API; the bridge should own the broker-facing MT5 API. Do not move or deploy code as part of this audit. First reconcile deployed image provenance, then define endpoint-specific post-cutover sources and implement against those contracts.

## 1. Deployed workload and local listener

### Kubernetes service

- Workload: Deployment `control-api`, pod `control-api-6db978db5f-8kvq7`, 1/1 Ready.
- Image reference: `localhost:5001/mt5-native-bridge-control-api@sha256:4bcc40645b32dc5bcb06745485ea97e6b48f4ed3590dce29c47513ce29f451a0`.
- Runtime image ID: `sha256:703312630c49721e86a6b7ac2149647a5c20d42c51959382c5a419576ffcbb22`.
- Kubernetes labels: `app.kubernetes.io/part-of=mt5-native-bridge`; `git-commit=b7dfd1da99b0af64ebe9ce43bd3520b39e21698a`.
- Entrypoint: container PID 1 is `python -m control_api`; the image supplies the command because the Deployment has no `command` or `args` override.
- Code location: `/app/control_api/app.py`, SHA-256 `0b579e2bf2bb7e4be1268f3c1ef561ee270335a0ef02d56e937f3e9cbba6f62d`.
- The deployed container has no `/app/control_api/observability.py`.
- Port and service: container/service port `22349`, ClusterIP Service `control-api` at `10.43.116.41`.
- Declared source repository: `mt5-native-bridge`, proven by the image name and workload labels. Exact build-source revision is **not proven**: the embedded app hash does not match the `b7df...` version of `control_api/app.py`, nor any version in the bridge checkout's reachable history examined for this audit. This is a source-provenance blocker, not proof of malicious or hand-edited image content.
- `/app/orchestration/config/platform.json` is a ConfigMap mount; `/app/runtime` is a read-only `mt5-native-bridge-runtime` PVC mount; the build manifest is a separate ConfigMap mount. The platform config currently contains `execution_mode=SHADOW`, an empty strategy list, and both bridge URLs at `http://127.0.0.1:9/mcp`. It does not contain the independent current runtime mode triplet.

### Local IPv4 listener on 127.0.0.1:22349

- Process: PID `52668`, Python 3.14, command `/.../Python -m control_api`.
- Working directory/source checkout: `/Users/caleb/mt5-native-bridge`.
- Local `/api/v1/system` reported `started_at=2026-09-20T00:04:15.476536+00:00` and source paths rooted at that checkout's runtime directory.
- Local `/api/v1/safety` returned HTTP 200 with `data.status=BLOCKED`, `execution_enabled=true`, legacy `REAL_EXECUTION`/armed state, and blocker `EXECUTION_BRIDGE_UNHEALTHY`.
- Exact loaded source version is **unknown**. The process started before the current app file modification time; the current bridge checkout is `5d4b018857794da8bcf1a9161876c1dc6fe31f73`, which does not identify already-imported process code.
- It is **not the same observed service state as Kubernetes**: local safety returns the legacy blocked payload, while Kubernetes returns a `SOURCE_UNAVAILABLE` envelope for a missing legacy execution file. Do not use the pre-existing local listener to infer current Kubernetes state. It was not stopped or modified.

## 2. Endpoint inventory and classifications

The live deployed handler exposes **24 GET route patterns** below (including the `/exposure` compatibility alias). The `trading-platform` source at the requested baseline also contains three newer nested strategy routes; they are **repo-only, not deployed**, because the live image lacks the observability module and does not parse the nested route segment. Counts below cover the 24 deployed GET route patterns only.

Classification counts:

- A `CANONICAL_AND_VALID`: 0
- B `VALID_COMPATIBILITY_PROJECTION`: 0
- C `STALE_AUTHORITY`: 8
- D `SOURCE_UNAVAILABLE`: 14
- E `MIXED_AUTHORITY`: 0
- F `FUTURE_COMPONENT_DEPENDENCY`: 1
- G `UNKNOWN`: 1

No route qualifies as B: none marks its output as a non-authoritative projection with a freshness/high-water indicator and explicit stale/unavailable semantics. The routes that read projections return ordinary success envelopes when files exist.

### Live route records

All routes are GET-only. `control_api.app._Handler` rejects POST/PUT/PATCH/DELETE with 405 `READ_ONLY_API`; OPTIONS is CORS preflight (204), not a data endpoint. No route performs a broker write. `BridgeReader.call` independently allowlists only MT5 read tools.

**GET `/api/v1/system` — G, process liveness only.** Implementation: `ControlApi.execute` (`control_api/app.py`). Purpose: report the API process identity/start time and configured source paths. Observed HTTP 200, not degraded. Source: process memory plus platform config loaded from `/app/orchestration/config/platform.json`; it does not test PostgreSQL, JetStream, MT5, or runtime authority. Fallback: none beyond config-read error -> 503. State writes: no. Broker dependency: no. Proposed source/semantics: keep as a narrowly named API liveness/config diagnostic; do not treat it as platform health or authority evidence.

**GET `/api/v1/safety` — D.** Implementation: `ControlApi.safety -> RuntimeSources.state -> _read_json`, then `BridgeReader.health` if required file reads succeed. Purpose: execution safety/consistency projection. Observed HTTP 200 with body `error=SOURCE_UNAVAILABLE`, `degraded=true`, and `source=/app/runtime/execution/state.json`. The dispatcher wraps this payload in HTTP 200 even though the helper knows the source failed. Sources expected by code: legacy orchestration manifest/state; legacy execution state, real-state and resume JSON; build manifest; execution-bridge health; optional execution event JSONL. No PostgreSQL or NATS reads. Fallback: build-manifest absence maps capability to UNKNOWN; required execution state has no fallback. State writes: no. Broker dependency: execution bridge health. Proposed contract: see §6–7; must represent independent authority modes and UNKNOWN/unavailable inputs explicitly, without recreating disabled execution state.

**GET `/api/v1/strategies` — C.** Implementation: `ControlApi._strategy_rows`. Purpose: configured strategy cards. Observed HTTP 200 with an empty list. Source: static platform JSON configuration; for Context cards, code can additionally read legacy resume state, signals/events JSONL, and a cohort JSON. Authority: not canonical strategy registry and not aligned to independent current modes. Fallback: config read error -> 503; strategy-specific optional cohort errors are swallowed into empty defaults, while signal/event file errors can escape the summary path. State writes: no. Broker dependency: no direct dependency. Proposed source: PostgreSQL strategy registry/configuration and platform-owned status; any runtime telemetry must be separately labeled with provenance/freshness.

**GET `/api/v1/strategies/{strategy_id}` — C.** Implementation: same list/filter path as strategies. Purpose: one strategy card. Observed 404 for the probed Context ID because the deployed config list is empty. Source, fallback, authority, and dependencies are the same as `/strategies`; no broker write. Proposed source: same canonical registry plus a separately sourced status projection.

**GET `/api/v1/signals` — C.** Implementation: `ControlApi._rows -> RuntimeSources.rows("signals")`. Purpose: Console signal list. Observed HTTP 200 with 88 rows. Source: `/app/runtime/orchestration/signals.jsonl`. Authority: explicitly non-authoritative compatibility output; the API does not query PostgreSQL or identify the JSONL projection in the response. Fallback: none; missing file -> 503. State writes: no. Broker dependency: no. Proposed source: PostgreSQL `strategy.entry_signals`; JetStream may support live notifications/cache invalidation, but must not replace durable DB authority. Do not fall back silently to JSONL.

**GET `/api/v1/signals/{signal_id}` — C.** Implementation: same signals JSONL load and ID filter. Purpose: one Console signal. Observed 404 for the probe ID. Data source, missing-file behavior, authority and proposed contract are the same as `/signals`; no broker dependency or writes.

**GET `/api/v1/events` — C.** Implementation: `ControlApi._rows -> RuntimeSources.rows("events")`. Purpose: operational event timeline. Observed HTTP 200 with 436 rows. Source: `/app/runtime/orchestration/events.jsonl`. Authority: legacy JSONL; endpoint does not expose canonical event provenance or projection freshness. Fallback: none; missing file -> 503. State writes: no. Broker dependency: no. Proposed source: classify event types. Durable domain events/audit facts should come from PostgreSQL; JetStream is appropriate for real-time transport/observations, with durable IDs and DB linkage. Do not use a JSONL fallback.

**GET `/api/v1/events/{event_id}` — C.** Implementation: same event JSONL load and ID filter. Purpose: one event. Observed 404 for probe ID. Data source/authority/fallback and proposed source match `/events`; no broker dependency or writes.

**GET `/api/v1/audit` — C.** Implementation: event route alias (`audit` maps to the same `events.jsonl`). Purpose: audit/event listing. Observed HTTP 200 with 436 rows, identical source count to `/events`. Source: legacy event JSONL, not a dedicated administrative audit log. Fallback: none; missing file -> 503. State writes: no. Broker dependency: no. Proposed source: persist durable actor/action/result/reason audit facts in PostgreSQL. Keep operational event transport separate from audit history.

**GET `/api/v1/audit/{event_id}` — C.** Implementation: same alias and `event_id` filter. Purpose: one audit/event record. Observed 404 for probe ID. Data source/authority/fallback and proposed source match `/audit`; no broker dependency or writes.

**GET `/api/v1/executions` — D.** Implementation: `ControlApi.execute` concatenates `execution_intents`, `execution_decisions` and `execution_skips`. Purpose: execution workflow history. Observed HTTP 503 `SOURCE_UNAVAILABLE` at `/app/runtime/execution/execution_intents.jsonl`. Sources are legacy JSONL, not canonical workflow state. No fallback. State writes: no. Broker dependency: no direct call. Proposed source: PostgreSQL execution intent/decision/attempt records, when present; current DISABLED mode can return an explicit empty/disabled result only if canonical state proves that, otherwise UNKNOWN.

**GET `/api/v1/executions/metrics` — D.** Implementation: counts execution JSONL decisions/events/trades/skips. Purpose: execution aggregate metrics. Observed HTTP 503 at the missing `execution_intents.jsonl`. Source: legacy execution JSONL (`real_trades.jsonl`, event/decision/skip logs included). No fallback. State writes: no. Broker dependency: no live call; it counts locally recorded broker actions. Proposed source: PostgreSQL workflow records for submitted/decided intents, and broker-held/account evidence for observed fills; never infer execution counts from a missing file.

**GET `/api/v1/executions/{id}` — D.** Implementation: same execution JSONL concatenation then ID filter. Purpose: one intent/decision/signal-linked execution row. Observed HTTP 503 before filtering because `execution_intents.jsonl` is absent. Proposed source: PostgreSQL execution records with explicit status and broker reconciliation references. No fallback, state writes or direct broker call.

**GET `/api/v1/broker/account` — D.** Implementation: `BridgeReader.call("mt5_account_info")`. Purpose: actual broker account facts. Observed HTTP 200, degraded, null data; unavailable text `SOURCE_UNAVAILABLE: MT5 read unavailable`. Configured provider is `mcp_url=http://127.0.0.1:9/mcp`, which refused connection. Authority: broker-held/account reality, but no broker read succeeded. Fallback: none. State writes: no. Broker dependency: read-only bridge. Proposed source: MT5 bridge account-read endpoint; unavailable must remain UNKNOWN/degraded or non-2xx, not masquerade as ordinary success.

**GET `/api/v1/broker/positions` — D.** Implementation: `mt5_positions`; purpose: actual open positions. Observed HTTP 200 degraded/null for the same refused provider. Broker is the source of truth; no fallback, no writes. Proposed source: MT5 bridge read.

**GET `/api/v1/broker/pending-orders` — D.** Implementation: `mt5_orders`; purpose: actual broker orders. Observed HTTP 200 degraded/null for the same refused provider. Broker is the source of truth; no fallback, no writes. Proposed source: MT5 bridge read.

**GET `/api/v1/broker/history-orders` — D.** Implementation: `mt5_history(limit=500)`; purpose: broker order history. Observed HTTP 200 degraded/null. Broker is the source of truth; no fallback, no writes. Proposed source: MT5 bridge read with explicit page/retention semantics.

**GET `/api/v1/broker/deals` — D.** Implementation: `mt5_history(limit=500)`; purpose: broker deal/fill history. Observed HTTP 200 degraded/null. Broker is the source of truth; no fallback, no writes. Proposed source: MT5 bridge read with clear distinction between orders and deals.

**GET `/api/v1/broker/symbols` — D.** Implementation: `mt5_symbols`; purpose: broker/instrument list. Observed HTTP 200 degraded/null. Broker is the source of truth; no fallback, no writes. Proposed source: MT5 bridge read.

**GET `/api/v1/broker/exposure` — D.** Implementation: `mt5_positions`, returned as `{positions, derived:false}`. Purpose: broker-position view, not computed exposure. Observed HTTP 200 degraded with unavailable positions. Broker is the source of truth; no fallback or writes. Proposed source: MT5 bridge positions; rename/document if true exposure is later calculated from canonical positions and risk policy.

**GET `/api/v1/exposure` — D (legacy alias).** Implementation and observed result are identical to `/broker/exposure`. No canonical state or fallback. Proposed contract: retain only as an explicitly documented alias during compatibility; the Console's intended path is `/broker/exposure`.

**GET `/api/v1/connections` — F.** Implementation: `BridgeReader.health` for both data and execution channels. Purpose: channel-level health and lifecycle counters. Observed HTTP 200 degraded; both configured `127.0.0.1:9` health URLs refused connection, resulting in UNKNOWN channel status. It also probes the execution bridge regardless of `EXECUTION_AUTHORITY_MODE=DISABLED`. State writes: no. Broker dependency: bridge health. Proposed source: bridge health for the data channel; mark execution channel `NOT_REQUIRED` when execution authority is disabled. Do not require an intentionally stopped 22348 service to claim P2 signal-path health.

**GET `/api/v1/reports` — D.** Implementation: hard-coded no-registry response. Purpose: report listing. Observed HTTP 200 degraded with `available=[]` and `report_registry` unavailable. Source: no registry. Fallback: fabricated-by-contract empty listing is labeled degraded but can still be mistaken for “no reports.” State writes/broker dependency: no. Proposed source: PostgreSQL report metadata plus separately identified artifact storage; without a registry, return explicit unavailable/UNKNOWN rather than a clean empty collection.

**GET `/api/v1/reports/{report_id}` — D.** Implementation: hard-coded unavailable response. Purpose: report detail. Observed HTTP 503 `SOURCE_UNAVAILABLE` from `report_registry`. No source/fallback, writes or broker dependency. Proposed source: canonical report registry and immutable report artifact; preserve unavailable/not-found distinction.

### Source-only routes in trading-platform HEAD (not deployed)

The baseline source adds `sub_resource` routing and `control_api/observability.py`. These paths returned 404 against the deployed image and must not be represented as deployed capabilities:

- **GET `/api/v1/strategies/{instance_id}/report` — C if enabled as currently implemented.** Calls strategy runner `build_standard_report()` implementations. Those read strategy-owned local state/heartbeat/manifest and JSONL events; Context also reads compact runner state, and liquidity adapters read instance state/daily/events files. Proposed source: canonical strategy registry plus a normalized, provenance-bearing PostgreSQL observation/report model; keep any runner file report explicitly compatibility-only.
- **GET `/api/v1/strategies/{family_id}/instances` — G if enabled as currently implemented.** Uses a static Python registry and state-file existence to declare an instance live; the Context instance is hard-coded live. Proposed source: PostgreSQL strategy/instance registry for identity/configuration and runtime supervisor/heartbeat for liveness; file existence alone is not liveness.
- **GET `/api/v1/strategies/{strategy_id}/shadow` — F.** Imports the Phase7 observer report, which reads Phase7 state/events/heartbeat/PID files. Phase7 is intentionally disabled. Proposed source: do not expose as active until the platform has an enabled observation contract; then use platform-owned observation records. Until then return an explicit `NOT_CONFIGURED`/UNKNOWN result.

## 3. Runtime source inventory

The deployed `RuntimeSources` uses a single filesystem root (`/app/runtime` in Kubernetes, `<checkout>/runtime` locally); it has no PostgreSQL or JetStream provider.

- **PLATFORM_CONFIG:** `orchestration/config/platform.json`. Supplies `strategies`, `execution_mode`, `mcp_url`, `execution_mcp_url`, and related flags. The deployed copy is a ConfigMap mount and currently says SHADOW with both bridge URLs set to port 9. It is a stale/static config view of the independent runtime authority modes.
- **LEGACY_JSONL:** orchestration `signals.jsonl`, `events.jsonl`, `route_decisions.jsonl`, `sizing_decisions.jsonl`, `distribution_queue.jsonl`, `account_snapshots.jsonl`.
- **LEGACY_JSONL / LEGACY_RUNTIME_STATE:** execution `execution_intents.jsonl`, `execution_decisions.jsonl`, `execution_skips.jsonl`, `events.jsonl`, `real_trades.jsonl`, plus `pid` and `heartbeat.json`.
- **LEGACY_RUNTIME_STATE:** orchestration `state.json`, `manifest.json`; execution `state.json`, `real_state.json`, `real_execution_resume.json`.
- **LEGACY_FILESYSTEM:** `artifacts/MT5TradingBridge_execution_build_manifest.json`; root-level Context cohort JSON; strategy runner state, manifest, heartbeat, PID, daily and event files in source-only observability code.
- **PHASE7:** `context_structure_retrace_phase7_state.json`, `context_structure_retrace_phase7.jsonl`, observer heartbeat/PID, consumed by source-only shadow report.
- **EXECUTION_BRIDGE / BROKER_READ:** `BridgeReader` health and read-only JSON-RPC tools via the `mcp_url` and `execution_mcp_url` from platform config. The deployed URLs are both loopback port 9; no actual broker read was completed.
- **CANONICAL_POSTGRES:** not read by any Control API handler/provider. Independently verified in this audit only: PostgreSQL schema migration 012 is installed; `strategy.entry_signals` count is 8; all 8 have `decision_time > T0`; 8 signal-related outbox events were present and none were pending.
- **CANONICAL_JETSTREAM:** not read by any Control API handler/provider. Independently verified in this audit only: relay connected to `TRADING_CORE`; stream had 16 messages at the probe time.
- **CANONICAL_PROJECTION:** none is explicitly identified by the API response schema. Although signals JSONL is intended as compatibility output, the API does not expose projection watermark, source authority, generation, or staleness.
- **UNKNOWN:** no strategy/report registry provider exists for `/reports`; safety authority semantics after independent modes are not represented.

The deployed API catches broker read exceptions and returns HTTP 200 with `degraded=true`, `data=null` and an unavailable string. `/safety` also returns HTTP 200 around a source-error object. Execution and report-by-ID source errors use HTTP 503. These inconsistent envelopes must be normalized in any future contract.

## 4. Read-only route exercise

The 24 deployed GET templates were exercised through a dedicated local port-forward at port 22350 to the Kubernetes `control-api` Service. No mutating method was called. Results:

- `/system`: 200, not degraded.
- `/safety`: 200, body `error=SOURCE_UNAVAILABLE`, missing `/app/runtime/execution/state.json`.
- `/strategies`: 200, 0 rows; strategy detail probe: 404.
- `/signals`: 200, 88 rows; detail probe: 404.
- `/events` and `/audit`: 200, 436 rows each; detail probes: 404.
- `/executions`, `/executions/metrics`, and execution detail: 503, missing `/app/runtime/execution/execution_intents.jsonl`.
- All six broker routes plus both exposure aliases: 200, degraded, null source data, `SOURCE_UNAVAILABLE: MT5 read unavailable`.
- `/connections`: 200, degraded; both configured endpoints refused connections.
- `/reports`: 200, degraded, no registry; report detail: 503.
- The three strategy-observability routes present in platform source but absent from deployed source returned 404.
- OPTIONS preflight returned 204. No API route was found that can submit a broker operation; the GET broker tool allowlist is read-only.

Control API logs produced no relevant application messages during the probe. The exact missing source is included in the `/safety` body. The local IPv4 process on port 22349 was separately left untouched; port-forward probes used port 22350 to avoid the existing listener.

## 5. Console signal path and divergence

- Console real-mode client (`trading-ops-console/src/api/RealTradingApi.ts`) calls `GET /signals` for `getSignals`; `getStrategies` calls both `/strategies` and `/signals`; dashboard loading also requests `/signals`.
- Console adapter documentation explicitly says its signal ground truth is `runtime/orchestration/signals.jsonl` (`src/api/adapters.ts`). Its adapter infers “published” from endpoint inclusion and leaves execution status unknown; it does not reconcile with the canonical signal table.
- `CONSOLE_SIGNAL_SOURCE`: Control API `/api/v1/signals` -> `runtime/orchestration/signals.jsonl`.
- `CONSOLE_SIGNAL_SOURCE_CANONICAL`: NO.
- `SIGNALS_JSONL_STILL_REQUIRED_BY_CONTROL_API`: YES. `/signals` and `/signals/{id}` call `_read_jsonl` with no DB fallback. Context strategy summary also reads signals JSONL when configured.
- `SIGNALS_JSONL_DIVERGENCE_RISK`: CONFIRMED. At audit time, 88 projection rows existed; only 2 were timestamped after T0. PostgreSQL contained 8 canonical rows after T0. The 2 JSONL post-T0 IDs had zero overlap with the 8 canonical IDs. The file has no endpoint-exposed high-water/reconciliation evidence. Historical rows in the 88-row total make whole-table totals non-comparable, but the post-T0 key comparison proves the Console path misses canonical post-cutover rows.
- The Console worktree was already dirty during this audit. It was read only; none of its concurrent/uncommitted changes were modified.

## 6. Proposed post-cutover source contract

1. **Durable platform/business state:** PostgreSQL. Signals use `strategy.entry_signals`; strategy identity/configuration and durable execution workflow/report metadata should use their canonical platform tables. Never silently fall back to JSONL when DB is missing or empty.
2. **Real-time event delivery:** JetStream for events/notifications where appropriate. Treat it as transport/observation, not business-state authority. Durable audit/business facts must be queryable in PostgreSQL and linked by event ID.
3. **Broker reality:** MT5 bridge read API for current account, quotes, positions, orders, deals, and symbols. These facts should remain broker-sourced, with explicit freshness/error status. Do not store a snapshot and present it as current broker truth.
4. **Platform runtime/mode state:** distinguish desired mode/config from observed process mode/heartbeat. Source desired deployment modes from the platform's declared runtime deployment configuration; source live mode/readiness from the running platform process/health contract. Both need provenance and observation time. A static legacy `platform.json` is not sufficient.
5. **Compatibility projections:** only serve for explicitly display-oriented endpoints. Include source kind (`COMPATIBILITY_PROJECTION`), generated time, canonical watermark/high-water, and freshness/lag status. Stale/missing must be surfaced, never silently returned as canonical data.
6. **Execution permission/fencing:** PostgreSQL/NATS/legacy files cannot simulate broker-held fencing. OD-06 remains unresolved. An enabled execution mode cannot be declared SAFE until the broker-held fence is independently verified; if evidence is absent, report UNKNOWN/BLOCKED.
7. **Reports/performance:** registry and durable outcomes from PostgreSQL, with immutable report artifacts identified separately. A missing report registry must be unavailable, not an empty “no reports” success. Strategy file reports may remain development-only projections with provenance.
8. **Status semantics:** envelope transport status, resource status, authority, freshness, and degraded reasons must be separate. HTTP 2xx should mean the request was processed, not imply its domain data is valid; clients must inspect resource status and provenance.

## 7. `/safety` semantics proposal (design only)

Represent three independent dimensions on every response: `ORCHESTRATOR_MODE`, `SIGNAL_AUTHORITY_MODE`, and `EXECUTION_AUTHORITY_MODE`, each with source, observation timestamp, and confidence. For the current `PRIMARY / DB_PRIMARY / DISABLED` configuration:

- Prove configured modes and compare them to observed primary orchestrator heartbeat/process mode. If either source is missing or conflicts, report UNKNOWN/BLOCKED with the exact evidence; do not infer from a missing execution file.
- Verify the canonical PostgreSQL dependency and signal authority (schema/read-only query health, current primary signal counts/high-water, and transactional outbox status). Verify JetStream connectivity/stream only as event-transport health, not as signal truth.
- Report execution as DISABLED from the active execution-authority configuration and observed service inventory. In that mode, `execution/state.json`, `real_state.json`, resume/armed files, 22348, execution consumer, Trade Manager, and Phase7 are NOT_REQUIRED; their absence is not a source error.
- Do not convert “execution disabled” into overall SAFE. Return a structured state such as `execution_permission=DISABLED`, `signal_authority=HEALTHY|DEGRADED|UNKNOWN`, `orchestrator=...`, and overall `SAFE|BLOCKED|UNKNOWN` from explicit required checks. A required but unavailable DB/config/heartbeat means UNKNOWN/BLOCKED, never SAFE.
- If execution authority becomes enabled in a future phase, add the execution workflow checks actually required by that mode and require broker-held fence evidence per OD-06. Do not treat disabled 22348 health as a blocker while execution is disabled; do not invent a local/database substitute for the broker-held fence.

This is a contract proposal, not an implementation decision about which Kubernetes API/service account will provide desired-mode and live-process evidence. That interface must be explicitly selected before changing `/safety`.

## 8. Ownership boundary and migration impact

**Recommendation: C — split APIs with explicit ownership.**

- **Trading platform owns the platform/control API:** system runtime/authority status, strategy registry and observability, canonical signals, events/audit, execution workflow, platform safety, and report registry. It queries PostgreSQL for durable state and uses JetStream for transport status/real-time indications as appropriate.
- **MT5 bridge owns a bridge API:** terminal/bridge health, account/broker state, quotes, positions, orders, deals, symbols, and MT5 execution primitives. The bridge validates future broker-held fence proofs but does not own platform strategy/execution policy.
- Console may compose the two APIs, but should not have the bridge become the owner of platform-level safety or strategy state. Existing `/broker/*` calls should be routed to the bridge API (or temporarily proxied by platform with a typed bridge client and clear provenance). Execution workflow records remain platform-owned; actual fills/positions remain broker-sourced.

Migration impact: establish separate API contracts and versioned clients; move platform-specific handlers/providers and strategy observability to the trading-platform deployment; keep broker read/bridge health in the bridge deployment; configure broker URL from an explicit service binding rather than loopback port 9; reconcile image provenance; then migrate Console route consumers and error/provenance handling. This audit performs none of those moves.

## 9. Audit controls and evidence

- Runtime configuration rechecked: `PRIMARY`, `DB_PRIMARY`, both DB/JetStream primary flags true, `EXECUTION_AUTHORITY_MODE=DISABLED`.
- PostgreSQL accepted connections; latest migration row was 012; schema query showed 8 canonical entry signals after T0 and 8 corresponding signal-related outbox rows, none pending.
- Relay connected to canonical `TRADING_CORE` (16 stream messages at probe time).
- `P2_SIGNAL_AUTHORITY_CUTOVER_STILL_VALID=YES` based on the observed runtime flags, primary orchestrator process, canonical DB/outbox records, and healthy relay/JetStream connection. The Control API source failure is a stale read-model issue, not evidence of P2 authority failure.
- No mutating HTTP method, migration, workload restart, authority/config edit, execution start, cutoff/T0 action, or broker write was performed. Broker writes attributable to this audit: **0**.
- `PRODUCTION_CHANGED=false`.
- Relevant files inspected: `control_api/app.py`, `control_api/observability.py`, `platform_runtime.py`, runner report/state readers, migrations 008/011/012, Console `RealTradingApi.ts`, `endpoints.ts`, and `adapters.ts`.

## 10. Blockers before implementation

1. Reproducible provenance for deployed digest `4bcc...` is missing: declared Git commit and embedded source hash disagree.
2. The currently served API artifact comes from the MT5 bridge-labeled workload, while the requested platform baseline contains newer but undeployed routes. Decide the migration/release owner before changing either copy.
3. Define the source interface for desired independent mode configuration versus live orchestrator/runtime observation.
4. Define event/audit taxonomy and durable PostgreSQL ownership for `/events` and `/audit` before implementing a replacement reader.
5. Canonical signal API cutover is required: current Console data demonstrably diverges from post-T0 PostgreSQL signal IDs.

**COMMIT:** this audit artifact is the only change on its isolated audit branch; the handoff includes its commit hash. Production and the concurrent Console worktree remain untouched.
