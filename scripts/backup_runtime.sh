#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${1:-$ROOT/../mt5-native-bridge-backup-$STAMP}"

if [ -e "$DEST" ]; then
  echo "Refusing to overwrite existing backup: $DEST" >&2
  exit 1
fi
mkdir -p "$DEST"

copy_if_present() {
  src="$ROOT/$1"
  if [ -e "$src" ]; then
    mkdir -p "$DEST/$(dirname "$1")"
    cp -pR "$src" "$DEST/$1"
  fi
}

copy_if_present context_structure_retrace_forward.py
copy_if_present context_structure_retrace
copy_if_present context_structure_retrace_forward_state.json
copy_if_present context_structure_retrace_forward.jsonl
copy_if_present context_structure_retrace_forward_manifest.json
copy_if_present context_structure_retrace_forward.heartbeat.json
copy_if_present context_structure_retrace_forward.pid
copy_if_present context_structure_retrace_forward_summary.md
copy_if_present docs
copy_if_present artifacts/test-results

(cd "$ROOT" && shasum -a 256 \
  context_structure_retrace_forward.py \
  context_structure_retrace_forward_state.json \
  context_structure_retrace_forward.jsonl \
  context_structure_retrace_forward_manifest.json \
  > "$DEST/SHA256SUMS.txt")
printf 'Read-only runtime backup created at %s\n' "$DEST"
