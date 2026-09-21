import json, os, shutil, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path

from research.multitimeframe_liquidity_sniper import resumable_export as rx

TF = 300
# Monday 2026-01-05 00:00 UTC .. Monday 2026-01-19 (two full weeks, M5)
START = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 1, 19, tzinfo=timezone.utc).timestamp())


def market_open(ts: int) -> bool:
    d = datetime.fromtimestamp(ts, timezone.utc)
    if d.weekday() == 5:
        return False
    if d.weekday() == 4 and d.hour >= 21:
        return False
    if d.weekday() == 6 and d.hour < 22:
        return False
    return True


def bar(ts, spread=6):
    base = 1.1 + (ts % 1000) / 100000
    return {"time": ts, "open": base, "high": base + 0.0002, "low": base - 0.0002, "close": base + 0.0001,
            "tick_volume": 10, "spread": spread, "real_volume": 0}


class FakeBridge:
    url = "fake://bridge"

    def __init__(self, first_available=None, tamper=None, unhealthy_after=None, server="Fake-Server"):
        self.rates_calls = 0
        self.first_available = first_available or 0
        self.tamper = tamper or {}
        self.unhealthy_after = unhealthy_after
        self.server = server
        self.tools = []
        self.ranges = []

    def call(self, tool, args):
        self.tools.append(tool)
        if tool not in rx.ALLOWED_TOOLS:
            raise PermissionError(tool)
        if tool == "mt5_terminal_info":
            return {"name": "MetaTrader 5", "company": "Fake", "build": 1, "server": self.server}
        if tool == "mt5_account_info":
            return {"server": self.server, "login": 1, "balance": 5}
        if tool == "mt5_symbol_info":
            return {"symbol": args["symbol"], "digits": 5} if args["symbol"] == "EURUSD" else {"symbol": args["symbol"], "error": "symbol unavailable"}
        if tool == "mt5_quote":
            return {"symbol": args["symbol"], "bid": 1.0, "ask": 1.1} if args["symbol"] == "EURUSD" else {"error": "no"}
        self.rates_calls += 1
        if self.unhealthy_after is not None and self.rates_calls > self.unhealthy_after:
            raise KeyboardInterrupt("simulated crash")
        a, b = args["start_timestamp"], args["end_timestamp"]
        self.ranges.append((a, b))
        seconds = rx.TF_SECONDS[args["timeframe"]]
        first = -(-max(a, self.first_available) // seconds) * seconds
        rows = [bar(t) for t in range(first, b, seconds) if a <= t < b and market_open(t)][: args["page_size"]]
        return {"rates": [self.tamper.get(r["time"], r) for r in rows]}


class ResumableExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_export(self, bridge, **kw):
        return rx.run_export(bridge, self.tmp, kw.pop("symbols", ["EURUSD"]), kw.pop("timeframes", ("M5",)),
                             kw.pop("start", START), kw.pop("end", END), kw.pop("page_size", 100),
                             sleep=lambda s: None, log=lambda m: None, empty_confirmations=2, **kw)

    def series_dir(self, tf="M5"):
        return self.tmp / "EURUSD" / tf

    # ---- completion & readiness
    def test_fresh_export_completes_and_readiness_is_derived(self):
        out = self.run_export(FakeBridge(), )
        meta = json.loads((self.series_dir() / "series.metadata.json").read_text())
        self.assertEqual(meta["status"], "COMPLETE")
        self.assertTrue(meta["chronological_order_pass"] and meta["ohlc_validation_pass"] and meta["gap_classification_complete"])
        self.assertEqual({g["classification"] for g in meta["gap_ledger"]}, {"EXPECTED_WEEKLY_CLOSE"})
        self.assertEqual(meta["unexpected_missing_bar_count"], 0)
        # only 14 days of data: below the 183-day history prerequisite, so readiness must be FALSE and name it
        r = out["readiness"]
        self.assertFalse(r["SAFE_TO_BEGIN_STAGED_RESEARCH"])
        self.assertEqual([b["condition"] for b in r["blocking"]], ["INSUFFICIENT_HISTORY"])
        self.assertEqual((r["blocking"][0]["symbol"], r["blocking"][0]["timeframe"]), ("EURUSD", "M5"))

    def test_readiness_true_only_when_every_prerequisite_passes(self):
        out = rx.evaluate_readiness(["EURUSD"], ("M5",), {"EURUSD": {"broker_symbol": "EURUSD"}},
            {("EURUSD", "M5"): self._meta()}, [{"fingerprint": "f"}, {"fingerprint": "f"}], {"min_history_days": 10})
        self.assertTrue(out["SAFE_TO_BEGIN_STAGED_RESEARCH"])
        self.assertEqual(out["blocking"], [])

    def _meta(self, **over):
        m = {"status": "COMPLETE", "first_timestamp": 0, "last_timestamp": 30 * 86400, "history": {"broker_limit_established": False},
             "misaligned_bar_count": 0, "spread_field_missing_count": 0, "spread_all_zero": False,
             "gap_classification_complete": True, "unexpected_missing_fraction": 0.0, "unexpected_missing_bar_count": 0,
             "feed_fingerprints": ["f"]}
        m.update(over)
        return m

    def test_each_failed_prerequisite_blocks_and_names_series(self):
        cases = {
            "SERIES_NOT_COMPLETE": {"status": "FAILED_VALIDATION", "failures": [{"condition": "OHLC_VALIDATION_FAILED"}]},
            "TIMEFRAME_ALIGNMENT_FAILED": {"misaligned_bar_count": 3},
            "SPREAD_FIELD_INVALID": {"spread_field_missing_count": 1},
            "UNEXPECTED_GAP_FRACTION_EXCEEDED": {"unexpected_missing_fraction": 0.5, "unexpected_missing_bar_count": 9},
            "SERIES_FEED_DIFFERS_FROM_RUN_FEED": {"feed_fingerprints": ["other"]},
            "GAP_CLASSIFICATION_INCOMPLETE": {"gap_classification_complete": False},
        }
        for condition, over in cases.items():
            r = rx.evaluate_readiness(["EURUSD"], ("M5",), {"EURUSD": {"broker_symbol": "EURUSD"}},
                {("EURUSD", "M5"): self._meta(**over)}, [{"fingerprint": "f"}], {"min_history_days": 10})
            self.assertFalse(r["SAFE_TO_BEGIN_STAGED_RESEARCH"], condition)
            self.assertIn(condition, [b["condition"] for b in r["blocking"]])
        r = rx.evaluate_readiness(["EURUSD", "GBPUSD"], ("M5",), {"EURUSD": {"broker_symbol": "EURUSD"}, "GBPUSD": {"broker_symbol": None, "error": "x"}},
            {("EURUSD", "M5"): self._meta(), ("GBPUSD", "M5"): None}, [{"fingerprint": "f"}, {"fingerprint": "g"}], {"min_history_days": 10})
        got = {(b["symbol"], b["timeframe"], b["condition"]) for b in r["blocking"]}
        self.assertIn(("GBPUSD", None, "SYMBOL_RESOLUTION_FAILED"), got)
        self.assertIn(("GBPUSD", "M5", "SERIES_NOT_EXPORTED"), got)
        self.assertIn((None, None, "FEED_IDENTITY_CHANGED_DURING_EXPORT"), got)

    # ---- resume / idempotence
    def test_crash_then_resume_fetches_only_missing_pages_and_is_idempotent(self):
        crashing = FakeBridge(unhealthy_after=5)
        with self.assertRaises(KeyboardInterrupt):
            self.run_export(crashing)
        pages = sorted((self.series_dir() / "pages").glob("*.json"))
        self.assertEqual(len(pages), 5)
        self.assertFalse((self.series_dir() / "series.metadata.json").exists())        # not COMPLETE while partial
        resumed = FakeBridge()
        self.run_export(resumed)
        plan = rx.page_ranges(START, END, "M5", 100)
        done = {tuple(map(int, p.stem.split("_"))) for p in pages}
        self.assertEqual(set(resumed.ranges), set(plan) - done)                        # only the genuinely missing ranges
        self.assertFalse(set(resumed.ranges) & done)
        meta1 = json.loads((self.series_dir() / "series.metadata.json").read_text())
        self.assertEqual(meta1["status"], "COMPLETE")
        again = FakeBridge()
        self.run_export(again)
        self.assertEqual(again.rates_calls, 0)                                          # nothing redownloaded
        meta2 = json.loads((self.series_dir() / "series.metadata.json").read_text())
        self.assertEqual(meta1["data_hash"], meta2["data_hash"])
        self.assertEqual(meta1["metadata_hash"], meta2["metadata_hash"])

    def test_complete_series_equals_uninterrupted_series(self):
        self.run_export(FakeBridge(unhealthy_after=None))
        clean = (self.series_dir() / "series.jsonl").read_text()
        shutil.rmtree(self.tmp / "EURUSD")
        with self.assertRaises(KeyboardInterrupt):
            self.run_export(FakeBridge(unhealthy_after=7))
        self.run_export(FakeBridge())
        self.assertEqual((self.series_dir() / "series.jsonl").read_text(), clean)

    def test_corrupt_page_is_quarantined_and_refetched_not_trusted(self):
        self.run_export(FakeBridge())
        pages = sorted((self.series_dir() / "pages").glob("*.json"))
        victim = pages[3]
        env = json.loads(victim.read_text())
        env["rows"][0]["close"] = 9.9                                                   # silent bit-rot; hash now wrong
        victim.write_text(json.dumps(env))
        bridge = FakeBridge()
        self.run_export(bridge)
        self.assertEqual(bridge.rates_calls, 1)                                         # exactly the corrupt page
        self.assertTrue(list((self.series_dir() / "pages" / "quarantine").glob("*")))
        ledger = [json.loads(l) for l in (self.series_dir() / "ledger.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["event"] == "PAGE_INVALIDATED" and e["reason"] == "ENVELOPE_HASH_MISMATCH" for e in ledger))
        self.assertEqual(json.loads((self.series_dir() / "series.metadata.json").read_text())["status"], "COMPLETE")

    def test_missing_page_and_torn_ledger_tail_recover(self):
        self.run_export(FakeBridge())
        pages = sorted((self.series_dir() / "pages").glob("*.json"))
        pages[0].unlink()
        with open(self.series_dir() / "ledger.jsonl", "ab") as h:
            h.write(b'{"schema":"native-mt5-page-ledger-v1","event":"PAGE_COM')      # torn write
        bridge = FakeBridge()
        self.run_export(bridge)
        self.assertEqual(bridge.rates_calls, 1)
        entries, corrupt = rx.read_ledger(self.series_dir() / "ledger.jsonl")
        self.assertEqual(corrupt, 1)                                                     # reported, never glued or trusted
        self.assertTrue(any(e["event"] == "INTEGRITY" for e in entries))

    def test_page_missing_its_ledger_record_is_adopted_without_redownload(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_export(FakeBridge(unhealthy_after=4))
        lines = (self.series_dir() / "ledger.jsonl").read_text().splitlines()
        (self.series_dir() / "ledger.jsonl").write_text("\n".join(lines[:-1]) + "\n")   # crash between page write and ledger append
        bridge = FakeBridge()
        self.run_export(bridge)
        plan = rx.page_ranges(START, END, "M5", 100)
        self.assertEqual(len(set(bridge.ranges)), len(plan) - 4)
        ledger = [json.loads(l) for l in (self.series_dir() / "ledger.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["event"] == "PAGE_ADOPTED" for e in ledger))

    def test_tampered_series_data_invalidates_complete_marker_and_is_rebuilt_from_pages(self):
        self.run_export(FakeBridge())
        (self.series_dir() / "series.jsonl").write_text("garbage\n")
        bridge = FakeBridge()
        self.run_export(bridge)
        self.assertEqual(bridge.rates_calls, 0)                                          # rebuilt from verified pages
        meta = json.loads((self.series_dir() / "series.metadata.json").read_text())
        self.assertEqual(rx.sha((self.series_dir() / "series.jsonl").read_text()), meta["data_hash"])

    def test_plan_mismatch_refuses_to_mix_ranges(self):
        self.run_export(FakeBridge())
        with self.assertRaises(SystemExit):
            self.run_export(FakeBridge(), end=END + 86400)

    # ---- validation gates
    def test_invalid_ohlc_blocks_complete_and_readiness_names_condition(self):
        bad_time = START + 3600
        broken = dict(bar(bad_time)); broken["high"] = broken["low"] - 1
        out = self.run_export(FakeBridge(tamper={bad_time: broken}))
        meta = json.loads((self.series_dir() / "series.metadata.json").read_text())
        self.assertEqual(meta["status"], "FAILED_VALIDATION")
        self.assertIn("OHLC_VALIDATION_FAILED", [f["condition"] for f in meta["failures"]])
        conds = [(b["symbol"], b["timeframe"], b["condition"]) for b in out["readiness"]["blocking"]]
        self.assertIn(("EURUSD", "M5", "SERIES_NOT_COMPLETE"), conds)

    def test_unexpected_gap_is_classified_not_hidden(self):
        class Holey(FakeBridge):
            def call(self, tool, args):
                r = super().call(tool, args)
                if tool == "mt5_rates_range":
                    r["rates"] = [x for x in r["rates"] if not START + 2 * 86400 <= x["time"] < START + 2 * 86400 + 3600]
                return r
        self.run_export(Holey())
        meta = json.loads((self.series_dir() / "series.metadata.json").read_text())
        self.assertEqual(meta["status"], "COMPLETE")
        unexpected = [g for g in meta["gap_ledger"] if g["classification"] == "UNEXPECTED_DATA_GAP"]
        self.assertEqual(len(unexpected), 1)
        self.assertEqual(unexpected[0]["missing_bars"], 12)

    def test_leading_empty_history_needs_confirmed_empty_pages_to_establish_broker_limit(self):
        bridge = FakeBridge(first_available=START + 10 * 86400)
        self.run_export(bridge)
        meta = json.loads((self.series_dir() / "series.metadata.json").read_text())
        self.assertEqual(meta["status"], "COMPLETE")
        self.assertTrue(meta["history"]["broker_limit_established"])
        self.assertEqual(meta["history"]["evidence"], "LEADING_PAGES_EMPTY_CONFIRMED_ON_RETRY")

    def test_empty_page_is_retried_before_being_confirmed(self):
        class Slow(FakeBridge):
            seen = 0
            def call(self, tool, args):
                r = super().call(tool, args)
                if tool == "mt5_rates_range" and args["start_timestamp"] == START and Slow.seen < 1:
                    Slow.seen += 1
                    return {"rates": []}                       # terminal still syncing history
                return r
        self.run_export(Slow())
        page = json.loads(next(iter(sorted((self.series_dir() / "pages").glob("*.json")))).read_text())
        self.assertEqual(page["state"], "COMPLETE")
        self.assertTrue(page["row_count"] > 0)

    def test_feed_change_between_probes_blocks_readiness(self):
        bridge = FakeBridge()
        self.run_export(bridge)
        bridge2 = FakeBridge(server="Other-Server")
        out = self.run_export(bridge2)
        self.assertIn("FEED_IDENTITY_CHANGED_DURING_EXPORT", [b["condition"] for b in out["readiness"]["blocking"]])

    # ---- safety
    def test_exporter_only_uses_read_tools(self):
        bridge = FakeBridge()
        self.run_export(bridge)
        self.assertLessEqual(set(bridge.tools), rx.ALLOWED_TOOLS)
        with self.assertRaises(PermissionError):
            rx.Bridge("http://127.0.0.1:1/mcp").call("mt5_market_order", {})
        source = Path(rx.__file__).read_text()
        for forbidden in ("mt5_market_order", "mt5_pending_order", "mt5_canonical_order_send", "mt5_close_position"):
            self.assertEqual(source.count(forbidden), 0)
        import ast
        names = {n.name.lower() for n in ast.walk(ast.parse(source)) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        self.assertFalse([n for n in names if any(w in n for w in ("replay", "optimi", "rank", "scenario", "grid"))])

    def test_gap_classification_rules(self):
        fri = int(datetime(2026, 1, 9, 21, 0, tzinfo=timezone.utc).timestamp())
        mon = int(datetime(2026, 1, 11, 22, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(rx.classify_gap(fri, mon), "EXPECTED_WEEKLY_CLOSE")
        mid = int(datetime(2026, 1, 7, 10, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(rx.classify_gap(mid, mid + 7200), "UNEXPECTED_DATA_GAP")
        xmas = int(datetime(2025, 12, 25, 0, 0, tzinfo=timezone.utc).timestamp())
        self.assertEqual(rx.classify_gap(xmas, xmas + 20 * 3600), "EXPECTED_HOLIDAY_CLOSURE")


class LegacyAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_legacy(self, symbol="EURUSD", tf="M5", start=START, end=END, mutate=None):
        seconds = rx.TF_SECONDS[tf]
        rows = [bar(t) for t in range(start, end, seconds) if market_open(t)]
        if mutate:
            rows = mutate(rows)
        body = "".join(rx.canon(r) + "\n" for r in rows)
        d = self.tmp / symbol
        d.mkdir(exist_ok=True)
        (d / f"{tf}.jsonl").write_text(body)
        import hashlib
        (d / f"{tf}.metadata.json").write_text(json.dumps({"data_hash": hashlib.sha256(body.encode()).hexdigest(),
            "requested_start_timestamp": start, "requested_end_timestamp": end, "retrieved_at": "x",
            "expected_page_count": 3, "actual_page_count": 3}))
        return d

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.tmp.rglob("*") if p.is_file() and "_validated" not in p.parts}

    def test_valid_legacy_series_is_adopted_without_modifying_legacy_files(self):
        self.write_legacy()
        before = self.snapshot()
        meta = rx.adopt_legacy_series(self.tmp, "EURUSD", "M5", START, END)
        self.assertEqual(meta["status"], "COMPLETE")
        self.assertEqual(meta["provenance_level"], "LEGACY_SERIES_HASH_ONLY")
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.tmp / "_validated" / "EURUSD" / "M5.validation.json").exists())

    def test_hash_mismatch_range_mismatch_and_invalid_ohlc_are_not_trusted(self):
        d = self.write_legacy()
        (d / "M5.jsonl").write_text((d / "M5.jsonl").read_text() + rx.canon(bar(END - 300)) + "\n")
        self.assertIn("LEGACY_DATA_HASH_MISMATCH", [f["condition"] for f in rx.adopt_legacy_series(self.tmp, "EURUSD", "M5", START, END)["failures"]])
        self.write_legacy()
        self.assertIn("LEGACY_RANGE_DIFFERS_FROM_PLAN", [f["condition"] for f in rx.adopt_legacy_series(self.tmp, "EURUSD", "M5", START, END + 86400)["failures"]])
        def poison(rows):
            rows[5] = {**rows[5], "high": rows[5]["low"] - 1}
            return rows
        self.write_legacy(mutate=poison)
        meta = rx.adopt_legacy_series(self.tmp, "EURUSD", "M5", START, END)
        self.assertEqual(meta["status"], "FAILED_VALIDATION")
        self.assertIn("OHLC_VALIDATION_FAILED", [f["condition"] for f in meta["failures"]])

    def test_unconfirmed_leading_span_is_not_promoted_to_broker_limit(self):
        self.write_legacy(start=START + 10 * 86400)
        meta = rx.adopt_legacy_series(self.tmp, "EURUSD", "M5", START, END)   # plan starts earlier than the data
        conditions = [f["condition"] for f in meta["failures"]]
        self.assertIn("LEGACY_RANGE_DIFFERS_FROM_PLAN", conditions)
        self.assertIn("HISTORY_START_NOT_ESTABLISHED", conditions)

    def test_audit_reports_unexported_series_and_uses_no_rates_tool(self):
        self.write_legacy()
        bridge = FakeBridge()
        out = rx.run_audit_legacy(bridge, self.tmp, ["EURUSD"], ("M5", "M15"), START, END, log=lambda m: None)
        self.assertNotIn("mt5_rates_range", bridge.tools)
        got = {(b["symbol"], b["timeframe"], b["condition"]) for b in out["readiness"]["blocking"]}
        self.assertIn(("EURUSD", "M15", "SERIES_NOT_EXPORTED"), got)
        self.assertFalse(out["readiness"]["SAFE_TO_BEGIN_STAGED_RESEARCH"])
        self.assertTrue(any(w["condition"] == "PAGE_LEVEL_PROVENANCE_ABSENT_LEGACY_SERIES" for w in out["readiness"]["warnings"]))


if __name__ == "__main__":
    unittest.main()
