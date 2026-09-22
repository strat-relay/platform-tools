from __future__ import annotations

import hashlib
import json
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
    """A syntactically valid JSON row that violates the canonical contract."""


class AppendOnlyTailer:
    def __init__(self, source: Path, checkpoint: Path, *, ingest: Callable[[dict[str, Any]], None] | None = None,
                 quarantine: Callable[[dict[str, Any]], None] | None = None):
        self.source, self.checkpoint, self.ingest, self.quarantine = source, checkpoint, ingest, quarantine

    def run_once(self) -> TailerResult:
        state = json.loads(self.checkpoint.read_text()) if self.checkpoint.exists() else {}
        offset = int(state.get("offset", 0)); fingerprint = state.get("sha256")
        data = self.source.read_bytes() if self.source.exists() else b""
        rotated = offset > len(data) or (fingerprint and hashlib.sha256(data[:offset]).hexdigest() != fingerprint)
        if rotated: offset = 0
        complete = data[offset:]
        last_newline = complete.rfind(b"\n")
        if last_newline < 0: return TailerResult([], [], rotated, offset)
        chunk, new_offset = complete[:last_newline + 1], offset + last_newline + 1
        records, malformed = [], []
        physical_line = data[:offset].count(b"\n") + 1
        cursor = offset
        for line_no, raw in enumerate(chunk.splitlines(), physical_line):
            source_offset = cursor
            cursor += len(raw) + 1
            try:
                record = json.loads(raw.decode("utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("signal JSONL row must be an object")
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                item = {"line": line_no, "source_offset": source_offset, "error": str(exc), "raw": raw.decode("utf-8", "replace"), "raw_sha256": hashlib.sha256(raw).hexdigest()}
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
                item = {"line": line_no, "source_offset": source_offset, "error": str(exc), "raw": raw.decode("utf-8", "replace"), "raw_sha256": hashlib.sha256(raw).hexdigest()}
                malformed.append(item)
                if self.quarantine: self.quarantine(item)
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint.write_text(json.dumps({"offset": new_offset, "sha256": hashlib.sha256(data[:new_offset]).hexdigest(), "source": str(self.source)}, sort_keys=True), encoding="utf-8")
        return TailerResult(records, malformed, rotated, new_offset)
