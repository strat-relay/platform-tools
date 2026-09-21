from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


class CompatibilityProjector:
    """Idempotent compatibility-only file projection; never a source of truth."""

    def __init__(self, output: Path):
        self.output = output

    def project(self, event: Mapping[str, Any]) -> bool:
        event_id = str(event["event_id"])
        existing = self.output.read_text(encoding="utf-8").splitlines() if self.output.exists() else []
        if any(json.loads(line).get("event_id") == event_id for line in existing if line.strip()):
            return False
        record = dict(event); record["compatibility_only"] = True
        self.output.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.output.name}.", dir=self.output.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for line in existing: fh.write(line + "\n")
                fh.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"); fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp, self.output)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        return True
