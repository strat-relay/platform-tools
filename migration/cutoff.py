from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _snapshot(source: Path) -> tuple[dict[str, Any], bytes]:
    with source.open("rb") as handle:
        before = os.fstat(handle.fileno())
        data = handle.read()
        after = os.fstat(handle.fileno())
    current = source.stat()
    if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or (
        after.st_dev, after.st_ino, after.st_size
    ) != (current.st_dev, current.st_ino, current.st_size):
        raise RuntimeError("source changed while establishing cutoff")
    if data and not data.endswith(b"\n"):
        raise RuntimeError("source EOF is inside an incomplete record; refusing an unsafe cutoff")
    cursor = len(data)
    last = None
    for line in reversed(data[:cursor].splitlines()):
        if line.strip():
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict) and candidate.get("signal_id"):
                    last = {"signal_id": candidate.get("signal_id"),
                            "signal_timestamp": candidate.get("signal_emitted_at") or candidate.get("signal_timestamp") or candidate.get("created_at")}
                    break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    return ({"path": str(source), "logical_identity": str(source.resolve()),
             "device": before.st_dev, "inode": before.st_ino,
             "file_size_bytes": before.st_size, "byte_offset": cursor,
             "prefix_sha256": hashlib.sha256(data).hexdigest(),
             "last_complete_pre_cutoff_signal": last}, data)


def establish_cutoff(source: Path, marker_path: Path, checkpoint_path: Path, *,
                     provenance: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create a durable EOF cutoff once, or resume from its saved boundary.

    Existing markers are authoritative. EOF is never sampled again on restart;
    if a crash happened after marker fsync but before checkpoint creation, the
    checkpoint is reconstructed from the immutable marker, not current EOF.
    """
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("cutoff_id") is None or marker.get("source_file", {}).get("byte_offset") is None:
            raise RuntimeError("existing evidence marker is not a forward-only cutoff")
        if not checkpoint_path.exists():
            file_info = marker["source_file"]
            _atomic_json(checkpoint_path, {"offset": file_info["byte_offset"],
                "sha256": file_info["prefix_sha256"], "source_path": file_info["path"],
                "source_device": file_info["device"], "source_inode": file_info["inode"]})
        return marker, False

    if checkpoint_path.exists():
        raise RuntimeError("orphan checkpoint exists without an immutable cutoff marker; refusing to resample EOF")

    file_info, _ = _snapshot(source)
    marker = {"cutoff_id": str(uuid.uuid4()), "cutoff_utc": _utc_now(),
              "source_file": file_info, "source_start_cursor": file_info["byte_offset"],
              "provenance": dict(provenance),
              "bootstrap_records_count_as_live": False,
              "pre_cutoff_records_ingested": 0}
    # O_EXCL prevents two first-starting workers from assigning different T0s.
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another first-starting worker won the atomic marker creation race.
        return establish_cutoff(source, marker_path, checkpoint_path, provenance=provenance)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(marker, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(marker_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    _atomic_json(checkpoint_path, {"offset": file_info["byte_offset"],
        "sha256": file_info["prefix_sha256"], "source_path": file_info["path"],
        "source_device": file_info["device"], "source_inode": file_info["inode"]})
    return marker, True
