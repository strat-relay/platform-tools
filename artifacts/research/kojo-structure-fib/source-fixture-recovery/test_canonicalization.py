"""Determinism checks for the KOJO_STRUCTURE_FIB source fixture."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).parent


def canonical_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


def test_canonical_serialization_is_stable() -> None:
    raw = (ROOT / "canonical_bars.v1.json").read_bytes()
    value = json.loads(raw)
    assert canonical_bytes(value) == raw
    assert hashlib.sha256(raw).hexdigest() == json.loads(
        (ROOT / "provenance.json").read_text()
    )["canonical_fixture_sha256"]


def test_bars_are_strictly_ascending_and_ohlc_only() -> None:
    value = json.loads((ROOT / "canonical_bars.v1.json").read_text())
    bars = value["bars"]
    timestamps = [bar["timestamp"] for bar in bars]
    assert timestamps == sorted(timestamps)
    assert len(timestamps) == len(set(timestamps))
    assert all(list(bar) == ["timestamp", "open", "high", "low", "close"] for bar in bars)

