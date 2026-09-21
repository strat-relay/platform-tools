"""Pure pagination semantics used by the read-only historical exporter."""
from __future__ import annotations

from typing import Iterable, Mapping

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}


def page_ranges(start_timestamp: int, end_timestamp: int, timeframe: str, page_size: int) -> list[tuple[int, int]]:
    """Return deterministic half-open request ranges.

    The endpoint convention is ``start <= candle_timestamp < end``.  Advancing
    the next request to the prior page's end creates no overlap and no gap;
    the exporter additionally deduplicates defensively.
    """
    if timeframe not in TF_SECONDS or not start_timestamp < end_timestamp or page_size <= 0:
        raise ValueError("invalid historical page request")
    span = TF_SECONDS[timeframe] * page_size
    out = []
    cur = int(start_timestamp)
    while cur < end_timestamp:
        nxt = min(int(end_timestamp), cur + span)
        out.append((cur, nxt)); cur = nxt
    return out


def normalize_page(rows: Iterable[Mapping], start_timestamp: int, end_timestamp: int, timeframe: str) -> list[dict]:
    """Validate, filter, sort, and deduplicate one endpoint response."""
    if timeframe not in TF_SECONDS:
        raise ValueError("unsupported timeframe")
    by_time = {}
    for row in rows:
        ts = int(row["time"])
        if start_timestamp <= ts < end_timestamp:
            by_time[ts] = dict(row)
    return [by_time[t] for t in sorted(by_time)]


def merge_pages(pages: Iterable[Iterable[Mapping]]) -> list[dict]:
    by_time = {}
    for page in pages:
        for row in page:
            by_time[int(row["time"])] = dict(row)
    return [by_time[t] for t in sorted(by_time)]
