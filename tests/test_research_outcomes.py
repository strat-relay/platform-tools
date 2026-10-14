from strategy_backtest.research_outcomes import ResearchOutcomeEntry, resolve_research_entry


def entry(direction="LONG", entry=100.0, stop=99.0, target=102.0):
    return ResearchOutcomeEntry("episode", "STRATEGY@V1", "TEST", direction, 0,
                                entry, stop, target, "param", "deps", "data",
                                {"source": "test"})


def bar(time, open_, high, low, close):
    return {"time": time, "open": open_, "high": high, "low": low, "close": close}


def test_long_and_short_single_barrier_use_initial_r():
    long = resolve_research_entry(entry(), [bar(0, 100, 102, 99.5, 101)])
    short = resolve_research_entry(entry("SHORT", 100, 101, 98), [bar(0, 100, 100.5, 98, 99)])
    assert long.status == "TP" and long.realized_r == 2.0
    assert short.status == "TP" and short.realized_r == 2.0


def test_entry_candle_both_barriers_is_ambiguous():
    result = resolve_research_entry(entry(), [bar(0, 100, 103, 98, 100)])
    assert result.status == "AMBIGUOUS_INTRABAR"
    assert result.realized_r is None


def test_open_barrier_precedes_intrabar_and_marks_uncertain_valuation():
    result = resolve_research_entry(entry(), [bar(0, 98, 103, 97, 99)])
    assert result.status == "SL"
    assert result.exit_price == 98
    assert result.valuation_status == "UNCERTAIN_GAP_OPEN_REFERENCE"
    assert result.realized_r == -2.0


def test_expected_closure_and_missing_gap_are_not_bridged():
    bars = [bar(0, 100, 101, 99.5, 100), bar(1800, 100, 101, 99.5, 100)]
    expected = resolve_research_entry(entry(), bars, expected_gaps=[{"from": 0, "to": 1800, "missing_bars": 1}])
    missing = resolve_research_entry(entry(), bars)
    assert expected.status == "DATA_GAP_UNRESOLVED"
    assert expected.gap_classification == "EXPECTED_MARKET_CLOSURE_REOPENING"
    assert missing.status == "DATA_GAP_UNRESOLVED"
    assert missing.gap_classification == "MISSING_OBSERVATION_GAP"


def test_reopening_open_barrier_is_known_outcome_with_uncertain_price():
    result = resolve_research_entry(entry(), [bar(0, 100, 101, 99.5, 100), bar(1800, 98, 101, 97, 99)],
                                    expected_gaps=[{"from": 0, "to": 1800, "missing_bars": 1}])
    assert result.status == "SL"
    assert result.gap_classification == "EXPECTED_MARKET_CLOSURE_REOPENING"
    assert result.valuation_status == "UNCERTAIN_GAP_OPEN_REFERENCE"


def test_dataset_end_is_open_without_invented_time_exit_and_provenance_is_stable():
    result = resolve_research_entry(entry(), [bar(0, 100, 100.5, 99.5, 100)])
    assert result.status == "OPEN_AT_DATASET_END"
    assert result.realized_r is None
    assert result.provenance["parameter_hash"] == "param"
    assert result.to_dict() == resolve_research_entry(entry(), [bar(0, 100, 100.5, 99.5, 100)]).to_dict()
