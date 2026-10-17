from types import SimpleNamespace

from research.price_action_forward_runtime import _causal_bars, _read_cached_symbol


def _bars(start: int, count: int, step: int):
    return tuple({"time": start + i * step, "open": 100, "high": 101,
                  "low": 99, "close": 100} for i in range(count))


def test_price_action_runtime_reads_validated_shared_cache_once():
    class Reader:
        def __init__(self):
            self.calls = []

        def get_snapshot(self, canonical, provider, history):
            self.calls.append((canonical, provider, history))
            h1 = _bars(1_000, 320, 3_600)
            m15 = _bars(1_000, 320, 900)
            return SimpleNamespace(
                symbol_info={"symbol": provider, "tick_size": 0.001},
                quote={"symbol": provider, "time": 2_000},
                source="REDIS_CANONICAL_CACHE",
                cache_age_seconds=4.0,
                continuity_status="CONTINUOUS",
                gap_status="NONE",
                recovery_status="NONE",
                bars={
                    "H1": SimpleNamespace(bars=h1, latest_completed_timestamp=h1[-1]["time"]),
                    "M15": SimpleNamespace(bars=m15, latest_completed_timestamp=m15[-1]["time"]),
                },
            )

    reader = Reader()
    result = _read_cached_symbol(reader, "USDJPY", "USDJPYm")

    assert reader.calls == [("USDJPY", "USDJPYm", {"H1": 320, "M15": 320})]
    assert len(result["H1"]) == 320
    assert len(result["M15"]) == 320
    assert result["source"] == "REDIS_CANONICAL_CACHE"
    assert result["gap_status"] == "NONE"


def test_runtime_source_has_no_direct_bridge_client():
    from pathlib import Path

    source = Path("research/price_action_forward_runtime.py").read_text()
    assert "Mt5ReadClient" not in source
    assert '"bridge_requests": 0' in source


def test_h1_waits_until_completed_m15_constituents_are_available():
    snapshot = {
        "H1": [{"time": 3_600}, {"time": 7_200}],
        "M15": [{"time": 6_300}, {"time": 7_200}],
    }
    h1, m15 = _causal_bars(snapshot)
    assert [row["time"] for row in h1] == [3_600]
    assert [row["time"] for row in m15] == [6_300, 7_200]
