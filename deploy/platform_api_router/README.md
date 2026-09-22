# Platform API Router

This internal-only compatibility router sends exactly `/api/v1/signals` and
`/api/v1/signals/*` to `platform-signals-api:22350`. Every other path is
proxied unchanged to `control-api:22349`. It has no application-level routing
or trading logic.

The ClusterIP service listens on port `22351`. Network policy permits ingress
from the in-namespace `cloudflared` pod and egress only to the two upstream
services and cluster DNS. No edge/tunnel origin configuration is changed here.

Build and deliver the image through the host's private registry, then deploy
the immutable image digest in `workload.yaml`.
