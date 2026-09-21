"""Local append-only fan-out stream for Phase 7 read-only observations."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

STREAM_SCHEMA = "trade-manager-shared-observation-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def market_envelope(symbol: str, *, observed_at: str, source_timestamp: str | None,
                   bid: float | None, ask: float | None, spread: float | None,
                   m1: dict[str, Any] | None, m5: dict[str, Any] | None,
                   context: dict[str, Any] | None, provider: str = "PHASE7_READ_ONLY",
                   phase: str = "PHASE7", sequence: int = 0,
                   economic_position_id: str | None = None,
                   setup_id: str | None = None, strategy_id: str | None = None,
                   m5_history: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    mid = (float(bid) + float(ask)) / 2 if bid is not None and ask is not None else None
    payload = {"schema_version": STREAM_SCHEMA, "event_type": "MARKET_OBSERVATION",
               "observation_id": None, "observed_at": observed_at, "source_timestamp": source_timestamp,
               "published_at": _now(), "symbol": symbol, "economic_position_id": economic_position_id,
               "setup_id": setup_id, "strategy_id": strategy_id, "bid": bid, "ask": ask,
               "spread": spread, "mid": mid, "m1": m1, "m5": m5, "m5_history": m5_history or [],
               "context": context or {}, "source": {"provider": provider, "phase": phase, "sequence": sequence},
               "future_data_used": False}
    payload["observation_id"] = _id({k: payload[k] for k in ("symbol", "economic_position_id", "setup_id", "strategy_id", "source_timestamp", "bid", "ask", "m5", "source")})
    return payload


def position_event(event_type: str, position: dict[str, Any], *, observed_at: str,
                   source: str = "PHASE7_READ_ONLY", sequence: int = 0) -> dict[str, Any]:
    if event_type not in {"POSITION_OPENED", "POSITION_UPDATED", "POSITION_CLOSED"}:
        raise ValueError("invalid position lifecycle event")
    payload = {"schema_version": STREAM_SCHEMA, "event_type": event_type, "event_id": None,
               "observed_at": observed_at, "published_at": _now(),
               "economic_position_id": position["economic_position_id"], "setup_id": position.get("setup_id"),
               "strategy_id": position.get("strategy_id", "CONTEXT_STRUCTURE_RETRACE_V1"),
               "symbol": position.get("symbol"), "direction": position.get("direction"),
               "entry": position.get("entry", position.get("executable_entry", position.get("executable_paper_entry"))),
               "entry_timestamp": position.get("entry_timestamp", position.get("fill_timestamp_iso")),
               "original_stop": position.get("original_stop", position.get("initial_stop", position.get("stop"))),
               "current_stop": position.get("current_stop", position.get("initial_stop", position.get("stop"))),
               "target": position.get("target", position.get("v1_target")), "size": position.get("size"),
               "status": position.get("status", position.get("v1_status", "OPEN")),
               "source": {"provider": source, "phase": "PHASE7", "sequence": sequence},
               "future_data_used": False}
    payload["event_id"] = _id({k: payload[k] for k in payload if k not in {"event_id", "published_at"}})
    return payload


class SharedObservationPublisher:
    """Single publisher; publish failures are observable and fail open."""
    def __init__(self, root: str | Path = "runtime/trade_manager/observation_stream",
                 *, max_bytes: int = 50_000_000):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.events_path = self.root / "events.jsonl"
        self.state_path = self.root / "publisher_state.json"
        self.health_path = self.root / "publisher_health.json"
        self.max_bytes = max_bytes
        state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.sequence = int(state.get("sequence", 0))
        self.last_event_at = state.get("last_event_at")
        self.failures = int(state.get("failures", 0))
        self.active = False
        self.published_ids: set[str] = set(state.get("published_ids", []))
        if self.events_path.exists():
            for line in self.events_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    key = row.get("observation_id") or row.get("event_id")
                    if key:
                        self.published_ids.add(str(key))
                except json.JSONDecodeError:
                    continue

    def publish(self, event: dict[str, Any]) -> bool:
        try:
            event = dict(event)
            key = event.get("observation_id") or event.get("event_id")
            if not key:
                raise ValueError("event missing id")
            key = str(key)
            if key in self.published_ids:
                return True
            self.sequence += 1
            event.setdefault("source", {})["sequence"] = self.sequence
            event["published_at"] = _now()
            if self.events_path.exists() and self.events_path.stat().st_size > self.max_bytes:
                self.rotate()
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True, separators=(",", ":"), default=str) + "\n")
            self.published_ids.add(key)
            self.active = True
            self.last_event_at = event["published_at"]
            self._save_state()
            return True
        except Exception as exc:
            self.failures += 1
            try:
                self._save_state(error=str(exc))
            except Exception:
                # The observer must remain fail-open even if diagnostics storage
                # itself is unavailable. Phase 7's own state remains authoritative.
                pass
            return False

    def rotate(self) -> None:
        archive = self.root / f"events.{int(time.time())}.jsonl"
        os.replace(self.events_path, archive)

    def _save_state(self, error: str | None = None) -> None:
        self.state_path.write_text(json.dumps({"sequence": self.sequence, "last_event_at": self.last_event_at,
                                               "failures": self.failures, "last_error": error,
                                               "published_ids": sorted(self.published_ids)}, indent=2) + "\n")
        self.health_path.write_text(json.dumps({"publisher_running": self.active, "publisher_last_event_at": self.last_event_at,
                                                "publisher_sequence": self.sequence, "publisher_failures": self.failures,
                                                "broker_writes": 0}, indent=2) + "\n")


class FanoutConsumer:
    def __init__(self, name: str, root: str | Path = "runtime/trade_manager/observation_stream"):
        self.name = name
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.root / f"checkpoint.{name}.json"
        self.checkpoint = int(json.loads(self.checkpoint_path.read_text()).get("sequence", 0)) if self.checkpoint_path.exists() else 0
        self.gaps: list[dict[str, Any]] = []

    def read(self, *, persist: bool = True) -> list[dict[str, Any]]:
        rows = []
        if not (self.root / "events.jsonl").exists():
            return rows
        for line in (self.root / "events.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line); sequence = int(row.get("source", {}).get("sequence", 0))
            if sequence <= self.checkpoint:
                continue
            if sequence > self.checkpoint + 1:
                self.gaps.append({"from": self.checkpoint + 1, "to": sequence - 1, "incomplete": True})
            rows.append(row); self.checkpoint = max(self.checkpoint, sequence)
        if persist:
            self.checkpoint_path.write_text(json.dumps({"consumer": self.name, "sequence": self.checkpoint,
                                                        "gaps": self.gaps}, indent=2) + "\n")
        return rows

    def health(self) -> dict[str, Any]:
        publisher = json.loads((self.root / "publisher_health.json").read_text()) if (self.root / "publisher_health.json").exists() else {}
        last = publisher.get("publisher_last_event_at")
        lag_seconds = None
        if last:
            try:
                lag_seconds = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(str(last).replace("Z", "+00:00"))).total_seconds())
            except ValueError:
                pass
        return {**publisher, "consumer": self.name, "collector_checkpoint": self.checkpoint,
                "collector_lag_events": max(0, int(publisher.get("publisher_sequence", 0)) - self.checkpoint),
                "collector_lag_seconds": lag_seconds, "gaps": self.gaps, "broker_writes": 0}
