import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from execution.models import ExecutionIntent
from execution.demo import (DEMO_CONTEXT, MAPPINGS, account_is_authorized, real_account_is_authorized,
                            virtual_size)
from execution.demo_broker import BrokerSubmissionRejected, DemoExecutionAdapter
from execution.storage import ExecutionStore
from live_execution_consumer import (config, safety_audit, validate_intent, create_intents, post_live_signals,
                                     _smoke_candidate, run_real_smoke_test, real_monitor_snapshot,
                                     _smoke_envelope_unchanged, _smoke_post_confirmation_preflight,
                                     _smoke_response_metrics, canonical_market_request,
                                     canonical_request_fingerprint,
                                     market_execution_candidate, market_execution_sizing,
                                     execution_state_consistency,
                                     execution_safety_audit, _execution_signal_rows)
from live_execution_consumer import process_intents


def signal(classification=None):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    return {"signal_id": "SIG-1", "strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "strategy_version": "V1",
            "strategy_instance_id": "phase6", "economic_position_id": "ep-1", "entry_opportunity_id": "op-1",
            "canonical_symbol": "XAUUSD", "broker_symbol_hint": "XAUUSDm", "direction": "LONG",
            "signal_timestamp": ts, "created_at": ts, "decision_time": ts,
            "signal_emitted_at": ts}


def sizing(decision="EXECUTABLE"):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    return {"sizing_decision_id": "SIZE-1", "signal_id": "SIG-1", "portfolio_id": "p", "account_id": "a",
            "decision": decision, "entry": 100.0, "stop": 99.0, "target": 101.0, "rounded_volume": 0.01,
            "desired_risk_amount": 2.0, "estimated_loss_at_stop": 1.0, "desired_risk_fraction": .01,
            "account_snapshot_id": "snap-1", "created_at": ts}


class ExecutionConsumerTests(unittest.TestCase):
    def _intent_fixture(self, *, age_seconds=0.1):
        s = signal()
        created = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
        s["signal_timestamp"] = created
        i = ExecutionIntent.from_records(s, sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        i["intent_created_at"] = created
        i["execution_intent_created_at"] = created
        i["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=5-age_seconds)).isoformat()
        return i

    def test_fresh_intent_reaches_age_gate_without_bridge_delay(self):
        class Adapter:
            def account_snapshot(self, _): return {"account_context_id": "a", "equity": 200, "free_margin": 200}
            def symbol_metadata(self, _): return {"volume_min": .01, "volume_max": 10, "volume_step": .01, "tick_size": .01}
            def quote(self, _): return {"bid": 100.1, "ask": 100.2, "freshness_state": "FRESH", "quote_age_ms": 0}
            def open_positions(self, _): return []
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(Path(td)); store.append("execution_intents", self._intent_fixture())
            started = datetime.now(timezone.utc)
            process_intents(store, {"mode": "DRY_RUN", "intent_max_age": 5, "signal_max_age": 120,
                                    "quote_max_age_ms": 2000, "position_conflict_policy": "REJECT_SAME_SYMBOL_DIRECTION",
                                    "live_armed": False}, Adapter())
            elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
            decision = store.rows("execution_decisions")[0]
            self.assertNotEqual(decision["reason"], "EXECUTION_INTENT_EXPIRED")
            self.assertLess(elapsed_ms, 1000)

    def test_expired_intent_is_rejected_before_bridge_reads(self):
        class NoReads:
            def __getattr__(self, name):
                raise AssertionError(f"bridge read should not occur: {name}")
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(Path(td)); store.append("execution_intents", self._intent_fixture(age_seconds=10))
            process_intents(store, {"mode": "REAL_EXECUTION", "intent_max_age": 5, "signal_max_age": 120}, NoReads())
            decision = store.rows("execution_decisions")[0]
            self.assertEqual(decision["reason"], "EXECUTION_INTENT_EXPIRED")
            self.assertEqual(decision["details"]["age_at_consumer_ms"] > 5000, True)

    def test_duplicate_intent_is_not_processed_twice(self):
        class Adapter:
            def account_snapshot(self, _): return {"account_context_id": "a", "equity": 200, "free_margin": 200}
            def symbol_metadata(self, _): return {"volume_min": .01, "volume_max": 10, "volume_step": .01, "tick_size": .01}
            def quote(self, _): return {"bid": 100.1, "ask": 100.2, "freshness_state": "FRESH", "quote_age_ms": 0}
            def open_positions(self, _): return []
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(Path(td)); store.append("execution_intents", self._intent_fixture())
            cfg = {"mode": "DRY_RUN", "intent_max_age": 5, "signal_max_age": 120,
                   "quote_max_age_ms": 2000, "position_conflict_policy": "REJECT_SAME_SYMBOL_DIRECTION", "live_armed": False}
            process_intents(store, cfg, Adapter()); process_intents(store, cfg, Adapter())
            self.assertEqual(len(store.rows("execution_decisions")), 1)

    def test_canonical_request_uses_executable_side_and_explicit_fields(self):
        metadata = {"filling_mode": 3, "raw": {"filling_mode": 3}}
        c = {"symbol": "XAUUSDm", "side": "BUY", "volume": .01, "ask": 4300.1,
             "bid": 4299.8, "stop": 4299.5, "target": 4300.5}
        r = canonical_market_request(c, metadata)
        self.assertEqual(r["price"], 4300.1)
        self.assertEqual(r["type"], 0)
        self.assertEqual(r["type_filling"], 1)
        self.assertEqual(r["deviation"], 50)
        self.assertEqual(r["type_time"], 0)
        short = dict(c, side="SELL", bid=4299.8, ask=4300.1)
        self.assertEqual(canonical_market_request(short, metadata)["price"], 4299.8)

    def test_canonical_fingerprint_changes_on_execution_field_change(self):
        base = {"schema_version": 1, "action": 1, "magic": 0, "symbol": "XAUUSDm", "volume": .01,
                "price": 4300.1, "sl": 4299.5, "tp": 4300.5, "deviation": 50,
                "type": 0, "type_filling": 1, "type_time": 0, "expiration": 0,
                "comment": "CANONICAL_ORDER_CHECK"}
        self.assertNotEqual(canonical_request_fingerprint(base),
                            canonical_request_fingerprint(dict(base, tp=4300.6)))

    def test_canonical_smoke_submission_includes_smoke_id_header(self):
        request = {"schema_version": 1, "action": 1, "magic": 0, "symbol": "XAUUSDm",
                   "volume": .01, "price": 4300.1, "sl": 4299.5, "tp": 4300.5,
                   "deviation": 50, "type": 0, "type_filling": 1, "type_time": 0,
                   "expiration": 0, "comment": "REAL_SMOKE_TEST"}
        text = "schema_version=1;action=1;magic=0;symbol=XAUUSDm;volume=0.0100000000;price=4300.1000000000;sl=4299.5000000000;tp=4300.5000000000;deviation=50;type=0;type_filling=1;type_time=0;expiration=0;comment=REAL_SMOKE_TEST"
        response = Mock()
        response.read.return_value = json.dumps({"result": {"content": [{"text": json.dumps({"ok": True, "retcode": 10009})}]}}).encode()
        with patch("execution.demo_broker.urlopen") as opened:
            opened.return_value.__enter__.return_value = response
            adapter = DemoExecutionAdapter("http://127.0.0.1:22348/mcp",
                                           mode="REAL_SMOKE_TEST",
                                           armed_context="SYNTHETIC_ACCOUNT@Exness-MT5Real27",
                                           verified_snapshot={"account_context_id": "SYNTHETIC_ACCOUNT@Exness-MT5Real27",
                                                              "raw": {"server": "Exness-MT5Real27", "type": 2}},
                                           transport_verified=True,
                                           smoke_test_id="SMOKE_test")
            adapter.submit_canonical_market_order(request=request, canonical_request_text=text,
                                                  request_fingerprint=canonical_request_fingerprint(request),
                                                  idempotency_key="SMOKE_test")
            sent_request = opened.call_args.args[0]
            self.assertEqual(sent_request.get_header("X-smoke-test-id"), "SMOKE_test")

    def test_market_entry_is_reference_only_and_fresh_side_is_canonical_price(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent["strategy_entry_price"] = 100.0
        intent["strategy_stop_price"] = 99.0
        intent["strategy_target_price"] = 102.0
        quote = {"bid": 100.4, "ask": 100.6}
        metadata = {"tick_size": 0.1, "tick_value": 1.0, "volume_min": .01, "volume_step": .01, "volume_max": 10.0,
                    "raw": {"filling_mode": 3}}
        sized = market_execution_sizing(intent, quote, metadata, 200.0)
        self.assertEqual(sized["execution_price"], 100.6)
        candidate = market_execution_candidate(intent, quote, sized["rounded_volume"])
        request = canonical_market_request(candidate, metadata)
        self.assertEqual(request["price"], 100.6)
        self.assertEqual(request["sl"], 99.0)
        self.assertEqual(intent["strategy_entry_price"], 100.0)

    def test_short_market_sizing_uses_bid_and_preserves_frozen_stop(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent.update(direction="SHORT", strategy_entry_price=100.0, strategy_stop_price=101.0, strategy_target_price=98.0)
        metadata = {"tick_size": 0.1, "tick_value": 1.0, "volume_min": .01, "volume_step": .01, "volume_max": 10.0}
        sized = market_execution_sizing(intent, {"bid": 99.4, "ask": 99.6}, metadata, 200.0)
        self.assertEqual(sized["execution_price"], 99.4)
        self.assertEqual(intent["strategy_stop_price"], 101.0)

    def test_market_sizing_below_minimum_fails_closed(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent.update(strategy_entry_price=100.0, strategy_stop_price=99.999, strategy_target_price=101.0)
        metadata = {"tick_size": 0.001, "tick_value": 1000.0, "volume_min": .01, "volume_step": .01, "volume_max": 10.0}
        result = market_execution_sizing(intent, {"bid": 100.0, "ask": 100.001}, metadata, 200.0)
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reason"], "BELOW_MINIMUM_VOLUME_FOR_RISK_BUDGET")

    def test_production_source_has_no_legacy_market_submit_call(self):
        source = Path(__file__).with_name("live_execution_consumer.py").read_text()
        production = source[source.index("def process_intents"):source.index("def safety_audit")]
        self.assertIn("submit_canonical_market_order", production)
        self.assertNotIn("submit_market_order(", production)

    def test_canonical_send_is_wire_gated_and_not_diagnostic(self):
        source = Path(__file__).parent.joinpath("ea", "MT5TradingBridge.mq5").read_text()
        self.assertIn("mt5_canonical_order_send", source)
        self.assertIn("if(!CANONICAL_ORDER_SEND_ENABLED)", source)
        diagnostic = Path(__file__).with_name("live_execution_consumer.py").read_text()
        diag = diagnostic[diagnostic.index("def canonical_order_diagnostic"):diagnostic.index("def smoke_preflight")]
        self.assertNotIn('"name": "mt5_canonical_order_send"', diag)

    def test_canonical_order_send_gate_matches_current_ea_source(self):
        source = Path(__file__).parent.joinpath("ea", "MT5TradingBridge.mq5").read_text()
        self.assertIn("CANONICAL_ORDER_SEND_ENABLED = true", source)
        self.assertIn("CANONICAL_ORDERSEND_DISABLED", source)

    def test_ea_ordercheck_and_market_order_diagnostics_are_read_only_distinguishable(self):
        source = Path(__file__).parent.joinpath("ea", "MT5TradingBridge.mq5").read_text()
        self.assertIn('\\"request\\":{', source)
        self.assertIn('\\"trade_setup\\":{', source)
        self.assertIn('\\"mt5_order_send_attempted\\":true', source)
        # OrderCheck must expose request construction, but must not invoke a
        # broker-mutating CTrade method.
        ordercheck = source[source.index("string OrderCheckJson"):source.index("string MarketOrderJson")]
        self.assertIn("OrderCheck(request,check)", ordercheck)
        self.assertNotRegex(ordercheck, r"trade\.(Buy|Sell|OrderSend|Position)")

    def test_ea_geometry_stale_response_is_structured_before_ordersend(self):
        source = Path(__file__).parent.joinpath("ea", "MT5TradingBridge.mq5").read_text()
        market = source[source.index("string MarketOrderJson"):source.index("string PendingOrderJson")]
        self.assertIn('\\"EXECUTION_GEOMETRY_STALE\\"', market)
        stale = market[:market.index("trade.SetAsyncMode")]
        self.assertNotIn("trade.Buy", stale)
        self.assertNotIn("trade.Sell", stale)

    def test_canonical_send_has_final_live_tick_geometry_gate(self):
        source = Path(__file__).parent.joinpath("ea", "MT5TradingBridge.mq5").read_text()
        send = source[source.index("string CanonicalOrderSendWireJson"):source.index("string MarketOrderJson")]
        self.assertIn("request.sl<tick.bid", send)
        self.assertIn("request.tp>tick.ask", send)
        self.assertIn("request.sl>tick.ask", send)
        self.assertIn("request.tp<tick.bid", send)
        self.assertIn('\\"EXECUTION_GEOMETRY_STALE\\"', send)
        stale = send[:send.index("ResetLastError();")]
        self.assertNotIn("OrderSend(request,result)", stale)

    def test_execution_state_contradiction_is_not_resolved_by_preference(self):
        with tempfile.TemporaryDirectory() as td, patch("live_execution_consumer.ORCHESTRATION", Path(td) / "orchestration"), patch("live_execution_consumer.REAL_STATE", Path(td) / "execution" / "real_state.json"), patch("live_execution_consumer.RESUME_STATE", Path(td) / "execution" / "resume.json"):
            root = Path(td); (root / "orchestration").mkdir(); (root / "execution").mkdir()
            (root / "orchestration" / "manifest.json").write_text(json.dumps({"mode": "SHADOW", "live_execution_enabled": False}))
            (root / "orchestration" / "state.json").write_text(json.dumps({"live_execution_enabled": True, "live_execution_cutoff_timestamp": "2026-01-01T00:00:00Z"}))
            (root / "execution" / "real_state.json").write_text(json.dumps({"armed": True, "mode": "REAL_EXECUTION", "account_context_id": "real"}))
            audit = execution_state_consistency()
            self.assertFalse(audit["safe"])
            self.assertIn("MANIFEST_ORCHESTRATION_LIVE_FLAG_MISMATCH", audit["reasons"])

    def test_real_intent_creation_is_blocked_by_contradiction(self):
        with tempfile.TemporaryDirectory() as td, patch("live_execution_consumer.execution_state_consistency", return_value={"safe": False, "reasons": ["EXECUTION_STATE_CONTRADICTION"], "resume": {}}):
            cfg = config(); cfg["mode"] = "REAL_EXECUTION"
            self.assertEqual(create_intents(ExecutionStore(Path(td)), cfg), 0)

    def test_resume_boundary_excludes_existing_and_gap_recovery_signals(self):
        state = {"live_execution_enabled": True, "live_execution_cutoff_timestamp": "2026-01-01T00:00:00Z"}
        rows = [{"signal_id": "old", "created_at": "2026-01-02T00:00:00Z", "signal_timestamp": "2026-01-02T00:00:00Z"},
                {"signal_id": "new", "created_at": "2026-01-03T00:00:00Z", "signal_timestamp": "2026-01-03T00:00:00Z"}]
        with patch("live_execution_consumer.execution_state_consistency", return_value={"safe": True, "resume": {"real_execution_resumed_at": "2026-01-02T12:00:00Z", "excluded_signal_ids": ["old"]}, "orchestration": state, "manifest": {}, "real": {}}), patch("live_execution_consumer.source_records", return_value=(rows, [], [])):
            candidates, _ = _execution_signal_rows()
            self.assertEqual([x["signal_id"] for x in candidates], ["new"])
    def test_execution_intent_is_deterministic(self):
        p = {"portfolio_id": "p"}; a = {"account_id": "a", "broker": "Exness"}
        s = signal(); z = sizing()
        a1 = ExecutionIntent.from_records(s, z, p, a)
        a2 = ExecutionIntent.from_records(s, z, p, a)
        self.assertEqual(a1.execution_intent_id, a2.execution_intent_id)
        self.assertEqual(a1.entry_condition_met_at, s["signal_timestamp"])
        self.assertIsNotNone(a1.expires_at)

    def test_reference_or_non_executable_cannot_create_intent(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(Path(td))
            with patch("live_execution_consumer.source_records", return_value=([signal()], [sizing("SKIPPED")], [{"signal_id": "SIG-1", "corrected_classification": "PRE_ORCHESTRATOR_REFERENCE"}])):
                self.assertEqual(create_intents(store, config()), 0)
            self.assertEqual(store.rows("execution_intents"), [])

    def test_executable_creates_one_idempotent_intent(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(Path(td)); s = sizing()
            platform = {"accounts": [{"account_id": "a", "broker": "E"}], "portfolios": [{"portfolio_id": "p"}]}
            with patch("live_execution_consumer.source_records", return_value=([signal()], [s], [{"signal_id": "SIG-1", "corrected_classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL"}])), patch("live_execution_consumer.load_config", return_value=platform):
                self.assertEqual(create_intents(store, config()), 1)
                self.assertEqual(create_intents(store, config()), 0)
            self.assertEqual(len(store.rows("execution_intents")), 1)

    def test_long_uses_ask_and_short_uses_bid(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        account = {"equity": 200.0, "free_margin": 200.0}
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        decision, reason, detail = validate_intent(intent, account, meta, {"bid": 99.8, "ask": 100.2}, [], config())
        self.assertEqual(decision, "DRY_RUN_ACCEPTED"); self.assertEqual(detail["executable_price"], 100.2)
        short = dict(intent, direction="SHORT", strategy_entry_price=100.0, strategy_stop_price=101.0, strategy_target_price=99.0)
        decision, reason, detail = validate_intent(short, account, meta, {"bid": 99.8, "ask": 100.2}, [], config())
        self.assertEqual(decision, "DRY_RUN_ACCEPTED"); self.assertEqual(detail["executable_price"], 99.8)

    def test_zero_equity_and_invalid_geometry(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        self.assertEqual(validate_intent(intent, {"equity": 0}, meta, {"bid": 99, "ask": 100}, [], config())[1], "REJECTED_ZERO_EQUITY")
        bad = dict(intent, strategy_target_price=99.0)
        self.assertEqual(validate_intent(bad, {"equity": 200}, meta, {"bid": 99, "ask": 100}, [], config())[1], "REJECTED_INVALID_GEOMETRY")

    def test_unset_drift_and_spread_policies_do_not_invent_rejection(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        self.assertEqual(validate_intent(intent, {"equity": 200}, meta, {"bid": 99.8, "ask": 100.2}, [], config())[0], "DRY_RUN_ACCEPTED")

    def test_audit_is_fail_closed_and_write_free(self):
        audit = safety_audit(); self.assertTrue(audit["pass"]); self.assertEqual(audit["broker_writes_performed"], 0)

    def test_demo_account_guard_requires_exact_context_and_server(self):
        good = {"account_context_id": DEMO_CONTEXT, "freshness_state": "FRESH",
                "raw": {"server": "MetaQuotes-Demo", "type": 0}}
        self.assertEqual(account_is_authorized(good), (True, "DEMO_ACCOUNT_VERIFIED"))
        wrong = {**good, "account_context_id": "1@Exness-Real"}
        self.assertEqual(account_is_authorized(wrong)[0], False)
        real = {**good, "raw": {"server": "Exness-Real", "type": 1}}
        self.assertEqual(account_is_authorized(real)[0], False)
        self.assertEqual(account_is_authorized({**good, "freshness_state": "AGING"})[1], "ACCOUNT_SNAPSHOT_STALE")

    def test_demo_and_real_account_validators_are_not_interchangeable(self):
        demo = {"account_context_id": DEMO_CONTEXT, "freshness_state": "FRESH",
                "raw": {"server": "MetaQuotes-Demo", "type": 0}}
        real = {"account_context_id": "SYNTHETIC_ACCOUNT@Exness-MT5Real27", "freshness_state": "FRESH",
                "raw": {"server": "Exness-MT5Real27", "type": 2}}
        self.assertFalse(real_account_is_authorized(demo, real["account_context_id"])[0])
        self.assertFalse(account_is_authorized(real)[0])
        self.assertTrue(real_account_is_authorized(real, real["account_context_id"])[0])
        wrong = {**real, "account_context_id": "wrong@Exness-MT5Real27"}
        self.assertFalse(real_account_is_authorized(wrong, real["account_context_id"])[0])

    def test_post_live_signals_require_new_creation_and_survive_restart_boundary(self):
        state = {"live_execution_enabled": True,
                 "live_execution_cutoff_timestamp": "2026-09-16T12:00:00+00:00",
                 "live_execution_cutoff_signal_ids": ["historical"]}
        rows = [{"signal_id": "historical", "created_at": "2026-09-16T12:01:00+00:00", "signal_timestamp": "2026-09-16T11:00:00+00:00"},
                {"signal_id": "old", "created_at": "2026-09-16T11:00:00+00:00", "signal_timestamp": "2026-09-16T11:00:00+00:00"},
                {"signal_id": "new", "created_at": "2026-09-16T12:01:00+00:00", "signal_timestamp": "2026-09-16T12:01:00+00:00"}]
        self.assertEqual(set(post_live_signals(rows, state)), {"new"})

    def test_demo_rejects_stale_quote_and_invalid_geometry(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        cfg = config(); cfg.update(mode="DEMO_EXECUTION", live_armed=False)
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        account = {"account_context_id": DEMO_CONTEXT, "freshness_state": "FRESH", "raw": {"server": "MetaQuotes-Demo", "type": 0}, "equity": 200, "free_margin": 200}
        self.assertEqual(validate_intent(intent, account, meta, {"bid": 99, "ask": 100, "freshness_state": "AGING"}, [], cfg)[1], "STALE_EXECUTION_QUOTE")
        bad = dict(intent, strategy_target_price=99.0)
        self.assertEqual(validate_intent(bad, account, meta, {"bid": 99, "ask": 100, "freshness_state": "FRESH"}, [], cfg)[1], "REJECTED_INVALID_GEOMETRY")

    def test_terminal_target_and_stop_are_rejected_before_submission(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        target = validate_intent(intent, {"equity": 200}, meta, {"bid": 101.0, "ask": 101.1}, [], config())
        self.assertEqual(target[1], "TARGET_ALREADY_REACHED")
        stop = validate_intent(intent, {"equity": 200}, meta, {"bid": 99.0, "ask": 99.1}, [], config())
        self.assertEqual(stop[1], "STOP_ALREADY_BREACHED")

    def test_explicit_terminal_setup_state_is_rejected(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent["v1_terminal_state"] = "TARGET_COMPLETED"
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        self.assertEqual(validate_intent(intent, {"equity": 200}, meta, {"bid": 99.8, "ask": 100.2}, [], config())[1], "SETUP_ALREADY_TERMINAL")

    def test_live_quote_freshness_is_required_and_age_is_reported(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        cfg = config(); cfg.update(mode="DEMO_EXECUTION", live_armed=False)
        account = {"equity": 200, "free_margin": 200, "account_context_id": DEMO_CONTEXT,
                   "freshness_state": "FRESH", "raw": {"server": "MetaQuotes-Demo", "type": 0}}
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        with patch("live_execution_consumer.load_state", return_value={"armed": True, "account_context_id": DEMO_CONTEXT}):
            stale = validate_intent(intent, account, meta, {"bid": 99.8, "ask": 100.2, "freshness_state": "AGING", "quote_age_ms": 3000}, [], cfg)
        self.assertEqual(stale[1], "STALE_EXECUTION_QUOTE")

    def test_long_and_short_terminal_side_are_directional(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        meta = {"volume_min": .01, "volume_max": 10, "volume_step": .01}
        short = dict(intent, direction="SHORT", strategy_entry_price=100.0, strategy_stop_price=101.0, strategy_target_price=99.0)
        self.assertEqual(validate_intent(short, {"equity": 200}, meta, {"bid": 98.9, "ask": 99.0}, [], config())[1], "TARGET_ALREADY_REACHED")
        self.assertEqual(validate_intent(short, {"equity": 200}, meta, {"bid": 100.9, "ask": 101.0}, [], config())[1], "STOP_ALREADY_BREACHED")

    def test_expired_intent_is_rejected(self):
        intent = ExecutionIntent.from_records(signal(), sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent["expires_at"] = "2020-01-01T00:00:00+00:00"
        self.assertEqual(validate_intent(intent, {"equity": 200}, {"volume_min": .01, "volume_max": 10, "volume_step": .01}, {"bid": 99, "ask": 100}, [], config())[1], "EXECUTION_INTENT_EXPIRED")

    def test_old_event_time_with_fresh_emission_passes_signal_age(self):
        now = datetime.now(timezone.utc)
        s = signal()
        s["signal_timestamp"] = (now - timedelta(minutes=10)).isoformat()
        s["signal_emitted_at"] = (now - timedelta(seconds=2)).isoformat()
        intent = ExecutionIntent.from_records(s, sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent["intent_created_at"] = (now - timedelta(seconds=1)).isoformat()
        intent["execution_intent_created_at"] = intent["intent_created_at"]
        intent["expires_at"] = (now + timedelta(seconds=4)).isoformat()
        result = validate_intent(intent, {"equity": 200}, {"volume_min": .01, "volume_max": 10, "volume_step": .01}, {"bid": 99.8, "ask": 100.2}, [], config())
        self.assertNotEqual(result[1], "EXECUTION_INTENT_EXPIRED")

    def test_emission_older_than_signal_limit_is_rejected(self):
        now = datetime.now(timezone.utc)
        s = signal(); s["signal_emitted_at"] = (now - timedelta(seconds=121)).isoformat()
        intent = ExecutionIntent.from_records(s, sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent["intent_created_at"] = (now - timedelta(seconds=1)).isoformat()
        intent["execution_intent_created_at"] = intent["intent_created_at"]
        intent["expires_at"] = (now + timedelta(seconds=4)).isoformat()
        result = validate_intent(intent, {"equity": 200}, {"volume_min": .01, "volume_max": 10, "volume_step": .01}, {"bid": 99.8, "ask": 100.2}, [], config())
        self.assertEqual(result[1], "EXECUTION_INTENT_EXPIRED")

    def test_fresh_signal_with_stale_intent_is_rejected(self):
        now = datetime.now(timezone.utc)
        s = signal(); s["signal_emitted_at"] = (now - timedelta(seconds=10)).isoformat()
        intent = ExecutionIntent.from_records(s, sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        intent["intent_created_at"] = (now - timedelta(seconds=6)).isoformat()
        intent["execution_intent_created_at"] = intent["intent_created_at"]
        intent["expires_at"] = (now + timedelta(seconds=4)).isoformat()
        result = validate_intent(intent, {"equity": 200}, {"volume_min": .01, "volume_max": 10, "volume_step": .01}, {"bid": 99.8, "ask": 100.2}, [], config())
        self.assertEqual(result[1], "EXECUTION_INTENT_EXPIRED")

    def test_missing_emission_timestamp_fails_closed(self):
        raw = {k: v for k, v in signal().items() if k != "signal_emitted_at"}
        intent = ExecutionIntent.from_records(raw, sizing(), {"portfolio_id": "p"}, {"account_id": "a", "broker": "E"}).to_dict()
        result = validate_intent(intent, {"equity": 200}, {"volume_min": .01, "volume_max": 10, "volume_step": .01}, {"bid": 99.8, "ask": 100.2}, [], config())
        self.assertEqual(result[1], "SIGNAL_EMISSION_TIME_UNAVAILABLE")

    def test_virtual_bankroll_does_not_use_broker_equity(self):
        meta = {"tick_size": 1.0, "tick_value": 1.0, "volume_min": .01, "volume_max": 100, "volume_step": .01}
        a = virtual_size(entry=100, stop=99, metadata=meta, virtual_equity=200)
        b = virtual_size(entry=100, stop=99, metadata=meta, virtual_equity=100000)
        self.assertEqual(a["desired_risk_amount"], 2.0)
        self.assertEqual(b["desired_risk_amount"], 1000.0)

    def test_virtual_bankroll_below_minimum_skips_without_rounding_up(self):
        meta = {"tick_size": 1.0, "tick_value": 1000.0, "volume_min": 0.01, "volume_max": 100, "volume_step": .01}
        result = virtual_size(entry=100, stop=99, metadata=meta, virtual_equity=200)
        self.assertEqual(result["decision"], "SKIP")
        self.assertEqual(result["reason"], "BELOW_MINIMUM_VOLUME_FOR_RISK_BUDGET")

    def test_btc_is_unavailable_and_cross_feed_mappings_are_not_auto_safe(self):
        self.assertEqual(MAPPINGS["BTCUSDm"]["status"], "UNAVAILABLE")
        self.assertTrue(all(v["status"] != "VERIFIED_DIRECT" for k, v in MAPPINGS.items() if k != "BTCUSDm"))

    def test_demo_adapter_rejects_research_endpoint(self):
        with self.assertRaisesRegex(RuntimeError, "RESEARCH_BRIDGE_NOT_ALLOWED"):
            DemoExecutionAdapter("http://127.0.0.1:22347/mcp", mode="DEMO_EXECUTION",
                                 armed_context=DEMO_CONTEXT,
                                 verified_snapshot={"account_context_id": DEMO_CONTEXT,
                                                    "raw": {"server": "MetaQuotes-Demo"}},
                                 transport_verified=True)

    def test_uncertain_submission_requires_reconciliation(self):
        from unittest.mock import patch
        with patch("execution.demo_broker.urlopen", side_effect=TimeoutError("timeout")):
            adapter = DemoExecutionAdapter("http://127.0.0.1:22348/mcp", mode="DEMO_EXECUTION",
                                           armed_context=DEMO_CONTEXT,
                                           verified_snapshot={"account_context_id": DEMO_CONTEXT,
                                                              "raw": {"server": "MetaQuotes-Demo"}},
                                           transport_verified=True)
            with self.assertRaisesRegex(RuntimeError, "RECONCILE_REQUIRED"):
                adapter.submit_market_order(broker_symbol="XAUUSD", side="BUY", volume=.01,
                                            stop_loss=99, take_profit=101, comment="test")

    def test_explicit_broker_rejection_is_not_position_reconciliation_failure(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self):
                return json.dumps({"result": {"isError": False, "content": [{
                    "type": "text", "text": json.dumps({
                        "ok": False, "sent": False, "retcode": 10016,
                        "retcode_description": "Invalid stops", "order": 0, "deal": 0
                    })
                }]}}).encode()
        adapter = DemoExecutionAdapter("http://127.0.0.1:22348/mcp", mode="DEMO_EXECUTION",
                                       armed_context=DEMO_CONTEXT,
                                       verified_snapshot={"account_context_id": DEMO_CONTEXT,
                                                          "raw": {"server": "MetaQuotes-Demo", "type": 0}},
                                       transport_verified=True)
        with patch("execution.demo_broker.urlopen", return_value=Response()):
            with self.assertRaises(BrokerSubmissionRejected) as raised:
                adapter.submit_market_order(broker_symbol="XAUUSD", side="BUY", volume=.01,
                                            stop_loss=99, take_profit=101, comment="test")
        self.assertEqual(raised.exception.response["retcode"], 10016)
        self.assertIn("Invalid stops", str(raised.exception))

    def test_pre_arm_signal_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as td:
            store = ExecutionStore(Path(td)); cfg = config(); cfg.update(mode="DEMO_EXECUTION")
            demo_state = {"armed": True, "armed_at": "2099-01-01T13:00:00+00:00", "virtual_equity": 200}
            platform = {"accounts": [], "portfolios": [], "demo_execution_account": {"account_id": "d", "broker": "MetaQuotes"},
                        "demo_execution_portfolio": {"portfolio_id": "p"}}
            with patch("live_execution_consumer.source_records", return_value=([signal()], [sizing()], [{"signal_id": "SIG-1", "corrected_classification": "PROSPECTIVE_ORCHESTRATOR_SIGNAL"}])), \
                 patch("live_execution_consumer.load_config", return_value=platform), patch("live_execution_consumer.load_state", return_value=demo_state):
                self.assertEqual(create_intents(store, cfg), 0)
            self.assertEqual(store.rows("execution_skips")[0]["reason"], "PRE_ARM_SIGNAL_REJECTED")

    def test_smoke_candidate_obeys_one_dollar_cap(self):
        metadata = {"trade_enabled": True, "tick_size": 1.0, "tick_value": 101.0,
                    "volume_min": .01, "volume_step": .01, "volume_max": 10,
                    "stops_level": 0, "freeze_level": 0,
                    "raw": {"digits": 2, "point": 1.0}}
        candidate, reason = _smoke_candidate("XAUUSDm", metadata, {"bid": 100.0, "ask": 101.0})
        self.assertIsNone(candidate)
        self.assertEqual(reason, "SMOKE_STOP_RISK_OVER_ONE_USD")

    def test_smoke_buy_geometry_accounts_for_spread(self):
        metadata = {"trade_enabled": True, "tick_size": .001, "tick_value": .1,
                    "volume_min": .01, "volume_step": .01, "volume_max": 200,
                    "stops_level": 0, "freeze_level": 0,
                    "raw": {"digits": 3, "point": .001}}
        candidate, reason = _smoke_candidate("XAUUSDm", metadata, {"bid": 4273.598, "ask": 4273.838})
        self.assertIsNone(reason)
        self.assertLess(candidate["stop"], 4273.598)
        self.assertGreater(candidate["target"], 4273.838)
        self.assertLessEqual(candidate["estimated_stop_loss"], 1.0)
        self.assertAlmostEqual(candidate["estimated_stop_loss"], .480, places=6)

    def test_smoke_buy_buffer_is_meaningfully_beyond_spread(self):
        metadata = {"trade_enabled": True, "tick_size": .001, "tick_value": .1,
                    "volume_min": .01, "volume_step": .01, "volume_max": 200,
                    "stops_level": 0, "freeze_level": 0,
                    "raw": {"digits": 3, "point": .001}}
        candidate, reason = _smoke_candidate("XAUUSDm", metadata, {"bid": 4273.598, "ask": 4273.838})
        self.assertIsNone(reason)
        self.assertAlmostEqual(candidate["breathing_buffer"], .240, places=6)
        self.assertAlmostEqual(candidate["bid"] - candidate["stop"], .240, places=6)
        self.assertTrue(candidate["sl_outside_spread"])
        self.assertTrue(candidate["breathing_room_valid"])

    def test_smoke_sell_geometry_is_symmetric(self):
        metadata = {"trade_enabled": True, "tick_size": .001, "tick_value": .1,
                    "volume_min": .01, "volume_step": .01, "volume_max": 200,
                    "stops_level": 0, "freeze_level": 0,
                    "raw": {"digits": 3, "point": .001}}
        candidate, reason = _smoke_candidate("XAUUSDm", metadata, {"bid": 4273.598, "ask": 4273.838}, "SELL")
        self.assertIsNone(reason)
        self.assertEqual(candidate["direction"], "SELL")
        self.assertGreater(candidate["stop"], candidate["ask"])
        self.assertLess(candidate["target"], candidate["bid"])
        self.assertTrue(candidate["breathing_room_valid"])

    def test_smoke_geometry_rejects_risk_over_cap_after_buffer(self):
        metadata = {"trade_enabled": True, "tick_size": .001, "tick_value": 10.0,
                    "volume_min": .01, "volume_step": .01, "volume_max": 200,
                    "stops_level": 0, "freeze_level": 0,
                    "raw": {"digits": 3, "point": .001}}
        candidate, reason = _smoke_candidate("XAUUSDm", metadata, {"bid": 4273.598, "ask": 4273.838})
        self.assertIsNone(candidate)
        self.assertEqual(reason, "SMOKE_STOP_RISK_OVER_ONE_USD")

    def test_smoke_confirmation_mismatch_has_zero_writes(self):
        preflight = {"account": {"raw": {"server": "Exness-MT5Real27", "type": 2}, "balance": 321.7},
                     "selected": {"symbol": "EURUSDm", "direction": "BUY", "volume": .01,
                                  "bid": 1.0, "ask": 1.1, "stop": 1.0, "target": 1.2,
                                  "estimated_stop_loss": .01, "quote": {"quote_age_ms": 0},
                                  "order_check": {"success": False, "retcode": 10016, "comment": "Invalid stops"}},
                     "endpoint": "http://127.0.0.1:22348/mcp"}
        with patch("live_execution_consumer.smoke_preflight", return_value=preflight), patch("builtins.input", return_value="yes"):
            self.assertEqual(run_real_smoke_test(False), 2)

    def test_preview_ordercheck_failure_does_not_block_confirmation(self):
        preflight = {"account": {"raw": {"server": "Exness-MT5Real27", "type": 2}, "balance": 321.7},
                     "selected": {"symbol": "XAUUSDm", "direction": "BUY", "side": "BUY", "volume": .01,
                                  "bid": 4314.334, "ask": 4314.594, "stop": 4314.072, "target": 4314.855,
                                  "estimated_stop_loss": .522, "quote": {"quote_age_ms": 0},
                                  "order_check": {"success": False, "retcode": 10016, "comment": "Invalid stops"}},
                     "endpoint": "http://127.0.0.1:22348/mcp"}
        with patch("live_execution_consumer.smoke_preflight", return_value=preflight), \
             patch("builtins.input", return_value="not-confirmed"), \
             patch("live_execution_consumer._print_smoke_preflight"):
            self.assertEqual(run_real_smoke_test(False), 2)

    def test_smoke_market_change_keeps_same_human_authorization_envelope(self):
        initial = {"symbol": "XAUUSDm", "direction": "BUY", "side": "BUY", "volume": 0.01,
                   "entry": 4286.626, "stop": 4286.104, "target": 4286.888,
                   "estimated_stop_loss": 0.522}
        refreshed = {"symbol": "XAUUSDm", "direction": "BUY", "side": "BUY", "volume": 0.01,
                     "entry": 4287.126, "stop": 4286.604, "target": 4287.388,
                     "estimated_stop_loss": 0.522}
        self.assertTrue(_smoke_envelope_unchanged(initial, refreshed))

    def test_post_confirmation_preflight_uses_provider_mcp_url(self):
        provider = Mock()
        provider.mcp_url = "http://127.0.0.1:22348/mcp"
        provider.account_snapshot.return_value = {
            "account_context_id": "SYNTHETIC_ACCOUNT@Exness-MT5Real27",
            "raw": {"type": 2},
        }
        provider.symbol_metadata.return_value = {
            "trade_enabled": True, "tick_size": .001, "tick_value": .1,
            "volume_min": .01, "volume_step": .01, "volume_max": 200,
            "stops_level": 0, "freeze_level": 0, "raw": {"digits": 3, "point": .001},
        }
        provider.order_check.return_value = {"success": True, "retcode": 0, "comment": "Done"}
        with patch("live_execution_consumer.REAL_SMOKE_ACCOUNT", "SYNTHETIC_ACCOUNT@Exness-MT5Real27"), \
             patch("live_execution_consumer._smoke_quote", return_value={
                "bid": 100.0, "ask": 100.26, "quote_received_at": datetime.now(timezone.utc).isoformat()}):
            result = _smoke_post_confirmation_preflight(provider, "XAUUSDm", "BUY", .01)
        self.assertEqual(result["endpoint"], "http://127.0.0.1:22348/mcp")

    def test_smoke_source_path_uses_canonical_submit_only(self):
        import inspect
        import live_execution_consumer as lec
        source = inspect.getsource(lec.run_real_smoke_test)
        self.assertIn("submit_canonical_market_order", source)
        self.assertNotIn("submit_market_order(", source)
        self.assertNotIn('"mt5_market_order"', source)

    def test_smoke_canonical_request_is_sent_from_checked_candidate(self):
        import inspect
        import live_execution_consumer as lec
        source = inspect.getsource(lec.run_real_smoke_test)
        self.assertIn('request=c["canonical_request"]', source)
        self.assertIn('canonical_request_text=c["canonical_request_text"]', source)
        self.assertIn('request_fingerprint=c["canonical_request_fingerprint"]', source)

    def test_ea_geometry_rejection_did_not_call_order_send(self):
        metrics = _smoke_response_metrics({"ok": False, "error": "EXECUTION_GEOMETRY_STALE",
                                           "mt5_order_send_attempted": False})
        self.assertEqual(metrics, {"broker_capable_requests_attempted": 1,
                                   "mt5_order_send_attempted": 0,
                                   "broker_orders_accepted": 0,
                                   "broker_fills_observed": 0})

    def test_broker_retcode_means_order_send_was_attempted(self):
        metrics = _smoke_response_metrics({"ok": False, "retcode": 10016,
                                           "retcode_description": "invalid stops"})
        self.assertEqual(metrics["broker_capable_requests_attempted"], 1)
        self.assertEqual(metrics["mt5_order_send_attempted"], 1)
        self.assertEqual(metrics["broker_orders_accepted"], 0)

    def test_smoke_stale_prior_state_does_not_count_as_new_write(self):
        preflight = {"account": {"raw": {"server": "Exness-MT5Real27", "type": 2}, "balance": 321.7},
                     "selected": {"symbol": "XAUUSDm", "direction": "BUY", "side": "BUY", "volume": .01,
                                  "entry": 4286.626, "stop": 4286.104, "target": 4286.888,
                                  "estimated_stop_loss": .522, "quote": {"quote_age_ms": 0},
                                  "order_check": {"available": True, "success": True}},
                     "endpoint": "http://127.0.0.1:22348/mcp", "provider": object(),
                     "existing_positions": []}
        refreshed = {"account": preflight["account"], "endpoint": preflight["endpoint"],
                     "provider": object(), "selected": {**preflight["selected"],
                         "entry": 4287.126, "stop": 4286.604, "target": 4287.388,
                         "order_check": {"available": True, "success": False, "retcode": 10016,
                                         "comment": "invalid stops"}}}
        with tempfile.TemporaryDirectory() as td, \
             patch("live_execution_consumer.SMOKE_STATE", Path(td) / "smoke_state.json"), \
             patch("live_execution_consumer.smoke_preflight", return_value=preflight), \
             patch("live_execution_consumer._smoke_post_confirmation_preflight", return_value=refreshed), \
             patch("builtins.input", return_value="CONFIRM REAL $1 SMOKE TEST"), \
             patch("builtins.print") as printed:
            Path(td, "smoke_state.json").write_text(json.dumps({"smoke_test_id": "OLD", "broker_writes_attempted": 1}))
            self.assertEqual(run_real_smoke_test(False), 2)
            payload = json.loads(printed.call_args[0][0])
            self.assertEqual(payload["broker_writes_attempted"], 0)

    def test_smoke_preflight_mode_is_read_only_by_contract(self):
        from orchestration.brokers.mt5_shadow import READ_ONLY_BRIDGE_TOOLS
        self.assertNotIn("mt5_market_order", READ_ONLY_BRIDGE_TOOLS)
        self.assertNotIn("mt5_close_position", READ_ONLY_BRIDGE_TOOLS)

    def test_real_monitor_is_read_only_and_reports_zero_writes(self):
        with tempfile.TemporaryDirectory() as td, \
             patch("live_execution_consumer.RUNTIME", Path(td)), \
             patch("live_execution_consumer.REAL_STATE", Path(td) / "real_state.json"), \
             patch("live_execution_consumer.REAL_TRADES", Path(td) / "real_trades.jsonl"), \
             patch("live_execution_consumer.load_config", return_value={"execution_mcp_url": "http://127.0.0.1:22348/mcp", "execution_transport_verified": True}), \
             patch("live_execution_consumer._monitor_bridge_health", return_value={"healthy": True, "queue_depth": 0, "pending": 0, "timeouts": 0, "orphans": 0}):
            (Path(td) / "real_state.json").write_text(json.dumps({
                "armed": True, "mode": "REAL_EXECUTION", "armed_at": "2026-01-01T00:00:00+00:00",
                "account_context_id": "SYNTHETIC_ACCOUNT@Exness-MT5Real27", "masked_login": "SYNTHETIC",
                "server": "Exness-MT5Real27", "starting_equity": 200, "virtual_equity": 200,
                "realized_pnl": 0
            }))
            snapshot = real_monitor_snapshot()
            self.assertTrue(snapshot["armed"])
            self.assertEqual(snapshot["bridge"]["pending"], 0)
            self.assertEqual(snapshot["safety"]["real_execution"], "ARMED")
            self.assertEqual(snapshot["signals"]["orders_submitted"], 0)


if __name__ == "__main__": unittest.main()
