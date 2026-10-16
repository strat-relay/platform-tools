from datetime import timezone

import pytest

from platform_api.instrument_analytics import InstrumentAnalyticsRepository


def test_period_contract_supports_standard_and_custom_ranges():
    repo = InstrumentAnalyticsRepository()
    start, end, key = repo._period({"period": ["7D"]})
    assert key == "7D"
    assert start.tzinfo == timezone.utc and end.tzinfo == timezone.utc
    start, end, key = repo._period({"period": ["CUSTOM"], "from": ["2026-01-01"], "to": ["2026-02-01"]})
    assert key == "CUSTOM" and end > start


@pytest.mark.parametrize("value", ["BROKER_EXECUTED", "RETROSPECTIVE", "FORWARD_PAPER", "THEORETICAL", "ALL"])
def test_populations_are_explicit(value):
    repo = InstrumentAnalyticsRepository()
    assert repo._population({"provenance": [value]}) == value


def test_retroactive_signal_is_not_forward_paper_population():
    repo = InstrumentAnalyticsRepository()
    sql = repo._population_sql("FORWARD_PAPER")
    assert "s.signal_emitted_at > o.exit_timestamp" in sql
    assert "RETRO" in sql


def test_invalid_period_and_sort_fail_closed():
    repo = InstrumentAnalyticsRepository()
    with pytest.raises(ValueError):
        repo._period({"period": ["ALL"]})
    with pytest.raises(ValueError):
        repo._population({"provenance": ["MIXED"]})
