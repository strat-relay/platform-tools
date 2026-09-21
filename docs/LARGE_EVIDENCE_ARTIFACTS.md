# Large evidence artifacts outside normal Git history

Captured 2026-09-16. These files were inspected in place and were not moved,
rewritten, truncated, or deleted. They are intentionally excluded from the
GitHub source checkpoint because their sizes are unsuitable for ordinary Git
history.

| Artifact | Location | Size (bytes) | SHA-256 | Provenance |
|---|---|---:|---|---|
| Phase 2 snapshots | `context_structure_retrace_phase2_snapshots.jsonl` | 250,725,583 | `e2cb6fc815316c3289167c12a79d3fa84acc5ea6fc1b43f68e7a01a08f19f5f0` | Context-structure-retrace Phase 2 snapshot stream |
| Phase 3 snapshots | `research/context_structure_retrace_phase3/context_structure_retrace_phase3_snapshots.jsonl.gz` | 745,562,033 | `874fd980dcbe820bc04cf83ef4f248a9051a519e81e216c08e5b9940671eeced` | Context-structure-retrace Phase 3 compressed snapshot stream |
| Phase 3 ledger | `research/context_structure_retrace_phase3/context_structure_retrace_phase3_ledger.jsonl.gz` | 77,587,608 | `81a379d9a342e4494c7a2de0b31dffa9e009514a31e2fd503a8795a54a790b32` | Context-structure-retrace Phase 3 compressed ledger |

These are evidence inputs/outputs for the research phases, not source code.
The repository documents their locations and integrity values, but GitHub does
not contain the file contents. They are **not remotely backed up by GitHub**.

## Required separate backup

Create an immutable copy of each artifact in a separate backup system or
storage account, preferably in a second physical or administrative domain.
Record the destination, timestamp, byte count, and SHA-256; verify each copy
against the values above before treating it as backed up. Preserve the
original files until verification succeeds, and retain this inventory with the
backup manifest. Do not use a Git commit as the backup for these files.
