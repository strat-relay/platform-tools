from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ExecutionStore:
    STREAMS = ("events", "execution_intents", "execution_decisions", "account_snapshots", "market_snapshots", "execution_skips")

    def __init__(self, root: Path):
        self.root = root; self.root.mkdir(parents=True, exist_ok=True)
        self.paths = {name: root / f"{name}.jsonl" for name in self.STREAMS}
        self.paths["state"] = root / "state.json"

    def rows(self, stream: str) -> list[dict[str, Any]]:
        path = self.paths[stream]
        if not path.exists(): return []
        return [json.loads(x) for x in path.read_text().splitlines() if x]

    def append(self, stream: str, row: dict[str, Any], unique_key: str | None = None) -> bool:
        if unique_key and any(r.get("unique_key") == unique_key for r in self.rows(stream)):
            return False
        value = dict(row)
        if unique_key: value["unique_key"] = unique_key
        with self.paths[stream].open("a") as fh:
            fh.write(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        return True

    def state(self) -> dict[str, Any]:
        if not self.paths["state"].exists(): return {"schema": "execution-state-v1", "status": "STOPPED"}
        return json.loads(self.paths["state"].read_text())

    def save_state(self, state: dict[str, Any]):
        tmp = self.paths["state"].with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.paths["state"])
