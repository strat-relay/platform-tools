"""Create an immutable, local evidence bundle for the next Context parity window.

This is deliberately a file-to-file capture utility. It does not contact Kubernetes,
Redis, PostgreSQL, a broker, or any production service. Operators first export the exact
bounded artifacts, then run this command against those files before replay begins.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "context-v1-parity-capture-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_commit() -> str | None:
    try:
        return subprocess.check_output(("git", "rev-parse", "HEAD"), text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def capture(output_dir: Path, artifacts: Iterable[tuple[str, Path]], *, capture_id: str | None = None,
            source_commit: str | None = None) -> dict:
    """Copy named evidence files into a new bundle and write a hashed manifest.

    The destination must not exist. This prevents an old snapshot from being silently
    replaced and keeps the manifest suitable for later parity evidence.
    """
    if output_dir.exists():
        raise FileExistsError(f"capture destination already exists: {output_dir}")
    requested = list(artifacts)
    if not requested:
        raise ValueError("at least one evidence artifact is required")
    for name, source in requested:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"artifact name must be a single filename: {name!r}")
        if not source.is_file():
            raise FileNotFoundError(source)

    output_dir.mkdir(parents=True)
    entries = []
    try:
        for name, source in requested:
            destination = output_dir / name
            shutil.copyfile(source, destination)
            entries.append({"name": name, "source": str(source.resolve()),
                            "bytes": destination.stat().st_size, "sha256": _sha256(destination)})
        manifest = {"schema": SCHEMA_VERSION,
                    "capture_id": capture_id or output_dir.name,
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "source_commit": source_commit or _source_commit(),
                    "artifacts": entries,
                    "production_writes": False,
                    "replay_contract": {"completed_bars_only": True,
                                        "bounded_interval_required": True,
                                        "live_state_must_be_immutable": True}}
        manifest["capture_hash"] = hashlib.sha256(
            json.dumps({key: value for key, value in manifest.items() if key != "captured_at"},
                       sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        (output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                                   encoding="utf-8")
        return manifest
    except Exception:
        shutil.rmtree(output_dir)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--market-data", type=Path)
    parser.add_argument("--publication-ledger", type=Path)
    parser.add_argument("--execution-ledger", type=Path)
    parser.add_argument("--deployment", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    candidates = (("state.json", args.state), ("market_data.jsonl", args.market_data),
                  ("publication_ledger.jsonl", args.publication_ledger),
                  ("execution_ledger.jsonl", args.execution_ledger),
                  ("deployment.json", args.deployment), ("schema.json", args.schema))
    artifacts = [(name, path) for name, path in candidates if path is not None]
    print(json.dumps(capture(args.output, artifacts, source_commit=args.source_commit), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
