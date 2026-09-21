# Trading Control API V1

The control API is a local, read-only observability service. It binds to
`127.0.0.1:22349` by default and never accepts a broker write or execution
control request. CORS is restricted to `http://localhost:5173` unless
`CONTROL_API_CORS_ORIGINS` is set (see [PROD_DEPLOY.md](PROD_DEPLOY.md)).

Start it from the repository root:

```sh
python3 -m control_api
```

API base URL: `http://127.0.0.1:22349/api/v1`

## Endpoints

`GET /system`, `/safety`, `/strategies`, `/strategies/{id}`, `/signals`,
`/signals/{id}`, `/executions`, `/executions/{id}`, `/executions/metrics`,
`/broker/account`, `/broker/positions`, `/broker/pending-orders`,
`/broker/history-orders`, `/broker/deals`, `/broker/exposure`, `/broker/symbols`,
`/connections`, `/reports`, `/reports/{id}`, `/events`, and `/audit`.

Signals, intents, decisions, skips, events, and audit records retain their
source IDs. A strategy observation is not a signal; a signal is not sizing; an
execution intent is not an OrderSend attempt; and an accepted order is not a
fill. Missing links and unavailable broker reads are returned as degraded or
unavailable rather than inferred.

Authoritative sources are `orchestration/config/platform.json`,
`runtime/orchestration/`, `runtime/execution/`, and the existing MT5 bridge
read paths. Data Channel is `127.0.0.1:22347`; Execution Channel is
`127.0.0.1:22348`. The API does not call write tools, shell commands, or a
Research Bridge.

Examples:

```sh
curl -fsS http://127.0.0.1:22349/api/v1/safety
curl -fsS 'http://127.0.0.1:22349/api/v1/signals?strategy_id=CONTEXT_STRUCTURE_RETRACE_V1'
curl -fsS http://127.0.0.1:22349/api/v1/executions/metrics
```

The intended future React `RealTradingApi` integration should use these GET
resources only. Mutating HTTP methods return `405 READ_ONLY_API`.
