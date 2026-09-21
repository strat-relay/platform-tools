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


class AppendOnlyTailer:
    def __init__(self, source: Path, checkpoint: Path, *, ingest: Callable[[dict[str, Any]], None] | None = None):
        self.source, self.checkpoint, self.ingest = source, checkpoint, ingest

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
        for line_no, raw in enumerate(chunk.splitlines(), 1):
            try:
                record = json.loads(raw.decode("utf-8")); record.setdefault("source_path", str(self.source))
                record.setdefault("source_line", line_no); record.setdefault("canonical_hash", hashlib.sha256(raw).hexdigest())
                records.append(record)
                if self.ingest: self.ingest(record)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                malformed.append({"line": line_no, "error": str(exc), "raw": raw.decode("utf-8", "replace")})
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint.write_text(json.dumps({"offset": new_offset, "sha256": hashlib.sha256(data[:new_offset]).hexdigest(), "source": str(self.source)}, sort_keys=True), encoding="utf-8")
        return TailerResult(records, malformed, rotated, new_offset)
