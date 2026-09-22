# Platform API Router

This internal-only compatibility router sends exactly `/api/v1/signals` and
`/api/v1/signals/*` to `platform-signals-api:22350`. Every other path is
proxied unchanged to `control-api:22349`. It has no application-level routing
or trading logic.

CORS is applied once at this boundary for `https://console.stratrelay.app`,
including credentialed browser requests. The router does not reflect arbitrary
origins and hides upstream CORS headers to prevent duplicate/conflicting
headers. Preflight requests are answered here; API behavior is otherwise
unchanged.

The ClusterIP service listens on port `22351`. Network policy permits ingress
from the in-namespace `cloudflared` pod and egress only to the two upstream
services and cluster DNS. No edge/tunnel origin configuration is changed here.

Build and deliver the image through the host's private registry, then deploy
the immutable image digest in `workload.yaml`. The single-replica Deployment
uses an in-place rollout because namespace quota does not permit a surge pod;
there can be a brief API interruption during replacement.
