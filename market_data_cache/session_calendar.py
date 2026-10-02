"""Provider-specific session closure classification for canonical bars.

The read bridge currently does not expose symbol trading-session metadata. Until it
does, the MT5 research feed uses this explicit calendar: weekends and the broker's
observed daily 23:00-23:45 UTC rollover window are non-tradable. This is an ingestion
rule, not strategy logic, and is scoped to provider MT5 rather than applied globally.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .store import TIMEFRAME_SECONDS


@dataclass(frozen=True)
class Gap:
    start: int
    end: int
    previous: int | None
    next: int | None

    @property
    def missing_bar_count(self) -> int:
        # ``end`` is exclusive and the caller supplies candle-aligned bounds.  The
        # calendar does not own timeframe parsing, so this is filled by the grouped
        # gap constructor below and remains a useful one-slot fallback here.
        return 1


@dataclass(frozen=True)
class SessionCalendar:
    provider: str
    maintenance_hours_utc: frozenset[int]

    def is_closed(self, timestamp: int) -> bool:
        dt = datetime.fromtimestamp(timestamp, timezone.utc)
        return dt.weekday() in (5, 6) or dt.hour in self.maintenance_hours_utc


# This is deliberately provider-scoped.  The current MT5 research bridge exposes
# no symbol/session metadata.  Known non-tradable windows for Exness MT5 forex:
#   21:00 UTC = 17:00 New York (EDT, UTC-4) — daily NY session close
#   22:00 UTC = 17:00 New York (EST, UTC-5) — daily NY session close after DST ends
#   23:00 UTC — observed broker daily maintenance/rollover window
# Crypto pairs (BTCUSDm etc.) trade 24/7 but share this calendar; gaps in crypto
# at these hours should be investigated rather than silently accepted.
MT5_RESEARCH_CALENDAR = SessionCalendar("MT5", frozenset({21, 22, 23}))


def is_expected_closure(provider: str, provider_symbol: str, timestamp: int) -> bool:
    """Return whether a candle slot is outside the MT5 research feed session."""
    if provider.upper() != "MT5":
        return False
    return MT5_RESEARCH_CALENDAR.is_closed(timestamp)


def classify_missing(rows: list[dict[str, Any]], timeframe: str, *, provider: str = "MT5",
                    provider_symbol: str = "") -> tuple[list[Gap], list[Gap]]:
    """Return (expected session closures, true/unknown gaps) without fabricating bars."""
    if len(rows) < 2:
        return [], []
    step = TIMEFRAME_SECONDS[timeframe]
    ordered = sorted(int(row["time"]) for row in rows)
    present = set(ordered)
    expected, true = [], []
    current: list[int] = []
    current_target: list[Gap] | None = None

    def flush() -> None:
        nonlocal current, current_target
        if not current or current_target is None:
            return
        current_target.append(Gap(current[0], current[-1] + step,
                                  max((value for value in ordered if value < current[0]), default=None),
                                  min((value for value in ordered if value > current[-1]), default=None)))
        current, current_target = [], None

    for timestamp in range(ordered[0] + step, ordered[-1], step):
        if timestamp in present:
            flush()
            continue
        target = expected if is_expected_closure(provider, provider_symbol, timestamp) else true
        if current_target is not target:
            flush()
            current_target = target
        current.append(timestamp)
    flush()
    return expected, true


def gap_payload(gap: Gap, timeframe: str) -> dict[str, Any]:
    step = TIMEFRAME_SECONDS[timeframe]
    return {"gap_start": gap.start, "gap_end": gap.end, "missing_bar_count": (gap.end - gap.start) // step,
            "previous_bar_timestamp": gap.previous, "next_bar_timestamp": gap.next,
            "gap_duration_seconds": gap.end - gap.start}
