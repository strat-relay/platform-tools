# StratRelay Trading Platform

This repository is the platform-side source boundary extracted from
`mt5-native-bridge`. It owns strategies, orchestration, execution workflow,
Trade Manager, research, Control/Ops API, PostgreSQL tooling, and platform
contracts.

MT5 communication crosses the explicit `contracts/mt5_bridge/` client boundary.
This repository does not import `mt5_bridge` implementation modules.

## Extraction provenance

- Source repository: `/Users/caleb/mt5-native-bridge`
- Source commit: `5d4b018857794da8bcf1a9161876c1dc6fe31f73`
- Extraction method: filtered `git archive` seeded this repository; source
  paths and bytes were selected from the A1 classification manifest.
- Bridge-owned paths were excluded.
- Legacy strategy source bytes were not rewritten.

This is a source-boundary extraction, not a Strategy Studio, PostgreSQL
authority, NATS, or live-trading implementation.
