"""Append-only research replay row shape; never used by live runners."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path


@dataclass(frozen=True)
class ReplayLedgerRow:
    setup_id: str
    symbol: str
    direction: str
    strategy_family: str
    h4_source_timestamp: datetime | None
    h4_context: dict
    h1_source_timestamp: datetime | None
    h1_context: dict
    m15_setup_timestamp: datetime | None
    m15_setup: dict
    m5_trigger_timestamp: datetime | None
    m5_trigger_family: str | None
    entry_timestamp: datetime | None
    fill_timestamp: datetime | None
    fill_price: float | None
    spread_at_fill: dict | None
    stop: float | None
    target: float | None
    gross_r: float | None
    cost_r: float | None
    net_r: float | None
    exit_reason: str | None
    control_level: str
    continuation_or_reversal: str
    configuration_hash: str
    source_commit: str

    def to_json(self) -> str:
        def default(value):
            return value.isoformat() if isinstance(value, datetime) else value
        return json.dumps(asdict(self), default=default, sort_keys=True)


def append_row(path: str | Path, row: ReplayLedgerRow) -> None:
    """Explicit opt-in append-only writer for offline research replays."""
    target = Path(path)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(row.to_json() + "\n")
