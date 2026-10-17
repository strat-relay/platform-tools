from __future__ import annotations

import json
from pathlib import Path

from research.price_action_forward_observer import Observer, audit_order_isolation, freeze, normalize_rows


def bar(ts: int, value: float = 100.0, *, complete: bool = True) -> dict:
    return {"time": ts, "open": value, "high": value + 1, "low": value - 1, "close": value + .25, "complete": complete, "tick_size": 0.01}


def snapshot(start: int, count: int, step: int) -> list[dict]:
    return [bar(start + i * step, 100 + i * .01) for i in range(count)]


def test_normalization_rejects_forming_duplicates_and_records_gaps():
    rows, rejects = normalize_rows([bar(0), bar(900), bar(1800, complete=False), bar(3600), bar(3600)], "M15")
    assert [r["time"] for r in rows] == [0, 900, 3600]
    assert {r["reason"] for r in rejects} == {"INCOMPLETE_CANDLE", "DUPLICATE", "EXPECTED_CLOSURE_GAP"}


def test_first_snapshot_is_warmup_only_and_restart_is_idempotent(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    freeze(t0="1970-01-01T00:00:00Z", output=manifest)
    state = tmp_path / "state"
    observer = Observer(state, manifest)
    h1 = snapshot(0, 10, 3600)
    m15 = snapshot(0, 40, 900)
    first = observer.ingest("EURUSD", "EURUSDm", h1, m15, observed_at="1970-01-01T01:00:00Z", snapshot_id="s1", source_kind="RECONSTRUCTED_FORWARD")
    assert first["first_snapshot_reconstructed"] is True
    assert first["emitted_evaluations"] == 0
    second = Observer(state, manifest).ingest("EURUSD", "EURUSDm", h1, m15, observed_at="1970-01-01T01:01:00Z", snapshot_id="s1", source_kind="LIVE_MARKET")
    assert second["new_events"] == 0
    assert not (state / "events.jsonl").exists()


def test_prefix_invariance_and_both_evaluators_are_present(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    freeze(t0="1970-01-01T00:00:00Z", output=manifest)
    state = tmp_path / "state"
    observer = Observer(state, manifest)
    h1 = snapshot(0, 20, 3600); m15 = snapshot(0, 80, 900)
    observer.ingest("EURUSD", "EURUSDm", h1, m15, observed_at="1970-01-01T01:00:00Z", snapshot_id="prefix", source_kind="RECONSTRUCTED_FORWARD")
    assert set(observer.state["evaluators"]) == {"sr_retest|EURUSD", "engulfing_continuation|EURUSD"}
    before = json.dumps(observer.state["evaluators"], sort_keys=True)
    Observer(state, manifest).ingest("EURUSD", "EURUSDm", h1, m15, observed_at="1970-01-01T01:00:01Z", snapshot_id="prefix", source_kind="LIVE_MARKET")
    assert json.dumps(json.loads((state / "state.json").read_text())["evaluators"], sort_keys=True) == before


def test_order_isolation_audit_is_fail_closed():
    result = audit_order_isolation()
    assert result["pass"] is True
    assert result["forbidden_bridge_tools"] == []
    assert result["production_publisher_imported"] is False
