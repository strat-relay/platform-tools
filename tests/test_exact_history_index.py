from research.exact_history_index import ExactHistoricalCandleIndex


def _bars():
    return [
        {"time": i * 300, "open": 1.0, "high": 10.0 + (i % 3), "low": 1.0 - (i % 2), "close": 5.0 + (i % 4)}
        for i in range(100)
    ]


def test_exact_reaction_and_role_queries_match_reference_predicates():
    bars = _bars()
    index = ExactHistoricalCandleIndex(bars, "M5")
    checks = 0
    for as_of in (1_500, 9_000, 29_700):
        for zone_low, zone_high in ((2.0, 8.0), (5.0, 10.0), (0.0, 100.0)):
            expected_reactions = [
                bar for bar in bars
                if bar["time"] + 300 <= as_of
                and bar["high"] >= zone_low
                and bar["low"] <= zone_high
            ]
            expected_role_flips = [
                bar for bar in bars
                if bar["time"] + 300 <= as_of
                and (bar["close"] < zone_low or bar["close"] > zone_high)
            ]
            assert index.intersecting(zone_low, zone_high, as_of) == expected_reactions
            assert index.outside_close(zone_low, zone_high, as_of) == expected_role_flips
            checks += 2
    assert checks == 18


def test_exact_queries_preserve_prefix_cutoff_and_start_boundary():
    bars = _bars()
    index = ExactHistoricalCandleIndex(bars, "M5")
    as_of = 9_000
    start = 1_500
    reactions = index.intersecting(2.0, 8.0, as_of, start_open=start)
    role_flips = index.outside_close(4.0, 6.0, as_of, start_open=start)
    assert reactions
    assert role_flips
    assert all(start <= bar["time"] and bar["time"] + 300 <= as_of for bar in reactions)
    assert all(start <= bar["time"] and bar["time"] + 300 <= as_of for bar in role_flips)
