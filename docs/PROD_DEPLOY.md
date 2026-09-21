# Production deploy: Trading Control API

```
browser ── console.stratrelay.app (static build, Cloudflare Access)
   │
   └─ fetch, credentials ─▶ api.stratrelay.app (Cloudflare Access)
                              │  Cloudflare Tunnel (cloudflared)
                              ▼
                       127.0.0.1:22349  python3 -m control_api   (this Mac)
```

The Control API has **no authentication of its own** and returns broker
account, position and exposure data. Access control is enforced only at the
Cloudflare edge. Never bind it to a non-loopback address, and never publish
`api.stratrelay.app` before the Access policy below exists.

## 1. API process

```sh
cp deploy/prod/control_api.env.example deploy/prod/control_api.env   # once
deploy/prod/run_control_api.sh
```

`CONTROL_API_CORS_ORIGINS` (comma-separated) is the browser allow-list; it
defaults to `http://localhost:5173`. Requests from other origins get a header
naming the first allowed origin, so the browser refuses them.

The process is not supervised. If it exits, the console shows "Failed to
fetch" until it is restarted.

## 2. Tunnel

Install `cloudflared`, then follow `deploy/prod/cloudflared-config.example.yml`.
It forwards only `api.stratrelay.app` to `127.0.0.1:22349`.

## 3. Cloudflare Access (create before the DNS route goes live)

- Self-hosted application covering `api.stratrelay.app` and
  `console.stratrelay.app`, with an allow policy for your identity only.
- On the API application, enable **Bypass OPTIONS requests to origin**.
  Browsers send CORS preflights without cookies, and Access would otherwise
  answer them with a login redirect.
- The Access session cookie is per hostname. After signing in to the console,
  open `https://api.stratrelay.app/api/v1/system` once in the same browser.

## 4. Console

`trading-ops-console/.env.production` sets real mode, the base URL
`https://api.stratrelay.app/api/v1` and `VITE_TRADING_API_CREDENTIALS=include`.
`npm run build` bakes these in; deploy `dist/` to `console.stratrelay.app`.

## Verify

```sh
curl -fsS -i http://127.0.0.1:22349/api/v1/system                 # local, no auth
curl -sS -o /dev/null -w '%{http_code}\n' https://api.stratrelay.app/api/v1/system   # must NOT be 200 without Access
```
