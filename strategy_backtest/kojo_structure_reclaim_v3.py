"""Kojo Structure Reclaim V3 — targeted repair of V2 semantic defects.

SOURCE_FIDELITY_BLOCKED = false  (all blockers resolved)

V3 STATUS FLAGS:
  SOURCE_FIDELITY_BLOCKED  = false
  READY_FOR_DISCOVERY      = true
  READY_FOR_VALIDATION     = false
  READY_FOR_SHADOW_SIGNALS = false
  READY_FOR_EXECUTION      = false

IMPLEMENTED IN V3:

  REPAIR 1 — OPPORTUNITY RETIREMENT POLICY

    V2 defect: _consumed_level_keys grew on ALL terminal states (CONSUMED,
    INVALIDATED, EXPIRED), permanently blocking new episodes on structural levels
    whose only prior episode was a failed retest (INVALIDATED) or timeout (EXPIRED).
    Source evidence: "If an original entry is missed, a NEW simple pullback can create
    a later valid opportunity."

    V3 correction: explicit separation of two identities:

      _consumed_opportunity_keys — grows ONLY on CONSUMED.
        Permanently retires the economic opportunity. Same structural level + direction
        can never re-fire once a signal was produced and consumed.

      _terminal_episode_ids — grows on ALL terminal states.
        Prevents a specific terminated episode from reactivating. Does NOT prevent a
        new, structurally distinct episode on the same level.

    New-causal-break guard: a new episode on a structural level is only created when
    the current H1 bar is the FIRST bar to close on the break side of the level (i.e.
    the previous H1 bar's close was on the non-break side). Prevents stale continuation
    bars from creating duplicates after INVALIDATED/EXPIRED without a time cooldown.

  REPAIR 2 — H1 CONFIRMATION SOURCE RULE  (H1_CLOSE_BEYOND_LEVEL_REQUIRED)

    V2 defect: h1_confirmation_evidence was permanently set to the break bar at setup
    creation time and never updated. The DUAL_TF_BASELINE_ENFORCED=true assertion was
    semantically mislabeled — it counted H1 structural context as H1 confirmation.

    Source evidence:
      H1_CLOSE → M15_RETEST → M15_REJECTION → ENTRY temporal ordering required.
      H1 wick-only penetration is NOT confirmation.
      An unfinished H1 candle is NOT confirmation.
      An M15 rejection that occurs before the qualifying H1 close is NOT valid.
      M15-only entry is a rare/aggressive exception; NOT included in V3 baseline.

    V3 resolution:
      The qualifying H1 bar = the completed H1 bar that closes BEYOND the structural
      level (i.e. the break bar itself).  This IS the H1 confirmation; no separate
      post-pullback H1 event is required by source.

      Provenance carries explicit H1 confirmation fields:
        h1_key_level_id                   — the structural level ID broken
        h1_confirmation_open_ts           — break bar open timestamp
        h1_confirmation_close_ts          — break bar close timestamp (= episode_start_ts)
        h1_confirmation_open/high/low/close
        h1_confirmation_relation_to_level — "CLOSE_BEYOND" (LONG) / "CLOSE_BELOW" (SHORT)

      Temporal guard: M15 bars with open_timestamp < episode_start_ts are skipped in
      WAITING_FOR_RETEST, enforcing the H1_CLOSE → M15_RETEST ordering at runtime.

      DUAL_TF_BASELINE_ENFORCED assertion removed from V3 diagnostics.
      M15_ONLY_BASELINE_ENABLED = false.

IMPLEMENTED IN V3 (continued):

  REPAIR 3 — TP1 CURRENT-DAY M15 REACTION ZONE  (SOURCE_EXPLICIT)

    Source evidence:
      TP1 is a meaningful CURRENT-DAY M15 reaction zone supported by wick and/or
      body-close rejection evidence, with planned reward >= 1.0R.
      TP1_MINIMUM_PLANNED_R = 1.0 is SOURCE_EXPLICIT; not a parameter.

    V3 implementation:
      _detect_m15_reaction_events: identify WICK_REJECTION and BODY_CLOSE_REJECTION
        from current UTC-day completed M15 bars (strictly causal).
      _cluster_reaction_zones: group nearby reaction events into zones using
        zone_tolerance = retest_tolerance_atr * m15_atr (existing KOJO parameter).
      Qualifying zones: zone in profit direction, planned_r >= TP1_MINIMUM_PLANNED_R.
      Ranking: (1) total_distinct_reactions desc, (2) last_reaction_ts desc,
               (3) distance_from_entry asc.
      If no qualifying zone: NO_QUALIFYING_TP1_REACTION_ZONE (no trade).

    CURRENT_DAY_M15_REACTION_RULE_IMPLEMENTED = true
    WICK_REACTION_SUPPORTED = true
    BODY_CLOSE_REJECTION_SUPPORTED = true
    TP1_MINIMUM_R_SOURCE_EXPLICIT = true
    TP1_MINIMUM_R = 1.0

  REPAIR 4 — TP2 LIQUIDITY OBJECTIVE

    Source evidence: TP2 represents liquidity (prior swing, equal highs/lows,
    previous-day extreme, session extreme, untouched external extreme).

    V3 implementation detects these liquidity types:
      SWING_HIGH / SWING_LOW — confirmed H1 swing pivots (from _confirmed_swings)
      EQUAL_HIGHS / EQUAL_LOWS — cluster of 2+ H1 pivots at same price
      PREV_DAY_HIGH / PREV_DAY_LOW — previous UTC-day extreme from H1 bars
      SESSION_HIGH / SESSION_LOW — last completed standard Forex session extreme
        (ASIAN: 22:00-08:00 UTC, LONDON: 07:00-16:00 UTC, NY: 13:00-22:00 UTC)
      UNTOUCHED_EXTERNAL_EXTREME — farthest confirmed H1 extreme in profit direction
        that has not been exceeded since episode_start_ts

    TP2 = nearest valid external liquidity objective beyond TP1.
    TP2_SELECTION_POLICY = NEAREST_VALID_EXTERNAL_LIQUIDITY
    TP2_SELECTION_POLICY_SOURCE_STATUS = IMPLEMENTATION_HYPOTHESIS
    All other qualifying liquidity objectives preserved in provenance.

  SCAFFOLD 4 — INITIAL TRADE PLAN (separate from trade management)

    V3 signals carry an initial_trade_plan block in provenance:
      targets_are_objectives = true — targets are the plan's objectives, not hard exits.
      mandatory_hold_to_target = false — exits at discretion/confirmation are valid.
      trade_management_policy_ref = SOURCE_RULE_REQUIRED — thresholds/transitions not yet
        defined, though possible management reasons are now source-supported:
        expected continuation fails; key level reclaimed; opposite M15 rejection;
        opposite structure; H1/M15 alignment deterioration; other thesis degradation.

PRESERVED FROM V2 (unchanged):
  - All causal invariants (no-lookahead, prefix-invariance, deterministic rerun)
  - H1 break detection algorithm
  - M15 retest detection + pullback envelope tracking
  - M15 strong pattern requirement (ENGULFING | REJECTION_WICK)
  - External-only target selection (must predate episode, outside envelope)
  - Stop computation (M15 retest zone + buffer)
  - TP2 selection logic
  - V2 artifacts and fingerprints unchanged

BROKER_WRITES = 0.
VALIDATION_OUTCOMES_ACCESSED = false.
PARAMETER_SEARCH = false.
PRODUCTION_ELIGIBLE = false.
"""
from __future__ import annotations

from typing import Any

from .models import (
    EntrySignal,
    MarketEvent,
    ParameterSchema,
    ParameterSet,
    SetupLifecycleEvent,
    StrategyVersion,
    fingerprint,
)

# ─── identity ──────────────────────────────────────────────────────────────────

STRATEGY_ID = "KOJO_STRUCTURE_RECLAIM_V3"
VERSION = "V3"
EVALUATOR_KEY = "kojo_structure_reclaim_v3"
INSTRUMENT = "XAUUSD"
TIMEFRAME_H1 = "H1"
TIMEFRAME_M15 = "M15"
H1_SECONDS = 3600
M15_SECONDS = 900

# ─── source fidelity status ────────────────────────────────────────────────────

SOURCE_FIDELITY_BLOCKED = False
READY_FOR_DISCOVERY = True
READY_FOR_VALIDATION = False
READY_FOR_SHADOW_SIGNALS = False
READY_FOR_EXECUTION = False

SOURCE_FIDELITY_BLOCKED_REASONS: tuple[str, ...] = ()
# All V3 blockers resolved:
#   H1_POST_PULLBACK_CONFIRMATION — resolved: break bar close IS H1 confirmation
#   TARGET_MEANINGFULNESS_BOUNDARY — resolved: TP1 requires current-day M15 reaction zone + 1.0R

# ─── setup states ──────────────────────────────────────────────────────────────

SETUP_DETECTED = "SETUP_DETECTED"
WAITING_FOR_RETEST = "WAITING_FOR_RETEST"
RETEST_SEEN = "RETEST_SEEN"
CONFIRMED = "CONFIRMED"
CONFIRMED_PENDING_NEXT_OPEN = "CONFIRMED_PENDING_NEXT_OPEN"
CONSUMED = "CONSUMED"
EXPIRED = "EXPIRED"
INVALIDATED = "INVALIDATED"

TERMINAL_STATES = {CONSUMED, EXPIRED, INVALIDATED}

# ─── H1 confirmation invariants (source-confirmed) ────────────────────────────

H1_CLOSE_BEYOND_LEVEL_REQUIRED = True
H1_CONFIRMATION_IS_COMPLETED_BAR = True
H1_CONFIRMATION_PRECEDES_M15_RETEST = True
M15_ONLY_BASELINE_ENABLED = False

# ─── target semantics ──────────────────────────────────────────────────────────

TARGET_SELECTION_SOURCE_RULE_REQUIRED = True
# No reaction-zone or liquidity primitive found in strategy_backtest/ scope.
# Candidates are classified as GENERIC_EXTERNAL_PIVOT only.
EXISTING_REACTION_ZONE_PRIMITIVE_FOUND = False
EXISTING_LIQUIDITY_PRIMITIVE_FOUND = False
TP_CANDIDATE_CLASS_GENERIC = "GENERIC_EXTERNAL_PIVOT"

# ─── near-coincident classification ───────────────────────────────────────────

TP1_NEAR_COINCIDENT = "EXTERNAL_BUT_NEAR_COINCIDENT_WITH_ENTRY"
TP1_STANDARD = "EXTERNAL_STANDARD"
TP1_NEAR_COINCIDENT_BOUNDARY = "SOURCE_RULE_REQUIRED"

# ─── trade plan semantics ──────────────────────────────────────────────────────

TARGETS_ARE_OBJECTIVES = True
MANDATORY_HOLD_TO_TARGET = False

# ─── TP1 reaction zone (source-explicit) ──────────────────────────────────────

TP1_MINIMUM_PLANNED_R = 1.0   # SOURCE_EXPLICIT — not a search parameter
CURRENT_DAY_M15_REACTION_RULE_IMPLEMENTED = True
WICK_REACTION_SUPPORTED = True
BODY_CLOSE_REJECTION_SUPPORTED = True
TP1_MINIMUM_R_SOURCE_EXPLICIT = True

# Hard-coded implementation thresholds (NOT parameters; not searched):
# Wick must occupy >= 33% of total bar range to qualify as a wick rejection.
WICK_REACTION_MIN_FRACTION = 0.33
# Candle body must occupy >= 25% of total bar range to qualify as body-close rejection.
BODY_CLOSE_MIN_FRACTION = 0.25

# ─── TP2 liquidity types ──────────────────────────────────────────────────────

LIQUIDITY_SWING_HIGH = "SWING_HIGH"
LIQUIDITY_SWING_LOW = "SWING_LOW"
LIQUIDITY_EQUAL_HIGHS = "EQUAL_HIGHS"
LIQUIDITY_EQUAL_LOWS = "EQUAL_LOWS"
LIQUIDITY_PREV_DAY_HIGH = "PREV_DAY_HIGH"
LIQUIDITY_PREV_DAY_LOW = "PREV_DAY_LOW"
LIQUIDITY_SESSION_HIGH = "SESSION_HIGH"
LIQUIDITY_SESSION_LOW = "SESSION_LOW"
LIQUIDITY_UNTOUCHED_EXTREME = "UNTOUCHED_EXTERNAL_EXTREME"

TP2_SELECTION_POLICY = "NEAREST_VALID_EXTERNAL_LIQUIDITY"
TP2_SELECTION_POLICY_SOURCE_STATUS = "IMPLEMENTATION_HYPOTHESIS"

# Standard Forex session boundaries (UTC hours) — not parameters.
# ASIAN crosses midnight: 22:00 prev day → 08:00 current day
_SESSION_DEFS = [
    ("ASIAN",  22, 8),    # 22:00 prev day to 08:00
    ("LONDON", 7,  16),   # 07:00 to 16:00
    ("NY",     13, 22),   # 13:00 to 22:00
]

# ─── helpers (identical to V2 — inlined) ──────────────────────────────────────

def _bar(event: MarketEvent) -> dict[str, Any]:
    return {
        "time": event.open_timestamp,
        "open": event.open,
        "high": event.high,
        "low": event.low,
        "close": event.close,
    }


def _event_evidence(event: MarketEvent) -> dict[str, Any]:
    return {
        "canonical_instrument": event.canonical_instrument,
        "timeframe": event.timeframe,
        "open_timestamp": event.open_timestamp,
        "close_timestamp": event.close_timestamp,
        "open": event.open,
        "high": event.high,
        "low": event.low,
        "close": event.close,
        "completed": event.completed,
    }


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1 - alpha) * out[-1])
    return out


def _atr(bars: list[dict[str, Any]], period: int = 14) -> float:
    if len(bars) < 2:
        return float(bars[0]["high"]) - float(bars[0]["low"]) if bars else 0.0
    trs: list[float] = []
    previous: float | None = None
    for b in bars:
        high = float(b["high"])
        low = float(b["low"])
        close = float(b["close"])
        if previous is None:
            trs.append(high - low)
        else:
            trs.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = close
    rolling = 0.0
    n = min(period, len(trs))
    for v in trs[-n:]:
        rolling += v
    return rolling / n if n else 0.0


def _ema_snapshot(h1_bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(b["close"]) for b in h1_bars]
    result: dict[str, Any] = {}
    for period in (20, 50, 100, 200):
        series = _ema(closes, period)
        last = series[-1] if series else None
        result[str(period)] = last
    if len(closes) >= 2:
        result["price_above_ema20"] = closes[-1] > result["20"] if result.get("20") is not None else None
        result["price_above_ema50"] = closes[-1] > result["50"] if result.get("50") is not None else None
        result["ema20_above_ema50"] = (
            result["20"] > result["50"]
            if result.get("20") is not None and result.get("50") is not None
            else None
        )
    return result


def _confirmed_swings(
    bars: list[dict[str, Any]], as_of_close_ts: int, lookback: int
) -> list[dict[str, Any]]:
    """Causal confirmed swing pivots.  Identical algorithm to V1/V2."""
    available = [b for b in bars if b["time"] + H1_SECONDS <= as_of_close_ts]
    out: list[dict[str, Any]] = []
    for i in range(lookback, len(available) - lookback):
        confirmation_close_ts = available[i + lookback]["time"] + H1_SECONDS
        if confirmation_close_ts > as_of_close_ts:
            continue
        window = available[i - lookback: i + lookback + 1]
        hi = float(available[i]["high"])
        lo = float(available[i]["low"])
        level_ts = available[i]["time"]
        if hi >= max(float(b["high"]) for b in window):
            out.append({
                "type": "RESISTANCE",
                "price": hi,
                "h1_open_timestamp": level_ts,
                "confirmed_at_close_ts": confirmation_close_ts,
                "level_id": f"R-{level_ts}-{hi:.5f}",
            })
        if lo <= min(float(b["low"]) for b in window):
            out.append({
                "type": "SUPPORT",
                "price": lo,
                "h1_open_timestamp": level_ts,
                "confirmed_at_close_ts": confirmation_close_ts,
                "level_id": f"S-{level_ts}-{lo:.5f}",
            })
    return out


def _bullish_engulfing(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    prev_o, prev_c = float(prev["open"]), float(prev["close"])
    curr_o, curr_c = float(curr["open"]), float(curr["close"])
    return prev_c < prev_o and curr_c > curr_o and curr_o <= prev_c and curr_c >= prev_o


def _bearish_engulfing(prev: dict[str, Any], curr: dict[str, Any]) -> bool:
    prev_o, prev_c = float(prev["open"]), float(prev["close"])
    curr_o, curr_c = float(curr["open"]), float(curr["close"])
    return prev_c > prev_o and curr_c < curr_o and curr_o >= prev_c and curr_c <= prev_o


def _rejection_wick_bullish(curr: dict[str, Any]) -> bool:
    o, h, lo, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
    body = abs(c - o)
    full_range = h - lo
    if full_range < 1e-9:
        return False
    lower_wick = min(o, c) - lo
    upper_wick = h - max(o, c)
    return (lower_wick >= 2 * body) and (lower_wick >= upper_wick * 2) and (c > o)


def _rejection_wick_bearish(curr: dict[str, Any]) -> bool:
    o, h, lo, c = float(curr["open"]), float(curr["high"]), float(curr["low"]), float(curr["close"])
    body = abs(c - o)
    full_range = h - lo
    if full_range < 1e-9:
        return False
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - lo
    return (upper_wick >= 2 * body) and (upper_wick >= lower_wick * 2) and (c < o)


def _m15_strong_confirmation_type(
    m15_bars: list[dict[str, Any]], index: int, direction: str
) -> str | None:
    """V2/V3: only ENGULFING and REJECTION_WICK count.  CONTINUATION_CLOSE is not accepted."""
    if index < 1 or index >= len(m15_bars):
        return None
    curr = m15_bars[index]
    prev = m15_bars[index - 1]
    if direction == "LONG":
        if _bullish_engulfing(prev, curr):
            return "BULLISH_ENGULFING"
        if _rejection_wick_bullish(curr):
            return "REJECTION_WICK"
    else:
        if _bearish_engulfing(prev, curr):
            return "BEARISH_ENGULFING"
        if _rejection_wick_bearish(curr):
            return "REJECTION_WICK"
    return None


# ─── reaction zone helpers ─────────────────────────────────────────────────────

def _utc_day_start(ts: int) -> int:
    """UTC midnight (00:00:00) of the calendar day containing ts."""
    return (ts // 86400) * 86400


def _detect_m15_reaction_events(
    m15_bars: list[dict[str, Any]],
    direction: str,
    entry_price: float,
    decision_ts: int,
) -> list[dict[str, Any]]:
    """Current-day M15 reaction events for TP1 zone discovery.

    Scans completed M15 bars from the current UTC day (open_timestamp >= day_start,
    close_timestamp <= decision_ts).  One event per bar maximum (wick takes priority).

    For LONG: resistance zones above entry (bearish reactions from highs).
    For SHORT: support zones below entry (bullish reactions from lows).
    """
    day_start = _utc_day_start(decision_ts)
    events: list[dict[str, Any]] = []

    for b in m15_bars:
        t = b["time"]
        if t < day_start:
            continue
        if t + M15_SECONDS > decision_ts:
            continue
        o, h, lo, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
        total_range = h - lo
        if total_range < 1e-9:
            continue

        if direction == "LONG":
            if h <= entry_price:
                continue
            upper_wick = h - max(o, c)
            body = abs(c - o)
            if upper_wick / total_range >= WICK_REACTION_MIN_FRACTION:
                events.append({"bar_open_ts": t, "reaction_price": h,
                                "reaction_type": "WICK_REJECTION"})
            elif c < o and body / total_range >= BODY_CLOSE_MIN_FRACTION:
                events.append({"bar_open_ts": t, "reaction_price": h,
                                "reaction_type": "BODY_CLOSE_REJECTION"})
        else:
            if lo >= entry_price:
                continue
            lower_wick = min(o, c) - lo
            body = abs(c - o)
            if lower_wick / total_range >= WICK_REACTION_MIN_FRACTION:
                events.append({"bar_open_ts": t, "reaction_price": lo,
                                "reaction_type": "WICK_REJECTION"})
            elif c > o and body / total_range >= BODY_CLOSE_MIN_FRACTION:
                events.append({"bar_open_ts": t, "reaction_price": lo,
                                "reaction_type": "BODY_CLOSE_REJECTION"})
    return events


def _cluster_reaction_zones(
    events: list[dict[str, Any]],
    zone_tolerance: float,
    direction: str,
    trading_day_start: int,
) -> list[dict[str, Any]]:
    """Group M15 reaction events into zones by proximity.

    Uses greedy single-pass clustering: events sorted by reaction_price; each event
    joins the last cluster if within zone_tolerance of the running cluster mean.
    """
    if not events:
        return []
    sorted_evs = sorted(events, key=lambda e: e["reaction_price"])
    clusters: list[list[dict[str, Any]]] = [[sorted_evs[0]]]
    for ev in sorted_evs[1:]:
        cluster = clusters[-1]
        cluster_mean = sum(e["reaction_price"] for e in cluster) / len(cluster)
        if abs(ev["reaction_price"] - cluster_mean) <= zone_tolerance:
            cluster.append(ev)
        else:
            clusters.append([ev])

    result: list[dict[str, Any]] = []
    for cluster in clusters:
        prices = [e["reaction_price"] for e in cluster]
        timestamps = [e["bar_open_ts"] for e in cluster]
        wick_count = sum(1 for e in cluster if e["reaction_type"] == "WICK_REJECTION")
        body_count = sum(1 for e in cluster if e["reaction_type"] == "BODY_CLOSE_REJECTION")
        zone_center = sum(prices) / len(prices)
        result.append({
            "reaction_zone_id": f"RZ-{direction}-{trading_day_start}-{zone_center:.5f}",
            "trading_day": trading_day_start,
            "zone_center": zone_center,
            "zone_low": min(prices) - zone_tolerance / 2,
            "zone_high": max(prices) + zone_tolerance / 2,
            "first_reaction_ts": min(timestamps),
            "last_reaction_ts": max(timestamps),
            "wick_reaction_count": wick_count,
            "body_close_rejection_count": body_count,
            "total_distinct_reactions": len(cluster),
            "reaction_event_ids": [
                f"{e['bar_open_ts']}-{e['reaction_type']}" for e in cluster
            ],
        })
    return result


def _build_m15_reaction_zones(
    m15_bars: list[dict[str, Any]],
    direction: str,
    entry_price: float,
    stop_price: float,
    decision_ts: int,
    zone_tolerance: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Full reaction-zone pipeline: detect → cluster → filter → rank.

    Returns (qualifying_zones, all_zones) where qualifying_zones satisfy:
      - zone in profit direction relative to entry_price
      - planned_r >= TP1_MINIMUM_PLANNED_R

    Ranking (for qualifying zones):
      1. total_distinct_reactions desc
      2. last_reaction_ts desc
      3. distance_from_entry asc
    """
    day_start = _utc_day_start(decision_ts)
    raw_events = _detect_m15_reaction_events(m15_bars, direction, entry_price, decision_ts)
    all_zones = _cluster_reaction_zones(raw_events, zone_tolerance, direction, day_start)

    risk = abs(entry_price - stop_price) if stop_price is not None else 0.0

    qualifying: list[dict[str, Any]] = []
    for zone in all_zones:
        zc = zone["zone_center"]
        if direction == "LONG" and zc <= entry_price:
            continue
        if direction == "SHORT" and zc >= entry_price:
            continue
        dist = abs(zc - entry_price)
        planned_r = dist / risk if risk > 0 else None
        if planned_r is None or planned_r < TP1_MINIMUM_PLANNED_R:
            continue
        zone = dict(zone)
        zone["distance_from_entry"] = dist
        zone["planned_r"] = planned_r
        qualifying.append(zone)

    qualifying.sort(
        key=lambda z: (-z["total_distinct_reactions"], -z["last_reaction_ts"],
                       z["distance_from_entry"])
    )
    return qualifying, all_zones


# ─── liquidity objective helpers ───────────────────────────────────────────────

def _liq_obj(
    liq_type: str, price: float, first_known_ts: int, source_tf: str,
    evidence: dict[str, Any], entry_price: float, stop_price: float,
    episode_start_ts: int, h1_bars: list[dict[str, Any]],
    direction: str,
) -> dict[str, Any]:
    """Build a liquidity objective dict with common provenance fields."""
    risk = abs(entry_price - stop_price) if stop_price else 0.0
    dist = abs(price - entry_price)
    planned_r = dist / risk if risk > 0 else None

    # untouched_at_decision_time: no H1 bar in [episode_start_ts, ∞) exceeded this level
    touched = False
    for b in h1_bars:
        if b["time"] + H1_SECONDS <= episode_start_ts:
            continue
        if direction == "LONG" and float(b["high"]) >= price:
            touched = True
            break
        if direction == "SHORT" and float(b["low"]) <= price:
            touched = True
            break

    return {
        "liquidity_objective_id": f"LIQ-{liq_type}-{price:.5f}",
        "liquidity_type": liq_type,
        "price": price,
        "first_known_ts": first_known_ts,
        "source_timeframe": source_tf,
        "evidence": evidence,
        "distance_from_entry": dist,
        "planned_r": planned_r,
        "untouched_at_decision_time": not touched,
    }


def _detect_liquidity_objectives(
    h1_bars: list[dict[str, Any]],
    direction: str,
    entry_price: float,
    stop_price: float,
    tp1_price: float,
    episode_start_ts: int,
    decision_ts: int,
    pivot_strength: int,
    equal_tol: float,
) -> list[dict[str, Any]]:
    """Detect all candidate TP2 liquidity objectives from causal data.

    Filters: in profit direction, beyond TP1, external to episode
    (confirmed_at_close_ts < episode_start_ts for swings; bars < episode_start_ts for others).
    Returns unsorted list; caller selects nearest valid as TP2.
    """
    results: list[dict[str, Any]] = []

    def _beyond_tp1(price: float) -> bool:
        if direction == "LONG":
            return price > tp1_price
        return price < tp1_price

    def _in_profit_dir(price: float) -> bool:
        if direction == "LONG":
            return price > entry_price
        return price < entry_price

    # ── 1. H1 swing high/low ─────────────────────────────────────────────────
    swings = _confirmed_swings(h1_bars, episode_start_ts, pivot_strength)
    swing_by_price: dict[float, dict[str, Any]] = {}
    for lv in swings:
        p = lv["price"]
        if not _in_profit_dir(p) or not _beyond_tp1(p):
            continue
        swing_by_price[p] = lv

    # ── 2. Equal highs/lows: 2+ swings within equal_tol of each other ────────
    swing_prices = sorted(swing_by_price.keys())
    equal_clusters: list[list[float]] = []
    for sp in swing_prices:
        if equal_clusters and abs(sp - equal_clusters[-1][-1]) <= equal_tol:
            equal_clusters[-1].append(sp)
        else:
            equal_clusters.append([sp])
    for cluster in equal_clusters:
        if len(cluster) < 2:
            continue
        cluster_mean = sum(cluster) / len(cluster)
        first_ts = min(swing_by_price[p]["h1_open_timestamp"] for p in cluster)
        liq_type = LIQUIDITY_EQUAL_HIGHS if direction == "LONG" else LIQUIDITY_EQUAL_LOWS
        results.append(_liq_obj(
            liq_type, cluster_mean, first_ts, "H1",
            {"prices": cluster, "count": len(cluster)},
            entry_price, stop_price, episode_start_ts, h1_bars, direction,
        ))

    # Add individual swings (SWING_HIGH / SWING_LOW) — use original price (not cluster mean)
    for p, lv in swing_by_price.items():
        liq_type = LIQUIDITY_SWING_HIGH if direction == "LONG" else LIQUIDITY_SWING_LOW
        results.append(_liq_obj(
            liq_type, p, lv["h1_open_timestamp"], "H1",
            {"level_id": lv["level_id"], "level_type": lv["type"]},
            entry_price, stop_price, episode_start_ts, h1_bars, direction,
        ))

    # ── 3. Previous UTC-day extreme ───────────────────────────────────────────
    current_day_start = _utc_day_start(decision_ts)
    prev_day_start = current_day_start - 86400
    prev_day_bars = [b for b in h1_bars if prev_day_start <= b["time"] < current_day_start]
    if prev_day_bars:
        pdh = max(float(b["high"]) for b in prev_day_bars)
        pdl = min(float(b["low"]) for b in prev_day_bars)
        first_ts_pd = min(b["time"] for b in prev_day_bars)
        if direction == "LONG" and _beyond_tp1(pdh) and _in_profit_dir(pdh):
            results.append(_liq_obj(
                LIQUIDITY_PREV_DAY_HIGH, pdh, first_ts_pd, "H1",
                {"prev_day_start": prev_day_start, "prev_day_end": current_day_start},
                entry_price, stop_price, episode_start_ts, h1_bars, direction,
            ))
        if direction == "SHORT" and _beyond_tp1(pdl) and _in_profit_dir(pdl):
            results.append(_liq_obj(
                LIQUIDITY_PREV_DAY_LOW, pdl, first_ts_pd, "H1",
                {"prev_day_start": prev_day_start, "prev_day_end": current_day_start},
                entry_price, stop_price, episode_start_ts, h1_bars, direction,
            ))

    # ── 4. Session extremes (last completed session before decision_ts) ───────
    for sess_name, start_h, end_h in _SESSION_DEFS:
        # Asian crosses midnight (start_h > end_h)
        crosses_midnight = start_h > end_h
        day_ts = current_day_start
        if crosses_midnight:
            # Session starts at start_h of previous day, ends at end_h of current day
            sess_start = (day_ts - 86400) + start_h * 3600
            sess_end = day_ts + end_h * 3600
        else:
            sess_start = day_ts + start_h * 3600
            sess_end = day_ts + end_h * 3600

        if sess_end > decision_ts:
            # Session hasn't completed yet; try the prior period
            if crosses_midnight:
                sess_start -= 86400
                sess_end -= 86400
            else:
                sess_start -= 86400
                sess_end -= 86400

        if sess_end > decision_ts:
            continue  # still not completed

        sess_bars = [b for b in h1_bars if sess_start <= b["time"] < sess_end]
        if not sess_bars:
            continue
        sess_high = max(float(b["high"]) for b in sess_bars)
        sess_low = min(float(b["low"]) for b in sess_bars)
        first_ts_s = min(b["time"] for b in sess_bars)
        ev_base = {"session": sess_name, "session_start": sess_start, "session_end": sess_end}
        if direction == "LONG" and _beyond_tp1(sess_high) and _in_profit_dir(sess_high):
            results.append(_liq_obj(
                LIQUIDITY_SESSION_HIGH, sess_high, first_ts_s, "H1",
                {**ev_base, "kind": "high"}, entry_price, stop_price,
                episode_start_ts, h1_bars, direction,
            ))
        if direction == "SHORT" and _beyond_tp1(sess_low) and _in_profit_dir(sess_low):
            results.append(_liq_obj(
                LIQUIDITY_SESSION_LOW, sess_low, first_ts_s, "H1",
                {**ev_base, "kind": "low"}, entry_price, stop_price,
                episode_start_ts, h1_bars, direction,
            ))

    # ── 5. Untouched external extreme ────────────────────────────────────────
    # Farthest pre-episode confirmed H1 extreme in profit direction beyond TP1
    pre_ep_bars = [b for b in h1_bars if b["time"] + H1_SECONDS <= episode_start_ts]
    if pre_ep_bars:
        if direction == "LONG":
            ext_price = max(float(b["high"]) for b in pre_ep_bars)
            first_ts_e = next(b["time"] for b in pre_ep_bars if float(b["high"]) == ext_price)
        else:
            ext_price = min(float(b["low"]) for b in pre_ep_bars)
            first_ts_e = next(b["time"] for b in pre_ep_bars if float(b["low"]) == ext_price)
        if _in_profit_dir(ext_price) and _beyond_tp1(ext_price):
            results.append(_liq_obj(
                LIQUIDITY_UNTOUCHED_EXTREME, ext_price, first_ts_e, "H1",
                {"lookback_bar_count": len(pre_ep_bars)},
                entry_price, stop_price, episode_start_ts, h1_bars, direction,
            ))

    return results


# ─── evaluator ─────────────────────────────────────────────────────────────────

class KojoStructureReclaimV3Evaluator:
    """Deterministic, causal evaluator for KOJO_STRUCTURE_RECLAIM_V3.

    SOURCE_FIDELITY_BLOCKED = false.  READY_FOR_DISCOVERY = true.
    PRODUCTION_ELIGIBLE = false.  BROKER_WRITES = 0.

    V3 changes vs V2:
      1. Opportunity retirement: CONSUMED-only permanent retirement.
      2. H1 confirmation: break bar close IS the H1 confirmation; temporal guard enforces
         H1_CLOSE → M15_RETEST → M15_REJECTION → ENTRY ordering.
      3. TP1: current-day M15 reaction zone with wick/body-close evidence, planned_r >= 1.0.
      4. TP2: nearest valid external liquidity objective (swing, equal H/L, prev-day,
         session, untouched extreme). NEAREST_VALID_EXTERNAL_LIQUIDITY policy.
      5. Initial trade plan: targets_are_objectives=true, mandatory_hold_to_target=false.
    """

    VERSION = "KOJO_STRUCTURE_RECLAIM_V3_EVALUATOR"

    def __init__(self) -> None:
        self.strategy_version: StrategyVersion | None = None
        self.parameters: ParameterSet | None = None
        self._h1_bars: list[dict[str, Any]] = []
        self._m15_bars: list[dict[str, Any]] = []
        self._setups: dict[str, dict[str, Any]] = {}
        # V3: split retirement identity
        #   _consumed_opportunity_keys — permanent, CONSUMED only
        #   _terminal_episode_ids — all terminal states, keyed by setup_id
        self._consumed_opportunity_keys: set[tuple[str, str]] = set()
        self._terminal_episode_ids: set[str] = set()
        self._emitted_lifecycle: set[tuple[str, str]] = set()
        self._rejections: dict[str, int] = {}
        self._weak_m15_rejections: int = 0

    def initialize(self, strategy_version: StrategyVersion, parameter_set: ParameterSet) -> None:
        if strategy_version.strategy_version_id != f"{STRATEGY_ID}@{VERSION}":
            raise ValueError(
                f"KojoStructureReclaimV3Evaluator requires {STRATEGY_ID}@{VERSION}, "
                f"got {strategy_version.strategy_version_id}"
            )
        schema = kojo_structure_reclaim_v3_parameter_schema()
        schema.validate(parameter_set.values)
        self.strategy_version = strategy_version
        self.parameters = parameter_set

    @property
    def _values(self) -> dict[str, Any]:
        if self.parameters is None:
            raise RuntimeError("evaluator not initialized")
        return dict(self.parameters.values)

    # ── event routing ──────────────────────────────────────────────────────────

    def consume_market_event(
        self, event: MarketEvent
    ) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        if event.canonical_instrument != INSTRUMENT or not event.completed:
            return ()
        if event.timeframe == TIMEFRAME_H1:
            return self._on_h1(event)
        if event.timeframe == TIMEFRAME_M15:
            return self._on_m15(event)
        return ()

    # ── H1 event handler ───────────────────────────────────────────────────────

    def _on_h1(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        self._h1_bars.append(_bar(event))
        index = len(self._h1_bars) - 1
        outputs: list[SetupLifecycleEvent | EntrySignal] = []

        for setup in list(self._setups.values()):
            if setup["state"] in TERMINAL_STATES:
                continue
            adv = self._advance_setup_on_h1(setup, event, index)
            outputs.extend(adv)

        new_outputs = self._detect_h1_breaks(event, index)
        outputs.extend(new_outputs)
        return tuple(outputs)

    def _detect_h1_breaks(
        self, event: MarketEvent, h1_index: int
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        if h1_index < 1:
            return outputs

        values = self._values
        pivot_strength = int(values["pivot_strength"])
        current_close = float(event.close)
        prev_h1_close = float(self._h1_bars[h1_index - 1]["close"])

        prev_close_ts = self._h1_bars[h1_index - 1]["time"] + H1_SECONDS
        levels = _confirmed_swings(self._h1_bars, prev_close_ts, pivot_strength)

        active_level_keys = {
            (s["structural_level_id"], s["direction"])
            for s in self._setups.values()
            if s["state"] not in TERMINAL_STATES
        }

        for level in levels:
            lid = level["level_id"]
            level_price = level["price"]

            if level["type"] == "RESISTANCE" and current_close > level_price:
                direction = "LONG"
                level_key = (lid, direction)
                if level_key in self._consumed_opportunity_keys or level_key in active_level_keys:
                    continue
                # V3: require genuine new causal break — previous bar must NOT already be
                # above the level (prevents stale continuation bars from creating duplicates
                # after INVALIDATED/EXPIRED without an arbitrary time cooldown)
                if prev_h1_close > level_price:
                    continue
                setup = self._create_setup(event, h1_index, level, direction)
                outputs.extend(self._emit_lifecycle(setup, SETUP_DETECTED, event))
                self._setups[setup["setup_id"]] = setup

            elif level["type"] == "SUPPORT" and current_close < level_price:
                direction = "SHORT"
                level_key = (lid, direction)
                if level_key in self._consumed_opportunity_keys or level_key in active_level_keys:
                    continue
                # V3: require genuine new causal break
                if prev_h1_close < level_price:
                    continue
                setup = self._create_setup(event, h1_index, level, direction)
                outputs.extend(self._emit_lifecycle(setup, SETUP_DETECTED, event))
                self._setups[setup["setup_id"]] = setup

        return outputs

    def _create_setup(
        self,
        break_event: MarketEvent,
        h1_break_index: int,
        level: dict[str, Any],
        direction: str,
    ) -> dict[str, Any]:
        ema_state = _ema_snapshot(self._h1_bars)
        setup_id = "KSRV3_" + fingerprint({
            "strategy": STRATEGY_ID,
            "version": VERSION,
            "parameter_set_fingerprint": self.parameters.fingerprint,
            "level_id": level["level_id"],
            "break_h1_open_ts": break_event.open_timestamp,
            "direction": direction,
        })[:24]

        if direction == "LONG":
            episode_envelope_high = float(break_event.high)
            episode_envelope_low = float(break_event.low)
        else:
            episode_envelope_high = float(break_event.high)
            episode_envelope_low = float(break_event.low)

        return {
            "setup_id": setup_id,
            "direction": direction,
            "structural_level_id": level["level_id"],
            "structural_level_price": level["price"],
            "structural_level_type": level["type"],
            "structure_timeframe": TIMEFRAME_H1,
            "episode_start_ts": break_event.close_timestamp,
            "break_timestamp": break_event.close_timestamp,
            "break_direction": direction,
            "break_h1_index": h1_break_index,
            "break_h1_close": float(break_event.close),
            "break_h1_evidence": _event_evidence(break_event),
            "episode_envelope_high": episode_envelope_high,
            "episode_envelope_low": episode_envelope_low,
            "pullback_extreme": None,
            "pullback_type": "UNCLASSIFIED",
            "ema_state": ema_state,
            "retest_timestamp": None,
            "retest_h1_index": None,
            "retest_m15_index": None,
            "confirmation_timestamp": None,
            "confirmation_m15_index": None,
            "confirmation_type": None,
            # H1 confirmation = the completed H1 bar that closes beyond the structural level
            # (the break bar itself). H1_CLOSE_BEYOND_LEVEL_REQUIRED=true.
            "h1_context_evidence": _event_evidence(break_event),
            "h1_key_level_id": level["level_id"],
            "h1_confirmation_open_ts": break_event.open_timestamp,
            "h1_confirmation_close_ts": break_event.close_timestamp,
            "h1_confirmation_open": float(break_event.open),
            "h1_confirmation_high": float(break_event.high),
            "h1_confirmation_low": float(break_event.low),
            "h1_confirmation_close": float(break_event.close),
            "h1_confirmation_relation_to_level": (
                "CLOSE_BEYOND" if direction == "LONG" else "CLOSE_BELOW"
            ),
            # M15 temporal tracking (set when transitions occur)
            "m15_retest_first_open_ts": None,
            "m15_rejection_open_ts": None,
            "m15_confirmation_evidence": None,
            "state": WAITING_FOR_RETEST,
            "stop_basis": None,
            "structural_extreme": None,
            "stop_buffer": float(self._values["stop_buffer_value"]),
            "final_stop": None,
            "tp1": None,
            "tp1_provenance": None,
            "tp2": None,
            "intended_entry": None,
            "m15_bars_in_retest": [],
            "expiry_h1_index": h1_break_index + int(self._values["max_retest_wait_h1_bars"]),
        }

    def _advance_setup_on_h1(
        self, setup: dict[str, Any], event: MarketEvent, h1_index: int
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        state = setup["state"]
        direction = setup["direction"]
        level_price = float(setup["structural_level_price"])
        values = self._values

        if state == WAITING_FOR_RETEST:
            if h1_index >= setup["expiry_h1_index"]:
                self._terminate_setup(setup, EXPIRED)
                outputs.extend(self._emit_lifecycle(setup, EXPIRED, event, reason="MAX_RETEST_WAIT_EXCEEDED"))
                return outputs

            close = float(event.close)
            if direction == "LONG" and close < level_price:
                self._terminate_setup(setup, INVALIDATED)
                outputs.extend(self._emit_lifecycle(setup, INVALIDATED, event, reason="H1_CLOSE_BELOW_LEVEL"))
                return outputs
            if direction == "SHORT" and close > level_price:
                self._terminate_setup(setup, INVALIDATED)
                outputs.extend(self._emit_lifecycle(setup, INVALIDATED, event, reason="H1_CLOSE_ABOVE_LEVEL"))
                return outputs

            if direction == "LONG":
                setup["episode_envelope_high"] = max(setup["episode_envelope_high"], float(event.high))
            else:
                setup["episode_envelope_low"] = min(setup["episode_envelope_low"], float(event.low))

            h1_atr = _atr(self._h1_bars, 14)
            tolerance = float(values["retest_tolerance_atr"]) * h1_atr
            retest = False
            if direction == "LONG":
                retest = (float(event.low) <= level_price + tolerance and
                          float(event.close) >= level_price - tolerance)
            else:
                retest = (float(event.high) >= level_price - tolerance and
                          float(event.close) <= level_price + tolerance)

            if retest:
                setup["retest_timestamp"] = event.close_timestamp
                setup["retest_h1_index"] = h1_index
                setup["state"] = RETEST_SEEN
                setup["confirmation_expiry_m15_count"] = int(values["max_confirmation_wait_m15_bars"])
                setup["retest_m15_bars_seen"] = 0
                outputs.extend(self._emit_lifecycle(setup, RETEST_SEEN, event))

        return outputs

    # ── M15 event handler ──────────────────────────────────────────────────────

    def _on_m15(self, event: MarketEvent) -> tuple[SetupLifecycleEvent | EntrySignal, ...]:
        self._m15_bars.append(_bar(event))
        m15_index = len(self._m15_bars) - 1
        outputs: list[SetupLifecycleEvent | EntrySignal] = []

        for setup in list(self._setups.values()):
            if setup["state"] in TERMINAL_STATES:
                continue
            adv = self._advance_setup_on_m15(setup, event, m15_index)
            outputs.extend(adv)

        return tuple(outputs)

    def _advance_setup_on_m15(
        self, setup: dict[str, Any], event: MarketEvent, m15_index: int
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        state = setup["state"]
        direction = setup["direction"]
        level_price = float(setup["structural_level_price"])
        values = self._values

        if state == WAITING_FOR_RETEST:
            # Temporal invariant: M15 retest must occur AFTER the H1 confirmation close.
            # H1_CLOSE → M15_RETEST → M15_REJECTION → ENTRY
            if event.open_timestamp < setup["episode_start_ts"]:
                return outputs

            m15_atr = _atr(self._m15_bars, 14)
            tolerance = float(values["retest_tolerance_atr"]) * m15_atr
            retest = False
            if direction == "LONG":
                retest = (float(event.low) <= level_price + tolerance and
                          float(event.close) >= level_price - tolerance)
            else:
                retest = (float(event.high) >= level_price - tolerance and
                          float(event.close) <= level_price + tolerance)
            if retest:
                setup["retest_timestamp"] = event.close_timestamp
                setup["retest_m15_index"] = m15_index
                setup["m15_retest_first_open_ts"] = event.open_timestamp
                setup["state"] = RETEST_SEEN
                setup["confirmation_expiry_m15_count"] = int(values["max_confirmation_wait_m15_bars"])
                setup["retest_m15_bars_seen"] = 0
                outputs.extend(self._emit_lifecycle(setup, RETEST_SEEN, event))
            return outputs

        if state == RETEST_SEEN:
            setup["retest_m15_bars_seen"] = setup.get("retest_m15_bars_seen", 0) + 1
            setup.setdefault("m15_bars_in_retest", []).append(m15_index)

            if direction == "LONG":
                extreme = float(event.low)
                if setup["pullback_extreme"] is None or extreme < setup["pullback_extreme"]:
                    setup["pullback_extreme"] = extreme
                setup["episode_envelope_low"] = min(setup["episode_envelope_low"], extreme)
            else:
                extreme = float(event.high)
                if setup["pullback_extreme"] is None or extreme > setup["pullback_extreme"]:
                    setup["pullback_extreme"] = extreme
                setup["episode_envelope_high"] = max(setup["episode_envelope_high"], extreme)

            if setup["retest_m15_bars_seen"] > int(values["max_confirmation_wait_m15_bars"]):
                self._terminate_setup(setup, EXPIRED)
                outputs.extend(
                    self._emit_lifecycle(setup, EXPIRED, event, reason="MAX_CONFIRMATION_WAIT_EXCEEDED")
                )
                return outputs

            if direction == "LONG" and float(event.close) < level_price:
                self._terminate_setup(setup, INVALIDATED)
                outputs.extend(
                    self._emit_lifecycle(setup, INVALIDATED, event, reason="M15_CLOSE_BELOW_LEVEL_DURING_RETEST")
                )
                return outputs
            if direction == "SHORT" and float(event.close) > level_price:
                self._terminate_setup(setup, INVALIDATED)
                outputs.extend(
                    self._emit_lifecycle(setup, INVALIDATED, event, reason="M15_CLOSE_ABOVE_LEVEL_DURING_RETEST")
                )
                return outputs

            conf_type = _m15_strong_confirmation_type(self._m15_bars, m15_index, direction)
            if conf_type is not None:
                setup["confirmation_timestamp"] = event.close_timestamp
                setup["confirmation_m15_index"] = m15_index
                setup["confirmation_type"] = conf_type
                setup["m15_rejection_open_ts"] = event.open_timestamp
                setup["m15_confirmation_evidence"] = _event_evidence(event)
                setup["state"] = CONFIRMED_PENDING_NEXT_OPEN
                outputs.extend(self._emit_lifecycle(setup, CONFIRMED, event, confirmation_type=conf_type))
            else:
                from .kojo_structure_reclaim import (
                    _continuation_close_bullish, _continuation_close_bearish
                )
                curr = self._m15_bars[m15_index]
                if (direction == "LONG" and _continuation_close_bullish(curr)) or \
                   (direction == "SHORT" and _continuation_close_bearish(curr)):
                    self._weak_m15_rejections += 1
            return outputs

        if state == CONFIRMED_PENDING_NEXT_OPEN:
            entry_price = float(event.open)
            decision_ts = setup["confirmation_timestamp"]
            entry_outputs = self._try_emit_signal(setup, event, m15_index, entry_price, decision_ts)
            outputs.extend(entry_outputs)
            return outputs

        return outputs

    def _try_emit_signal(
        self,
        setup: dict[str, Any],
        event: MarketEvent,
        m15_index: int,
        entry_price: float,
        decision_ts: int,
    ) -> list[SetupLifecycleEvent | EntrySignal]:
        outputs: list[SetupLifecycleEvent | EntrySignal] = []
        direction = setup["direction"]
        level_price = float(setup["structural_level_price"])
        values = self._values
        buffer = float(values["stop_buffer_value"])

        stop_price, stop_basis, structural_extreme = self._compute_stop(
            setup, entry_price, direction, level_price, buffer
        )
        if stop_price is None:
            self._reject("INVALID_STOP_GEOMETRY")
            self._terminate_setup(setup, INVALIDATED)
            outputs.extend(self._emit_lifecycle(setup, INVALIDATED, event, reason="INVALID_STOP_GEOMETRY"))
            return outputs

        tp1, tp1_prov, tp2 = self._compute_targets_v3(setup, direction, entry_price, stop_price)
        if tp1 is None:
            self._reject("NO_QUALIFYING_TP1_REACTION_ZONE")
            self._terminate_setup(setup, CONSUMED)
            outputs.extend(
                self._emit_lifecycle(setup, CONSUMED, event, reason="NO_QUALIFYING_TP1_REACTION_ZONE")
            )
            return outputs

        if direction == "LONG":
            valid = stop_price < entry_price < tp1
        else:
            valid = tp1 < entry_price < stop_price
        if not valid:
            self._reject("INVALID_SIGNAL_GEOMETRY")
            self._terminate_setup(setup, INVALIDATED)
            outputs.extend(self._emit_lifecycle(setup, INVALIDATED, event, reason="INVALID_SIGNAL_GEOMETRY"))
            return outputs

        setup["stop_basis"] = stop_basis
        setup["structural_extreme"] = structural_extreme
        setup["final_stop"] = stop_price
        setup["tp1"] = tp1
        setup["tp1_provenance"] = tp1_prov
        setup["tp2"] = tp2
        setup["intended_entry"] = entry_price

        signal_id = "KSRV3_SIG_" + fingerprint({
            "setup_id": setup["setup_id"],
            "confirmation_timestamp": setup["confirmation_timestamp"],
            "direction": direction,
            "entry_price": entry_price,
        })[:24]

        provenance = {
            "strategy_id": STRATEGY_ID,
            "strategy_version": self.strategy_version.strategy_version_id,
            "parameter_set_fingerprint": self.parameters.fingerprint,
            "evaluator_version": self.VERSION,
            "timeframe": TIMEFRAME_H1,
            "entry_timeframe": TIMEFRAME_M15,
            "setup_id": setup["setup_id"],
            "direction": direction,
            "structural_level_id": setup["structural_level_id"],
            "structural_level_price": setup["structural_level_price"],
            "structural_level_type": setup["structural_level_type"],
            "structure_timeframe": setup["structure_timeframe"],
            "episode_start_ts": setup["episode_start_ts"],
            "episode_envelope_high": setup["episode_envelope_high"],
            "episode_envelope_low": setup["episode_envelope_low"],
            "pullback_extreme": setup["pullback_extreme"],
            "pullback_type": setup["pullback_type"],
            "break_timestamp": setup["break_timestamp"],
            "break_direction": setup["break_direction"],
            "break_h1_evidence": setup["break_h1_evidence"],
            # H1 confirmation source rule: break bar close beyond level is the H1 confirmation.
            # H1_CLOSE_BEYOND_LEVEL_REQUIRED=true, H1_CONFIRMATION_IS_COMPLETED_BAR=true.
            "h1_context_evidence": setup["h1_context_evidence"],
            "h1_key_level_id": setup["h1_key_level_id"],
            "h1_confirmation_open_ts": setup["h1_confirmation_open_ts"],
            "h1_confirmation_close_ts": setup["h1_confirmation_close_ts"],
            "h1_confirmation_open": setup["h1_confirmation_open"],
            "h1_confirmation_high": setup["h1_confirmation_high"],
            "h1_confirmation_low": setup["h1_confirmation_low"],
            "h1_confirmation_close": setup["h1_confirmation_close"],
            "h1_confirmation_relation_to_level": setup["h1_confirmation_relation_to_level"],
            # M15 temporal ordering: H1_CLOSE → M15_RETEST → M15_REJECTION → ENTRY
            "m15_retest_first_ts": setup["m15_retest_first_open_ts"],
            "m15_retest_level_id": setup["structural_level_id"],
            "m15_rejection_ts": setup["m15_rejection_open_ts"],
            "entry_decision_ts": decision_ts,
            "m15_confirmation_evidence": setup["m15_confirmation_evidence"],
            "retest_timestamp": setup["retest_timestamp"],
            "confirmation_timestamp": setup["confirmation_timestamp"],
            "confirmation_type": setup["confirmation_type"],
            "ema_state": setup["ema_state"],
            "stop_basis": stop_basis,
            "structural_extreme": structural_extreme,
            "stop_buffer_value": buffer,
            "final_stop": stop_price,
            "tp1": tp1,
            "tp1_provenance": tp1_prov,
            "tp2": tp2,
            "intended_entry": entry_price,
            "available_through": event.close_timestamp,
            "tp1_class": "CURRENT_DAY_M15_REACTION_ZONE",
            # Initial trade plan — separated from trade management policy.
            # targets_are_objectives=true, mandatory_hold_to_target=false.
            "targets_are_objectives": TARGETS_ARE_OBJECTIVES,
            "mandatory_hold_to_target": MANDATORY_HOLD_TO_TARGET,
            "trade_management_policy_ref": "SOURCE_RULE_REQUIRED",
            "initial_trade_plan": {
                "planned_entry": entry_price,
                "initial_stop": stop_price,
                "planned_tp1": tp1,
                "planned_tp1_reason": "EXTERNAL_STRUCTURAL_OBJECTIVE",
                "planned_tp2": tp2,
                "planned_tp2_reason": "EXTERNAL_STRUCTURAL_OBJECTIVE",
                "targets_are_objectives": TARGETS_ARE_OBJECTIVES,
                "mandatory_hold_to_target": MANDATORY_HOLD_TO_TARGET,
                "trade_management_policy_ref": "SOURCE_RULE_REQUIRED",
            },
            # V3 status — SOURCE_FIDELITY_BLOCKED=false; not yet production-eligible (no validation)
            "v3_status": "READY_FOR_DISCOVERY",
            "production_eligible": False,
            # dual_timeframe_confirmed removed (V2 mislabel); M15_ONLY_BASELINE_ENABLED=false
        }

        self._terminate_setup(setup, CONSUMED)
        outputs.extend(self._emit_lifecycle(setup, CONSUMED, event, entry=entry_price))
        outputs.append(EntrySignal(
            signal_id=signal_id,
            strategy_version_id=self.strategy_version.strategy_version_id,
            canonical_instrument=INSTRUMENT,
            direction=direction,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=tp1,
            decision_timestamp=decision_ts,
            order_type="MARKET",
            provenance=provenance,
        ))
        return outputs

    def _terminate_setup(self, setup: dict[str, Any], terminal_state: str) -> None:
        """Set terminal state.

        V3 retirement policy:
          CONSUMED   → add to _consumed_opportunity_keys (permanent; same economic opportunity
                        must never re-fire) AND _terminal_episode_ids (episode-level)
          INVALIDATED/EXPIRED → add to _terminal_episode_ids only (episode terminated, but
                        the structural level may form a new causal episode later)
        """
        setup["state"] = terminal_state
        self._terminal_episode_ids.add(setup["setup_id"])
        if terminal_state == CONSUMED:
            self._consumed_opportunity_keys.add((setup["structural_level_id"], setup["direction"]))

    def _compute_stop(
        self,
        setup: dict[str, Any],
        entry_price: float,
        direction: str,
        level_price: float,
        buffer: float,
    ) -> tuple[float | None, str | None, float | None]:
        """Identical stop computation to V1/V2."""
        retest_m15_indices = setup.get("m15_bars_in_retest", [])
        m15_bars_in_zone = [self._m15_bars[i] for i in retest_m15_indices if i < len(self._m15_bars)]

        if direction == "LONG":
            if m15_bars_in_zone:
                extreme = min(float(b["low"]) for b in m15_bars_in_zone)
                stop = extreme - buffer
                if stop < entry_price:
                    return stop, "M15_RETEST_ZONE_SWING_LOW", extreme
            stop = level_price - buffer
            if stop < entry_price:
                return stop, "STRUCTURAL_LEVEL_MINUS_BUFFER", level_price
            return None, None, None
        else:
            if m15_bars_in_zone:
                extreme = max(float(b["high"]) for b in m15_bars_in_zone)
                stop = extreme + buffer
                if stop > entry_price:
                    return stop, "M15_RETEST_ZONE_SWING_HIGH", extreme
            stop = level_price + buffer
            if stop > entry_price:
                return stop, "STRUCTURAL_LEVEL_PLUS_BUFFER", level_price
            return None, None, None

    def _compute_targets_v3(
        self, setup: dict[str, Any], direction: str, entry_price: float, stop_price: float | None = None
    ) -> tuple[float | None, dict[str, Any] | None, float | None]:
        """TP1 from current-day M15 reaction zones; TP2 from liquidity objectives.

        TP1: current UTC-day M15 reaction zone with planned_r >= TP1_MINIMUM_PLANNED_R.
        TP2: nearest valid external liquidity objective beyond TP1.

        Rejection code NO_QUALIFYING_TP1_REACTION_ZONE emitted when no qualifying
        zone exists (no current-day M15 reaction evidence, or all zones below 1.0R).
        """
        effective_stop = stop_price if stop_price is not None else setup.get("final_stop")
        episode_start_ts = setup["episode_start_ts"]
        envelope_high = setup["episode_envelope_high"]
        envelope_low = setup["episode_envelope_low"]

        # Use m15_atr from available M15 bars as zone tolerance base
        m15_atr = _atr(self._m15_bars, 14) if self._m15_bars else 0.0
        zone_tolerance = float(self._values["retest_tolerance_atr"]) * m15_atr
        if zone_tolerance < 1e-9:
            zone_tolerance = 1.0  # fallback if ATR not yet meaningful

        # Use confirmation_timestamp as the decision time for causal day boundary
        decision_ts = setup.get("confirmation_timestamp") or (
            self._m15_bars[-1]["time"] + M15_SECONDS if self._m15_bars else 0
        )

        qualifying_zones, all_zones = _build_m15_reaction_zones(
            self._m15_bars, direction, entry_price,
            effective_stop or entry_price, decision_ts, zone_tolerance,
        )

        if not qualifying_zones:
            return None, None, None

        tp1_zone = qualifying_zones[0]
        tp1_price = tp1_zone["zone_center"]
        risk = abs(entry_price - effective_stop) if effective_stop is not None else 0.0
        target_distance = abs(tp1_price - entry_price)
        planned_r = tp1_zone["planned_r"]
        envelope_width = abs(envelope_high - envelope_low)

        # TP2: liquidity objectives
        pivot_strength = int(self._values["pivot_strength"])
        equal_tol = zone_tolerance  # reuse same tolerance for equal-highs clustering
        liq_objectives = _detect_liquidity_objectives(
            self._h1_bars, direction, entry_price, effective_stop or entry_price,
            tp1_price, episode_start_ts, decision_ts,
            pivot_strength, equal_tol,
        )
        # Filter: in profit direction, beyond TP1
        valid_liq = [
            o for o in liq_objectives
            if o["distance_from_entry"] > target_distance
        ]
        valid_liq.sort(key=lambda o: o["distance_from_entry"])
        tp2_obj = valid_liq[0] if valid_liq else None
        tp2_price = tp2_obj["price"] if tp2_obj else None

        risk = abs(entry_price - effective_stop) if effective_stop is not None else 0.0
        stop_distance = risk
        dist_over_env = target_distance / envelope_width if envelope_width > 1e-9 else None
        dist_over_stop = target_distance / stop_distance if stop_distance > 1e-9 else None

        tp1_prov = {
            "tp1_class": "CURRENT_DAY_M15_REACTION_ZONE",
            "reaction_zone_id": tp1_zone["reaction_zone_id"],
            "zone_center": tp1_zone["zone_center"],
            "zone_low": tp1_zone["zone_low"],
            "zone_high": tp1_zone["zone_high"],
            "trading_day": tp1_zone["trading_day"],
            "first_reaction_ts": tp1_zone["first_reaction_ts"],
            "last_reaction_ts": tp1_zone["last_reaction_ts"],
            "wick_reaction_count": tp1_zone["wick_reaction_count"],
            "body_close_rejection_count": tp1_zone["body_close_rejection_count"],
            "total_distinct_reactions": tp1_zone["total_distinct_reactions"],
            "reaction_event_ids": tp1_zone["reaction_event_ids"],
            "distance_from_entry": target_distance,
            "planned_r": planned_r,
            "tp1_minimum_r_applied": TP1_MINIMUM_PLANNED_R,
            "episode_start_ts": episode_start_ts,
            "episode_envelope_high": envelope_high,
            "episode_envelope_low": envelope_low,
            "episode_envelope_width": envelope_width,
            # All candidates for auditability
            "all_reaction_zones": all_zones,
            "all_qualifying_zones": qualifying_zones,
            # TP2 selection
            "tp2_selection_policy": TP2_SELECTION_POLICY,
            "tp2_selection_policy_source_status": TP2_SELECTION_POLICY_SOURCE_STATUS,
            "tp2_objective": tp2_obj,
            "tp2_candidate_class": TP2_SELECTION_POLICY,
            "all_liquidity_objectives": valid_liq,
            # Diagnostic block for downstream consumers and audit
            "tp1_diagnostics": {
                "tp1_candidate_class": "CURRENT_DAY_M15_REACTION_ZONE",
                "target_selection_source_rule_required": TARGET_SELECTION_SOURCE_RULE_REQUIRED,
                "reaction_zone_id": tp1_zone["reaction_zone_id"],
                "first_reaction_ts": tp1_zone["first_reaction_ts"],
                "entry_price": entry_price,
                "target_distance": target_distance,
                "spread_at_decision_bar": None,
                "tick_size": None,
                "episode_envelope_width": envelope_width,
                "stop_distance": stop_distance,
                "planned_r": planned_r,
                "target_distance_over_spread": None,
                "target_distance_over_envelope_width": dist_over_env,
                "target_distance_over_stop_distance": dist_over_stop,
                "near_coincident_class": TP1_NEAR_COINCIDENT_BOUNDARY,
            },
        }

        return tp1_price, tp1_prov, tp2_price

    # ── lifecycle events ───────────────────────────────────────────────────────

    def _emit_lifecycle(
        self, setup: dict[str, Any], status: str, event: MarketEvent, **extra: Any
    ) -> list[SetupLifecycleEvent]:
        key = (setup["setup_id"], status)
        if key in self._emitted_lifecycle:
            return []
        self._emitted_lifecycle.add(key)
        provenance = {
            "setup_id": setup["setup_id"],
            "structural_level_id": setup["structural_level_id"],
            "structural_level_price": setup["structural_level_price"],
            "direction": setup["direction"],
            "state": setup["state"],
            "parameter_set_fingerprint": self.parameters.fingerprint,
            **extra,
        }
        return [SetupLifecycleEvent(
            setup_id=setup["setup_id"],
            strategy_version_id=self.strategy_version.strategy_version_id,
            canonical_instrument=INSTRUMENT,
            status=status,
            event_timestamp=event.close_timestamp,
            provenance=provenance,
        )]

    def _reject(self, code: str) -> None:
        self._rejections[code] = self._rejections.get(code, 0) + 1

    # ── snapshot / restore ─────────────────────────────────────────────────────

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "h1_bars": list(self._h1_bars),
            "m15_bars": list(self._m15_bars),
            "setups": {k: dict(v) for k, v in self._setups.items()},
            "consumed_opportunity_keys": [list(x) for x in sorted(self._consumed_opportunity_keys)],
            "terminal_episode_ids": sorted(self._terminal_episode_ids),
            "emitted_lifecycle": [list(x) for x in sorted(self._emitted_lifecycle)],
            "rejections": dict(self._rejections),
            "weak_m15_rejections": self._weak_m15_rejections,
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        self._h1_bars = list(state.get("h1_bars", []))
        self._m15_bars = list(state.get("m15_bars", []))
        self._setups = {k: dict(v) for k, v in state.get("setups", {}).items()}
        self._consumed_opportunity_keys = {
            tuple(x) for x in state.get("consumed_opportunity_keys", [])
        }
        self._terminal_episode_ids = set(state.get("terminal_episode_ids", []))
        self._emitted_lifecycle = {tuple(x) for x in state.get("emitted_lifecycle", [])}
        self._rejections = dict(state.get("rejections", {}))
        self._weak_m15_rejections = state.get("weak_m15_rejections", 0)

    def diagnostics(self) -> dict[str, Any]:
        return {
            # V3 source fidelity status
            "SOURCE_FIDELITY_BLOCKED": SOURCE_FIDELITY_BLOCKED,
            "READY_FOR_DISCOVERY": READY_FOR_DISCOVERY,
            "READY_FOR_VALIDATION": READY_FOR_VALIDATION,
            "READY_FOR_SHADOW_SIGNALS": READY_FOR_SHADOW_SIGNALS,
            "READY_FOR_EXECUTION": READY_FOR_EXECUTION,
            "source_fidelity_blocked_reasons": list(SOURCE_FIDELITY_BLOCKED_REASONS),
            # retirement accounting
            "consumed_opportunity_keys_count": len(self._consumed_opportunity_keys),
            "terminal_episode_ids_count": len(self._terminal_episode_ids),
            # operational counts
            "rejection_counts": dict(self._rejections),
            "weak_m15_rejections": self._weak_m15_rejections,
            "active_setup_count": sum(
                1 for s in self._setups.values() if s["state"] not in TERMINAL_STATES
            ),
            "setup_states": {k: v["state"] for k, v in self._setups.items()},
            # NOT emitted (removed from V3):
            #   consumed_level_keys_count — replaced by consumed_opportunity_keys_count
            #   DUAL_TF_BASELINE_ENFORCED — semantically mislabeled in V2; removed
        }


# ─── parameter schema (identical to V2) ───────────────────────────────────────

def kojo_structure_reclaim_v3_parameter_schema() -> ParameterSchema:
    """V3 parameters identical to V1/V2.

    All semantic corrections and scaffolds are structural (evaluator logic), not
    parametric.  PARAMETER_SEARCH = false.
    """
    return ParameterSchema(
        "kojo-structure-reclaim-v3",
        {
            "pivot_strength": {
                "required": True,
                "minimum": 1,
                "maximum": 10,
                "description": "H1 swing pivot lookback bars on each side",
            },
            "retest_tolerance_atr": {
                "required": True,
                "minimum": 0.1,
                "maximum": 3.0,
                "description": "Fraction of ATR within which price must approach the level to count as retest",
            },
            "max_retest_wait_h1_bars": {
                "required": True,
                "minimum": 4,
                "maximum": 100,
                "description": "Max H1 bars to wait for retest after structural break",
            },
            "max_confirmation_wait_m15_bars": {
                "required": True,
                "minimum": 4,
                "maximum": 100,
                "description": "Max M15 bars to wait for confirmation after retest",
            },
            "stop_buffer_type": {
                "required": True,
                "enum": ["PRICE"],
                "description": "Stop buffer type (PRICE only)",
            },
            "stop_buffer_value": {
                "required": True,
                "minimum": 0.0,
                "maximum": 1000.0,
                "description": "Price units beyond structural swing extreme for stop",
            },
        },
    )


def kojo_structure_reclaim_v3_baseline_parameter_set(
    strategy_version_id: str | None = None,
) -> ParameterSet:
    """V3 research hypothesis baseline — identical values to V1/V2.

    SOURCE_FIDELITY_BLOCKED = true.  Not performance-optimized.
    PARAMETER_SEARCH = false.
    """
    sv_id = strategy_version_id or f"{STRATEGY_ID}@{VERSION}"
    return ParameterSet(
        parameter_set_id="kojo-structure-reclaim-v3-baseline",
        strategy_version_id=sv_id,
        schema_id="kojo-structure-reclaim-v3",
        values={
            "pivot_strength": 2,
            "retest_tolerance_atr": 0.5,
            "max_retest_wait_h1_bars": 24,
            "max_confirmation_wait_m15_bars": 16,
            "stop_buffer_type": "PRICE",
            "stop_buffer_value": 1.0,
        },
        provenance={
            "source": "RESEARCH_HYPOTHESIS_FROM_LIVE_OBSERVATION",
            "instrument": INSTRUMENT,
            "context_timeframe": TIMEFRAME_H1,
            "entry_timeframe": TIMEFRAME_M15,
            "source_fidelity_status": "SOURCE_FIDELITY_BLOCKED",
            "baseline_rationale": (
                "Same causal deterministic defaults as V1/V2. "
                "V3 implements opportunity retirement repair only. "
                "H1 confirmation semantics and target meaningfulness boundary remain "
                "SOURCE_RULE_REQUIRED. PARAMETER_SEARCH=false."
            ),
        },
    )
