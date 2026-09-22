from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class TailerResult:
    records: list[dict[str, Any]]
    malformed: list[dict[str, Any]]
    rotated: bool
    offset: int


class QuarantinedRecordError(ValueError):
    """A source record is deterministically invalid for canonical ingestion."""

    def __init__(self, message: str, *, failure_class: str = "CANONICAL_REJECTION"):
        super().__init__(message)
        self.failure_class = failure_class


class AppendOnlyTailer:
    def __init__(self, source: Path, checkpoint: Path, *, ingest: Callable[[dict[str, Any]], None] | None = None,
                 quarantine: Callable[[dict[str, Any]], None] | None = None,
                 reject_rotation: bool = False):
        self.source, self.checkpoint, self.ingest, self.quarantine = source, checkpoint, ingest, quarantine
        self.reject_rotation = reject_rotation

    def run_once(self) -> TailerResult:
        state = json.loads(self.checkpoint.read_text()) if self.checkpoint.exists() else {}
        offset = int(state.get("offset", 0)); fingerprint = state.get("sha256")
        if self.source.exists():
            with self.source.open("rb") as source_file:
                source_stat = os.fstat(source_file.fileno())
                data = source_file.read()
            identity = {"source_path": str(self.source), "source_device": source_stat.st_dev,
                        "source_inode": source_stat.st_ino}
        else:
            data = b""
            identity = {"source_path": str(self.source), "source_device": None, "source_inode": None}
        identity_changed = (
            state.get("source_device") is not None and state.get("source_inode") is not None
            and (state.get("source_device"), state.get("source_inode"))
            != (identity["source_device"], identity["source_inode"])
        )
        rotated = identity_changed or offset > len(data) or (fingerprint and hashlib.sha256(data[:offset]).hexdigest() != fingerprint)
        if rotated and self.reject_rotation:
            raise RuntimeError("append-only source identity/prefix changed after cutoff; refusing pre-cutoff replay")
        if rotated: offset = 0
        complete = data[offset:]
        last_newline = complete.rfind(b"\n")
        if last_newline < 0: return TailerResult([], [], rotated, offset)
        chunk, new_offset = complete[:last_newline + 1], offset + last_newline + 1
        records, malformed = [], []
        physical_line = data[:offset].count(b"\n") + 1
        cursor = offset
        for line_no, physical_record in enumerate(chunk.splitlines(keepends=True), physical_line):
            source_offset = cursor
            cursor += len(physical_record)
            raw = physical_record.rstrip(b"\r\n")
            try:
                record = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError as exc:
                item = self._quarantine_item(identity, line_no, source_offset, raw, exc, "DECODE_ERROR")
                malformed.append(item)
                if self.quarantine: self.quarantine(item)
                continue
            except json.JSONDecodeError as exc:
                item = self._quarantine_item(identity, line_no, source_offset, raw, exc, "JSON_ERROR")
                malformed.append(item)
                if self.quarantine: self.quarantine(item)
                continue
            if not isinstance(record, dict):
                exc = QuarantinedRecordError("signal JSONL row must be an object", failure_class="CANONICAL_VALIDATION")
                item = self._quarantine_item(identity, line_no, source_offset, raw, exc, exc.failure_class)
                malformed.append(item)
                if self.quarantine: self.quarantine(item)
                continue
            record.setdefault("source_path", str(self.source))
            record.setdefault("source_line", line_no); record.setdefault("source_offset", source_offset); record.setdefault("canonical_hash", hashlib.sha256(raw).hexdigest())
            record["_source_rotation_replay"] = rotated
            try:
                if self.ingest: self.ingest(record)
                records.append(record)
            except QuarantinedRecordError as exc:
                item = self._quarantine_item(identity, line_no, source_offset, raw, exc, exc.failure_class)
                malformed.append(item)
                if self.quarantine: self.quarantine(item)
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = {"offset": new_offset, "sha256": hashlib.sha256(data[:new_offset]).hexdigest(), **identity}
        fd, temporary = tempfile.mkstemp(prefix=f".{self.checkpoint.name}.", dir=self.checkpoint.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(checkpoint, handle, sort_keys=True)
                handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, self.checkpoint)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return TailerResult(records, malformed, rotated, new_offset)

    @staticmethod
    def _quarantine_item(identity: dict[str, Any], line_no: int, source_offset: int, raw: bytes,
                         exc: Exception, failure_class: str) -> dict[str, Any]:
        return {**identity, "line": line_no, "source_offset": source_offset,
                "failure_class": failure_class, "error": str(exc),
                "raw": raw.decode("utf-8", "replace"),
                "raw_sha256": hashlib.sha256(raw).hexdigest()}
