from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class OrchestrationStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.paths = {name: root / f"{name}.jsonl" for name in ("events", "signals", "route_decisions", "sizing_decisions", "distribution_queue", "account_snapshots", "classification_corrections", "delivery_status")}
        self.state_path = root / "state.json"

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"schema": "orchestration-state-v1", "status": "STOPPED", "processed_signal_ids": [], "last_poll_at": None}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def append(self, stream: str, row: dict[str, Any], unique_key: str | None = None) -> bool:
        path = self.paths[stream]
        if unique_key and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and json.loads(line).get("unique_key") == unique_key:
                    return False
        out = dict(row)
        if unique_key:
            out["unique_key"] = unique_key
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(out, separators=(",", ":"), sort_keys=True, default=str) + "\n")
        return True

    def rows(self, stream: str) -> list[dict[str, Any]]:
        path = self.paths[stream]
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
