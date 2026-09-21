"""Append-only storage for causal observations, evidence events, and decisions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlStream:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._keys = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        row = json.loads(line)
                        if row.get("observation_id") or row.get("event_id") or row.get("idempotency_key"):
                            self._keys.add(row.get("observation_id") or row.get("event_id") or row.get("idempotency_key"))
                    except json.JSONDecodeError:
                        continue

    def append(self, record: dict[str, Any], key: str | None = None) -> bool:
        record_key = key or record.get("observation_id") or record.get("event_id") or record.get("idempotency_key")
        if record_key and record_key in self._keys:
            return False
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n")
        if record_key:
            self._keys.add(record_key)
        return True

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows


class ObservationStore:
    def __init__(self, root: str | Path = "trade_manager/observations"):
        root = Path(root)
        self.observations = JsonlStream(root / "observations.jsonl")
        self.events = JsonlStream(root / "events.jsonl")
        self.decisions = JsonlStream(root / "decisions.jsonl")
        self.experiments = JsonlStream(root / "experiment_results.jsonl")

    def last_observations(self) -> dict[str, dict[str, Any]]:
        latest = {}
        for row in self.observations.records():
            pid = row.get("economic_position_id")
            if pid:
                latest[pid] = row
        return latest

    def append_bundle(self, bundle: dict[str, Any]) -> dict[str, int]:
        observation = bundle["observation"]
        return {"observation": int(self.observations.append(observation)),
                "events": sum(int(self.events.append(event)) for event in bundle.get("events", [])),
                "decision": int(self.decisions.append(bundle["decision"]))}

    def append_experiment_results(self, rows: list[dict[str, Any]]) -> int:
        return sum(int(self.experiments.append(row, key=f"{row.get('economic_position_id')}|{row.get('management_experiment_id')}|{row.get('timestamp')}")) for row in rows)
