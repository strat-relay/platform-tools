"""Kojo Structure Reclaim V3 — targeted repair of V2 semantic defects.

SOURCE_FIDELITY_BLOCKED = true

This evaluator implements only the source-supported repairs identified in the
V2 semantic fidelity audit.  Two findings remain unresolved and are scaffolded
but NOT implemented:

  H1_POST_PULLBACK_CONFIRMATION_EVIDENCE:
    SOURCE_RULE_REQUIRED — no deterministic source-supported definition exists yet
    for what H1 candle/pattern/action constitutes a post-pullback H1 confirmation.
    The evaluator exposes the scaffold field but leaves it unresolved.

  TARGET_MEANINGFULNESS_BOUNDARY:
    SOURCE_RULE_REQUIRED — no source-supported minimum target distance rule exists.
    Near-coincident external targets are classified in diagnostics but not rejected.

V3 STATUS FLAGS:
  SOURCE_FIDELITY_BLOCKED = true
  READY_FOR_DISCOVERY     = false
  READY_FOR_VALIDATION    = false
  READY_FOR_SHADOW_SIGNALS = false
  READY_FOR_EXECUTION     = false

IMPLEMENTED IN V3:

  REPAIR 1 — OPPORTUNITY RETIREMENT POLICY

    V2 defect: _consumed_level_keys grew on ALL terminal states (CONSUMED,
    INVALIDATED, EXPIRED), permanently blocking new episodes on structural levels
    whose only prior episode was a failed retest (INVALIDATED) or timeout (EXPIRED).
    Source evidence item 4: "If an original entry is missed, a NEW simple pullback
    can create a later valid opportunity."

    V3 correction: explicit separation of two identities:

      _consumed_opportunity_keys — grows ONLY on CONSUMED.
        Permanently retires the economic opportunity. Same structural level + direction
        can never re-fire once a signal was produced and consumed.

      _terminal_episode_ids — grows on ALL terminal states.
        Prevents a specific terminated episode from reactivating. Does NOT prevent a
        new, structurally distinct episode on the same level.

    New-causal-break guard: a new episode on a structural level is only created when
    the current H1 bar is the FIRST bar to close on the break side of the level (i.e.,
    the previous H1 bar's close was on the non-break side). This prevents stale
    continuation bars from creating duplicate episodes after an INVALIDATED or EXPIRED
    episode without requiring an arbitrary time cooldown.

SCAFFOLDED BUT NOT IMPLEMENTED IN V3:

  SCAFFOLD 2 — H1 POST-PULLBACK CONFIRMATION EVIDENCE

    V2 defect: h1_confirmation_evidence was permanently set to the break bar at setup
    creation time and never updated. The DUAL_TF_BASELINE_ENFORCED=true assertion was
    therefore semantically mislabeled — it counted H1 structural context as H1
    confirmation.

    V3 adds three distinct fields:
      h1_context_evidence     — the original H1 break bar (always available)
      h1_post_pullback_confirmation_evidence — placeholder, value = SOURCE_RULE_REQUIRED
      m15_confirmation_evidence — unchanged from V2

    The DUAL_TF_BASELINE_ENFORCED assertion is removed from V3 diagnostics.

  SCAFFOLD 3 — TARGET MEANINGFULNESS DIAGNOSTICS

    V2 defect: 9 of 68 accepted signals had TP1 essentially coincident with entry
    price. No minimum meaningful distance rule exists in V2.

    V3 adds tp1_diagnostics to every accepted signal's provenance:
      target_distance              — abs(tp1 - entry)
      spread_at_decision_bar       — None (not available in MarketEvent; requires enrichment)
      episode_envelope_width       — abs(envelope_high - envelope_low)
      stop_distance                — abs(entry - stop)
      planned_r                    — target_distance / stop_distance
      target_distance_over_spread  — None (requires spread data)
      target_distance_over_envelope_width
      target_distance_over_stop_distance
      near_coincident_class        — SOURCE_RULE_REQUIRED (boundary pending source evidence)

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

SOURCE_FIDELITY_BLOCKED = True
READY_FOR_DISCOVERY = False
READY_FOR_VALIDATION = False
READY_FOR_SHADOW_SIGNALS = False
READY_FOR_EXECUTION = False

SOURCE_FIDELITY_BLOCKED_REASONS = (
    "H1_POST_PULLBACK_CONFIRMATION_EVIDENCE: SOURCE_RULE_REQUIRED — "
    "no deterministic source-supported definition for post-pullback H1 confirmation",
    "TARGET_MEANINGFULNESS_BOUNDARY: SOURCE_RULE_REQUIRED — "
    "near-coincident external target threshold not yet defined by source evidence",
)

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

# ─── near-coincident classification ───────────────────────────────────────────

TP1_NEAR_COINCIDENT = "EXTERNAL_BUT_NEAR_COINCIDENT_WITH_ENTRY"
TP1_STANDARD = "EXTERNAL_STANDARD"
TP1_NEAR_COINCIDENT_BOUNDARY = "SOURCE_RULE_REQUIRED"

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


# ─── evaluator ─────────────────────────────────────────────────────────────────

class KojoStructureReclaimV3Evaluator:
    """Deterministic, causal evaluator for KOJO_STRUCTURE_RECLAIM_V3.

    SOURCE_FIDELITY_BLOCKED = true.
    PRODUCTION_ELIGIBLE = false.
    BROKER_WRITES = 0.

    V3 changes vs V2:
      1. Opportunity retirement: only CONSUMED permanently retires (level_id, direction).
         INVALIDATED/EXPIRED terminate their specific episode but do not block future
         causal episodes on the same structural level.
      2. H1 evidence scaffold: h1_context_evidence (break bar) and
         h1_post_pullback_confirmation_evidence (SOURCE_RULE_REQUIRED) are separate fields.
         DUAL_TF_BASELINE_ENFORCED assertion removed.
      3. Target diagnostics: tp1_diagnostics block in every signal provenance.
         near_coincident_class = SOURCE_RULE_REQUIRED (boundary deferred).
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
            # V3 SCAFFOLD: separate context from (unresolved) post-pullback H1 confirmation
            "h1_context_evidence": _event_evidence(break_event),          # break bar; always available
            "h1_post_pullback_confirmation_evidence": None,                # SOURCE_RULE_REQUIRED
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
            self._reject("NO_EXTERNAL_STRUCTURAL_OBJECTIVE")
            self._terminate_setup(setup, CONSUMED)
            outputs.extend(
                self._emit_lifecycle(setup, CONSUMED, event, reason="NO_EXTERNAL_STRUCTURAL_OBJECTIVE")
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
            # V3 SCAFFOLD: three distinct evidence fields
            "h1_context_evidence": setup["h1_context_evidence"],
            "h1_post_pullback_confirmation_evidence": "SOURCE_RULE_REQUIRED",
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
            "tp1_class": "EXTERNAL",
            # V3 status — never production-eligible while SOURCE_FIDELITY_BLOCKED
            "v3_status": "SOURCE_FIDELITY_BLOCKED",
            "production_eligible": False,
            # dual_timeframe_confirmed removed; see h1_post_pullback_confirmation_evidence
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
        """External-only target selection with V3 diagnostic enrichment.

        Identical selection logic to V2.  Adds tp1_diagnostics block to provenance.
        near_coincident_class = SOURCE_RULE_REQUIRED — definitional boundary is pending
        source evidence; not a rejection criterion.
        """
        current_close_ts = self._h1_bars[-1]["time"] + H1_SECONDS if self._h1_bars else 0
        pivot_strength = int(self._values["pivot_strength"])
        all_levels = _confirmed_swings(self._h1_bars, current_close_ts, pivot_strength)

        episode_start_ts = setup["episode_start_ts"]
        envelope_high = setup["episode_envelope_high"]
        envelope_low = setup["episode_envelope_low"]

        external_candidates: list[dict[str, Any]] = []

        for level in all_levels:
            if level["confirmed_at_close_ts"] >= episode_start_ts:
                continue
            price = level["price"]
            if direction == "LONG":
                if level["type"] != "RESISTANCE":
                    continue
                if price <= entry_price:
                    continue
                if price <= envelope_high:
                    continue
            else:
                if level["type"] != "SUPPORT":
                    continue
                if price >= entry_price:
                    continue
                if price >= envelope_low:
                    continue
            external_candidates.append(level)

        if direction == "LONG":
            external_candidates.sort(key=lambda l: l["price"])
        else:
            external_candidates.sort(key=lambda l: l["price"], reverse=True)

        if not external_candidates:
            return None, None, None

        tp1_level = external_candidates[0]
        tp1_price = tp1_level["price"]
        effective_stop = stop_price if stop_price is not None else setup.get("final_stop")
        risk = abs(entry_price - effective_stop) if effective_stop is not None else 0.0
        target_distance = abs(tp1_price - entry_price)
        planned_r = target_distance / risk if risk > 0 else None
        envelope_width = abs(envelope_high - envelope_low)
        stop_distance = risk

        # V3 SCAFFOLD: target diagnostics — all ratio fields exposed for source-evidence
        # analysis.  near_coincident_class boundary is SOURCE_RULE_REQUIRED.
        tp1_diagnostics = {
            "target_structural_id": tp1_level["level_id"],
            "target_h1_open_timestamp": tp1_level["h1_open_timestamp"],
            "entry_price": entry_price,
            "target_distance": target_distance,
            "spread_at_decision_bar": None,          # requires MarketEvent spread enrichment
            "tick_size": None,                        # requires contract metadata enrichment
            "episode_envelope_width": envelope_width,
            "stop_distance": stop_distance,
            "planned_r": planned_r,
            "target_distance_over_spread": None,      # requires spread data
            "target_distance_over_envelope_width": (
                target_distance / envelope_width if envelope_width > 0 else None
            ),
            "target_distance_over_stop_distance": (
                target_distance / stop_distance if stop_distance > 0 else None
            ),
            # Boundary for near-coincident classification not yet defined by source evidence.
            # All external targets receive SOURCE_RULE_REQUIRED until the boundary is specified.
            "near_coincident_class": TP1_NEAR_COINCIDENT_BOUNDARY,
            "near_coincident_class_note": (
                f"Classification into '{TP1_NEAR_COINCIDENT}' or '{TP1_STANDARD}' "
                "requires source-defined distance criterion. See V3 audit findings."
            ),
        }

        tp1_prov = {
            "level_id": tp1_level["level_id"],
            "level_type": tp1_level["type"],
            "level_price": tp1_price,
            "level_h1_open_timestamp": tp1_level["h1_open_timestamp"],
            "level_confirmed_at_close_ts": tp1_level["confirmed_at_close_ts"],
            "episode_start_ts": episode_start_ts,
            "predates_episode": True,
            "episode_envelope_high": envelope_high,
            "episode_envelope_low": envelope_low,
            "outside_pullback_envelope": True,
            "tp1_class": "EXTERNAL",
            "distance_from_entry": target_distance,
            "planned_r": planned_r,
            "tp1_diagnostics": tp1_diagnostics,
        }

        tp2_level = external_candidates[1] if len(external_candidates) > 1 else None
        tp2_price = tp2_level["price"] if tp2_level else None

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
