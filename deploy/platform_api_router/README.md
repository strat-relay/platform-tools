# Platform API Router

This internal-only router sends exactly `/api/v1/signals` and
`/api/v1/signals/*` to `platform-signals-api:22350`. All remaining `/api/v1`
paths, including the bare `/api/v1` path, go to the
`platform-control-api:22350` Service alias, which selects the same pod and
process as the signal API. Other paths retain the legacy `control-api:22349`
fallback. The router has no application-level trading logic.

CORS is applied once at this boundary for `https://console.stratrelay.app`,
including credentialed browser requests. The router does not reflect arbitrary
origins and hides upstream CORS headers to prevent duplicate/conflicting
headers. Preflight requests advertise GET, POST, and PATCH; each upstream API
still enforces whether a specific route accepts the method. API behavior is
otherwise unchanged.

The ClusterIP service listens on port `22351`. Network policy permits ingress
from the in-namespace `cloudflared` pod and egress only to the platform API,
legacy Control API, and cluster DNS. The existing `cloudflared-egress` policy
separately allows TCP 22351. No edge/tunnel origin configuration is changed
here.

Build and deliver the image through the host's private registry, then deploy
the immutable image digest in `workload.yaml`. The single-replica Deployment
uses an in-place rollout because namespace quota does not permit a surge pod;
there can be a brief API interruption during replacement.
