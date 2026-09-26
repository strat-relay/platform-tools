from __future__ import annotations

from collections import defaultdict
from statistics import mean, median
from typing import Iterable

from .models import EntrySignal, EntrySignalOutcome


def _block(outcomes: list[EntrySignalOutcome]) -> dict:
    outcomes = [outcome for outcome in outcomes if outcome.status in {"TARGET_HIT", "STOPPED", "TIME_EXIT"}]
    rs = [outcome.realized_r for outcome in outcomes]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gross_loss = abs(sum(losses))
    durations = [outcome.exit_timestamp - int(outcome.provenance["entry_timestamp"]) for outcome in outcomes if outcome.provenance.get("entry_timestamp") is not None]
    return {"trade_count": len(rs), "win_count": len(wins), "loss_count": len(losses), "win_rate": len(wins) / len(rs) if rs else 0.0, "gross_r": sum(rs), "net_r": sum(rs), "expectancy_r": mean(rs) if rs else 0.0, "profit_factor": sum(wins) / gross_loss if gross_loss else None, "maximum_drawdown_r": drawdown, "average_r": mean(rs) if rs else 0.0, "median_r": median(rs) if rs else 0.0, "average_hold_duration": mean(durations) if durations else 0.0}


def calculate_metrics(signals: Iterable[EntrySignal], outcomes: Iterable[EntrySignalOutcome]) -> dict:
    signals = list(signals)
    outcomes = list(outcomes)
    result = {"candidate_setup_count": None, "signal_count": len(signals), **_block(outcomes)}
    for key, selector in (("instrument", lambda signal: signal.canonical_instrument), ("direction", lambda signal: signal.direction), ("timeframe", lambda signal: signal.provenance.get("timeframe", "UNKNOWN"))):
        grouped: dict[str, set[str]] = defaultdict(set)
        for signal in signals:
            grouped[selector(signal)].add(signal.signal_id)
        result[key] = {name: _block([outcome for outcome in outcomes if outcome.signal_id in ids]) for name, ids in grouped.items()}
    return result
