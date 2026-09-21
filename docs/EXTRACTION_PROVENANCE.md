# Trading-platform extraction provenance

`trading-platform` was seeded from the bridge repository at commit
`5d4b018857794da8bcf1a9161876c1dc6fe31f73` using a filtered Git archive.

The selection came from `docs/extraction/data/file_classification.csv`:

- `TRADING_PLATFORM`, `LEGACY`, and `MIGRATION_TOOL` files were eligible.
- `MT5_BRIDGE` files were excluded.
- C3 platform contracts and protocol fixtures were included explicitly.
- Generated runtime state and large generated research outputs were excluded.

Stage 1 intentionally preserves historical paths and file bytes. Domain
namespacing and strategy re-freezing are deferred until a separately approved
stage.

The source bridge repository remains the owner of `mt5_bridge/`, `ea/`, bridge
transport/lifecycle code, and bridge-specific tests.
