# Stage 1 bridge/client boundary

This stage establishes the platform-side MT5 protocol client without changing
the frozen bridge wire contract.

## Boundary

`contracts/mt5_bridge/` is the platform-facing client boundary. It owns HTTP,
JSON-RPC framing, compatibility headers, typed transport/tool errors, and the
version-1 canonical request serializer. It does not import strategies,
orchestration, Trade Manager, risk, ownership, or execution policy.

`paper_runner.call_bridge` now delegates through `Mt5ReadClient`. Existing
callers that still use their local adapters remain compatibility debt for the
next staged migration; no business logic was moved into the client.

The existing root `bridge.py` remains the operational bridge entrypoint and
compatibility surface in this commit. It was intentionally not physically
moved because concurrent dual-listener edits were dirty at the C2 baseline.
That move belongs in a clean follow-up commit and must preserve module-global
test seams.

## Safety

The execution safety audit now exposes its actual platform-policy scan set.
The MT5 client is deliberately outside that set, while a negative-control test
proves forbidden calls still fail the audit. No audit passes merely because a
source directory was moved.

`TRACKED_DOCKER_BUILD_READY=false`; no image or deployment was changed.
