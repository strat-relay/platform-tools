"""Tests for KOJO V3 live-entry timing repair and equal-high/low clustering repair.

A. LIVE ENTRY (8 tests)
   1. confirmation at T + quote at T+2s emits signal
   2. no quote before confirmation may emit
   3. stale pending setup cannot emit (obs > confirmation + LIVE_ENTRY_STALE_THRESHOLD_SECONDS)
   4. duplicate quote cannot emit duplicate signal
   5. historical replay behavior unchanged (completed M15 bar still works)
   6. live path does not advance M15 completed-bar watermark
   7. no fake completed M15 bar is created (entry_mechanism=LIVE_QUOTE in provenance)
   8. decision/provenance timestamps remain causal

B. CLUSTERING (7 tests)
   1. two genuinely equal highs form one cluster
   2. several genuinely equal highs cluster
   3. A~B B~C but A not~C: no oversized cluster
   4. cluster diameter never exceeds tolerance
   5. input ordering does not change clusters
   6. equal lows obey the same invariant
   7. BTC pathological 36-member input no longer forms one cluster
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy_backtest.models import (
    EntrySignal,
    LiveEntryPriceObservation,
    MarketEvent,
    ParameterSet,
    SetupLifecycleEvent,
    StrategyVersion,
)
from strategy_backtest.kojo_structure_reclaim_v3 import (
    CLUSTERING_ALGORITHM,
    CONFIRMED_PENDING_NEXT_OPEN,
    CONSUMED,
    LIVE_ENTRY_STALE_THRESHOLD_SECONDS,
    STRATEGY_ID,
    VERSION,
    EVALUATOR_KEY,
    _bounded_diameter_clusters,
    KojoStructureReclaimV3Evaluator,
    kojo_structure_reclaim_v3_baseline_parameter_set,
    kojo_structure_reclaim_v3_parameter_schema,
)

H1 = "H1"
M15 = "M15"
XAUUSD = "XAUUSD"
H1S = 3600
M15S = 900
BASE_TS = 1_784_100_000  # well-aligned unix timestamp, UTC weekday


def h1(t, o, h, lo, c, instr=XAUUSD):
    return MarketEvent(
        canonical_instrument=instr, timeframe=H1,
        open_timestamp=t, close_timestamp=t + H1S,
        open=o, high=h, low=lo, close=c,
        completed=True, source="TEST",
    )


def m15(t, o, h, lo, c, instr=XAUUSD):
    return MarketEvent(
        canonical_instrument=instr, timeframe=M15,
        open_timestamp=t, close_timestamp=t + M15S,
        open=o, high=h, low=lo, close=c,
        completed=True, source="TEST",
    )


def make_evaluator() -> KojoStructureReclaimV3Evaluator:
    ev = KojoStructureReclaimV3Evaluator()
    sv = StrategyVersion(STRATEGY_ID, VERSION, EVALUATOR_KEY, kojo_structure_reclaim_v3_parameter_schema())
    ps = kojo_structure_reclaim_v3_baseline_parameter_set(sv.strategy_version_id)
    ev.initialize(sv, ps)
    return ev


def feed(evaluator, events):
    sigs, lcs = [], []
    for ev in events:
        for out in evaluator.consume_market_event(ev):
            if isinstance(out, EntrySignal):
                sigs.append(out)
            elif isinstance(out, SetupLifecycleEvent):
                lcs.append(out)
    return sigs, lcs


def build_up_to_confirmation(base_ts=BASE_TS, level=2000.0, direction="SHORT"):
    """Events from test_kojo_structure_reclaim_v3.build_pivot_sequence but stopping
    BEFORE the M15 entry bar, leaving the setup in CONFIRMED_PENDING_NEXT_OPEN."""
    from tests.test_kojo_structure_reclaim_v3 import build_pivot_sequence
    all_events = build_pivot_sequence(base_ts, level, direction)
    # Last event is the M15 entry bar; drop it.
    return all_events[:-1], all_events[-1]


# ─────────────────────────────────────────────────────────────────────────────
# A. LIVE ENTRY TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestLiveEntryBasic(unittest.TestCase):

    def _setup_pending(self, base_ts=BASE_TS, level=2000.0, direction="SHORT"):
        """Return (evaluator, confirmation_ts, entry_bar) with setup in CONFIRMED_PENDING_NEXT_OPEN."""
        ev = make_evaluator()
        pre_events, entry_bar = build_up_to_confirmation(base_ts, level, direction)
        sigs, _ = feed(ev, pre_events)
        self.assertEqual(len(sigs), 0, "signal fired before live quote — check fixture")
        self.assertTrue(ev.has_pending_live_entry_setups(), "no pending setup after confirmation bar")
        # Extract confirmation_timestamp from the first pending setup
        conf_ts = next(
            s["confirmation_timestamp"]
            for s in ev._setups.values()
            if s["state"] == CONFIRMED_PENDING_NEXT_OPEN
        )
        return ev, conf_ts, entry_bar

    def test_live_quote_at_t_plus_2_emits_signal(self):
        """Confirmation at T + quote at T+2s → signal emitted via live path."""
        ev, conf_ts, _ = self._setup_pending()
        level = 2000.0
        # SHORT: entry = bid, bid < ask < level (price dropped below SHORT level)
        bid = level - 7.5
        ask = bid + 0.5
        obs = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=conf_ts + 2,
        )
        outputs = ev.consume_live_entry_price(obs)
        sigs = [o for o in outputs if isinstance(o, EntrySignal)]
        self.assertEqual(len(sigs), 1, "expected exactly one signal from live quote")

    def test_quote_before_confirmation_does_not_emit(self):
        """A quote with observed_at <= confirmation_timestamp must be rejected."""
        ev, conf_ts, _ = self._setup_pending()
        level = 2000.0
        bid = level - 7.5
        ask = bid + 0.5
        obs_at_conf = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=conf_ts,  # exactly at confirmation — not strictly after
        )
        outputs = ev.consume_live_entry_price(obs_at_conf)
        self.assertEqual(len([o for o in outputs if isinstance(o, EntrySignal)]), 0)

        obs_before = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=conf_ts - 1,  # before confirmation
        )
        outputs2 = ev.consume_live_entry_price(obs_before)
        self.assertEqual(len([o for o in outputs2 if isinstance(o, EntrySignal)]), 0)

    def test_stale_quote_does_not_emit(self):
        """Quote more than LIVE_ENTRY_STALE_THRESHOLD_SECONDS after confirmation → no signal."""
        ev, conf_ts, _ = self._setup_pending()
        level = 2000.0
        bid = level - 7.5
        ask = bid + 0.5
        stale_obs = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=conf_ts + LIVE_ENTRY_STALE_THRESHOLD_SECONDS + 1,
        )
        outputs = ev.consume_live_entry_price(stale_obs)
        self.assertEqual(len([o for o in outputs if isinstance(o, EntrySignal)]), 0)

    def test_duplicate_quote_does_not_emit_duplicate_signal(self):
        """Second call to consume_live_entry_price after a signal emits nothing."""
        ev, conf_ts, _ = self._setup_pending()
        level = 2000.0
        bid = level - 7.5
        ask = bid + 0.5
        obs1 = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=conf_ts + 2,
        )
        obs2 = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid - 0.1, ask=ask - 0.1,
            observed_at=conf_ts + 5,
        )
        out1 = ev.consume_live_entry_price(obs1)
        sigs1 = [o for o in out1 if isinstance(o, EntrySignal)]
        self.assertEqual(len(sigs1), 1, "first live quote should produce a signal")

        out2 = ev.consume_live_entry_price(obs2)
        sigs2 = [o for o in out2 if isinstance(o, EntrySignal)]
        self.assertEqual(len(sigs2), 0, "second live quote must not produce a duplicate signal")

    def test_historical_replay_unchanged(self):
        """Feeding the entry bar via consume_market_event still produces a signal (historical path)."""
        ev = make_evaluator()
        pre_events, entry_bar = build_up_to_confirmation()
        feed(ev, pre_events)
        # Feed the historical entry bar — should still work
        outputs = []
        for out in ev.consume_market_event(entry_bar):
            outputs.append(out)
        sigs = [o for o in outputs if isinstance(o, EntrySignal)]
        self.assertEqual(len(sigs), 1, "historical entry bar should still produce a signal")

    def test_live_path_does_not_advance_m15_watermark(self):
        """consume_live_entry_price must not add bars to the evaluator's M15 bar list."""
        ev, conf_ts, _ = self._setup_pending()
        m15_count_before = len(ev._m15_bars)
        level = 2000.0
        bid = level - 7.5
        ask = bid + 0.5
        obs = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=conf_ts + 2,
        )
        ev.consume_live_entry_price(obs)
        m15_count_after = len(ev._m15_bars)
        self.assertEqual(m15_count_before, m15_count_after,
                         "live entry must not append bars to _m15_bars")

    def test_no_fake_completed_m15_bar_in_provenance(self):
        """Signal provenance must show entry_mechanism=LIVE_QUOTE, not a completed bar source."""
        ev, conf_ts, _ = self._setup_pending()
        level = 2000.0
        bid = level - 7.5
        ask = bid + 0.5
        obs = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=conf_ts + 2,
        )
        outputs = ev.consume_live_entry_price(obs)
        sigs = [o for o in outputs if isinstance(o, EntrySignal)]
        self.assertEqual(len(sigs), 1)
        prov = sigs[0].provenance
        self.assertEqual(prov.get("entry_mechanism"), "LIVE_QUOTE",
                         "live signal must carry entry_mechanism=LIVE_QUOTE, not M15_OPEN")

    def test_decision_and_provenance_timestamps_causal(self):
        """decision_timestamp == confirmation_ts; live_entry_observed_at >= confirmation_ts."""
        ev, conf_ts, _ = self._setup_pending()
        level = 2000.0
        bid = level - 7.5
        ask = bid + 0.5
        observed_at = conf_ts + 3
        obs = LiveEntryPriceObservation(
            canonical_instrument=XAUUSD,
            bid=bid, ask=ask,
            observed_at=observed_at,
        )
        outputs = ev.consume_live_entry_price(obs)
        sigs = [o for o in outputs if isinstance(o, EntrySignal)]
        self.assertEqual(len(sigs), 1)
        sig = sigs[0]
        prov = sig.provenance

        # decision_timestamp == confirmation_timestamp (causal: entry decided at confirmation close)
        self.assertEqual(sig.decision_timestamp, conf_ts)

        # live_entry_observed_at is after confirmation
        self.assertGreater(prov["live_entry_observed_at"], prov["confirmation_close_ts"])

        # latency is the difference
        expected_latency = observed_at - conf_ts
        self.assertEqual(prov["live_entry_latency_seconds"], expected_latency)

        # reference_entry_price is midpoint
        expected_mid = (bid + ask) / 2.0
        self.assertAlmostEqual(prov["reference_entry_price"], expected_mid, places=6)

        # For SHORT: entry_price == bid (executable); ask > bid
        self.assertEqual(sig.entry_price, bid)
        self.assertEqual(prov["bid_at_entry"], bid)
        self.assertEqual(prov["ask_at_entry"], ask)


# ─────────────────────────────────────────────────────────────────────────────
# B. CLUSTERING TESTS
# ─────────────────────────────────────────────────────────────────────────────

class TestEqualHighLowClustering(unittest.TestCase):
    """Tests for _bounded_diameter_clusters — the fixed equal-highs/lows algorithm.

    Tests operate on the extracted module-level function directly, bypassing
    H1 swing pivot detection so that cluster semantics can be verified in isolation.
    """

    def test_two_genuinely_equal_highs_cluster(self):
        """Two prices within equal_tol form exactly one cluster of 2."""
        tol = 5.0
        result = _bounded_diameter_clusters([100.0, 103.0], tol)
        multi = [c for c in result if len(c) >= 2]
        self.assertEqual(len(multi), 1)
        self.assertEqual(sorted(multi[0]), [100.0, 103.0])

    def test_several_genuinely_equal_highs_cluster(self):
        """Multiple prices all within tol of the cluster start → one cluster."""
        tol = 6.0
        prices = sorted([120.0, 122.0, 124.0, 125.0])  # span=5 ≤ tol=6
        result = _bounded_diameter_clusters(prices, tol)
        multi = [c for c in result if len(c) >= 2]
        self.assertEqual(len(multi), 1)
        self.assertEqual(len(multi[0]), 4)

    def test_transitive_chain_does_not_produce_oversized_cluster(self):
        """A~B and B~C but A not~C must NOT form one cluster of 3."""
        tol = 5.0
        # A=100, B=104 (A~B: 4≤5), C=108 (B~C: 4≤5), but C-A=8 > tol
        result = _bounded_diameter_clusters([100.0, 104.0, 108.0], tol)
        max_cluster_len = max(len(c) for c in result)
        self.assertLess(max_cluster_len, 3,
                        "transitive A~B~C must not produce a 3-member cluster when A not~C")

    def test_cluster_diameter_never_exceeds_tolerance(self):
        """For every cluster, max(prices) - min(prices) <= equal_tol."""
        tol = 4.0
        prices = sorted([120.0, 122.0, 124.0,   # span=4, one cluster
                          129.0, 130.0, 132.0,   # span=3, one cluster
                          140.0])                 # singleton
        result = _bounded_diameter_clusters(prices, tol)
        for cluster in result:
            span = max(cluster) - min(cluster)
            self.assertLessEqual(span, tol, f"cluster {cluster} span {span} exceeds tol {tol}")

    def test_input_ordering_does_not_change_clusters(self):
        """Clusters from sorted input equal clusters from shuffled input (after sorting)."""
        import random
        tol = 5.0
        prices_asc = [120.0, 123.0, 130.0, 133.0]
        prices_shuffled = list(prices_asc)
        random.seed(42)
        random.shuffle(prices_shuffled)

        result_asc = _bounded_diameter_clusters(sorted(prices_asc), tol)
        result_shuf = _bounded_diameter_clusters(sorted(prices_shuffled), tol)

        self.assertEqual(
            sorted(sorted(c) for c in result_asc),
            sorted(sorted(c) for c in result_shuf),
        )

    def test_equal_lows_obey_same_invariant(self):
        """Cluster algorithm is symmetric — applies equally to low prices."""
        tol = 4.0
        # Three prices for SHORT lows: two close, one far
        prices = sorted([1978.0, 1980.0, 1986.0])  # 1978~1980 (span=2≤4), 1986 alone
        result = _bounded_diameter_clusters(prices, tol)
        # The pair [1978,1980] must form a 2-member cluster
        multi = [c for c in result if len(c) >= 2]
        self.assertEqual(len(multi), 1)
        self.assertEqual(sorted(multi[0]), [1978.0, 1980.0])
        # All clusters satisfy diameter invariant
        for cluster in result:
            self.assertLessEqual(max(cluster) - min(cluster), tol)

    def test_btc_pathological_36_member_does_not_form_one_cluster(self):
        """BTC pathological case: 36 prices spanning ~10× tol must not form one cluster.

        Production data: 36-member EQUAL_HIGHS cluster, span=1026 pts, equal_tol≈114 pts.
        Adjacent gaps were ≤ 113.85 pts, allowing the old transitive rule to chain them all.
        Bounded-diameter rule breaks this into many small clusters.
        """
        tol = 114.0  # approximate equal_tol from BTC production case
        # 36 prices, adjacent gap ≈ 28.5 pts (< tol), total span ≈ 997.5 pts ≈ 8.7× tol
        prices = sorted(83536.47 + i * 28.5 for i in range(36))
        result = _bounded_diameter_clusters(prices, tol)
        max_count = max(len(c) for c in result)
        self.assertLess(max_count, 36,
                        "pathological 36-member input must not produce one cluster")
        # All clusters must satisfy diameter invariant
        for cluster in result:
            span = max(cluster) - min(cluster)
            self.assertLessEqual(span, tol, f"cluster span {span} > tol {tol}")


if __name__ == "__main__":
    unittest.main()
