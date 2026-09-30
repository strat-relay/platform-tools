from __future__ import annotations

from outcome_attribution import broker_authoritative, resolve_candle, resolve_intrabar, target_distance, validate_temporal_order


def bar(t, low, high):
    return {"time": t, "low": low, "high": high, "duration_seconds": 300}


def test_pre_entry_target_touch_is_not_a_target():
    result = resolve_candle("SHORT", bar(100, 90, 110), stop=108, target=95,
                           entry_time=220)
    assert result["status"] == "OPEN"
    assert result["reason"] == "PRE_ENTRY_CANDLE_EXCLUDED"


def test_m1_resolves_post_entry_stop():
    result = resolve_candle("SHORT", bar(100, 90, 110), stop=108, target=95,
                           entry_time=220,
                           finer_bars=[bar(220, 100, 109)])
    assert result["status"] == "STOPPED"


def test_unknown_same_candle_order_is_ambiguous():
    result = resolve_candle("SHORT", bar(300, 90, 110), stop=108, target=95,
                           entry_time=300, target_r=1.0)
    assert result == {"status": "AMBIGUOUS_INTRABAR", "realized_r": 0.0,
                      "exit_timestamp": 300, "reason": "UNKNOWN_ORDERING"}


def test_m1_target_before_stop():
    result = resolve_intrabar("SHORT", [bar(300, 94, 100), bar(600, 90, 110)],
                             stop=108, target=95, entry_time=300)
    assert result["status"] == "TARGET_HIT"


def test_m1_stop_before_target():
    result = resolve_intrabar("SHORT", [bar(300, 99, 109), bar(600, 90, 100)],
                             stop=108, target=95, entry_time=300)
    assert result["status"] == "STOPPED"


def test_broker_stop_overrides_theoretical_target():
    result = broker_authoritative(strategy_outcome="TARGET_HIT", strategy_realized_r=1.0,
                                  broker_outcome="STOPPED", broker_realized_r=-1.0,
                                  signal_emitted_at=100, broker_fill_time=110,
                                  broker_exit_time=120)
    assert result["status"] == "STOPPED"
    assert result["realized_r"] == -1.0
    assert result["strategy_outcome"] == "TARGET_HIT"


def test_exit_before_fill_is_invalid():
    assert validate_temporal_order(signal_emitted_at=100, broker_fill_time=110,
                                   broker_exit_time=109) == "INVALID_OUTCOME_TEMPORAL_ORDER"


def test_target_distance_is_unsigned_for_both_directions():
    assert target_distance(100, 110) == 10
    assert target_distance(110, 100) == 10
