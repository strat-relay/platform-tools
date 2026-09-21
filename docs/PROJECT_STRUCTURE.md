# Project structure

## Current physical structure

The repository is currently a flat compatibility layout. Active scripts,
manifests, ledgers and state files live at the repository root. `strategies/`
and `research/` already provide a logical documentation/source grouping, while
historical outputs remain at root to preserve the paths expected by existing
scripts and runners.

## Target logical structure

```text
mt5-native-bridge/
├── README.md
├── bridge.py
├── MT5TradingBridge.mq5
├── context_structure_retrace/       # reusable causal feature library
├── strategies/                       # strategy specs and source ownership
├── research/                         # phase-specific development code/data
├── docs/                             # operations, architecture, ADRs, handoff
├── tests/                            # future normalized test taxonomy
├── scripts/                          # safe operator helpers
├── artifacts/                        # immutable test/review evidence
├── runtime/                          # future destination for runtime files
└── archive/                          # superseded research
```

## Deferred physical migration

The following remain at root until a maintenance window: V1 state, JSONL
ledger, heartbeat, PID, manifest, summary, V1 runner, bridge entrypoint and
all existing strategy-compatible root artifacts. PID 49978 has the repository
as its cwd and may have these paths open or expected by name. Moving them now
could interrupt collection or split recovery state.

The target `runtime/` tree is therefore logical documentation only today. A
future migration must: stop the runner cleanly, snapshot all state/ledger and
manifest files, update paths atomically, run the complete suite, verify hashes,
then restart once under maintenance supervision.

## Path policy

- Source and frozen manifests are source-controlled where appropriate.
- Prospective ledgers/state are backed up and retained; never truncate them.
- PIDs and heartbeats are volatile and should not be committed as source.
- Secrets and credentials never belong in this repository or backups.
- Superseded results remain in place or under `archive/` until explicitly
  archived with provenance.
