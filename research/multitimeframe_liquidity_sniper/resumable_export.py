"""Resumable, page-durable, READ-ONLY historical MT5 exporter (research only).

Durability model (per broker symbol / timeframe series):

  <out>/<SYMBOL>/<TF>/series_plan.json      immutable request plan (range, page size)
  <out>/<SYMBOL>/<TF>/pages/<a>_<b>.json     one self-verifying page per request range
  <out>/<SYMBOL>/<TF>/ledger.jsonl           append-only, fsynced progress log
  <out>/<SYMBOL>/<TF>/series.jsonl           normalized series (written only at finalize)
  <out>/<SYMBOL>/<TF>/series.metadata.json   written LAST; its status is the COMPLETE marker

A page is written durably (tmp + fsync + rename + directory fsync) and only then
recorded in the ledger.  Page files embed their request range, provenance, row
hash and envelope hash, so a page whose ledger record was lost is verified and
adopted rather than trusted or blindly redownloaded.  On restart every persisted
page is re-verified; a corrupt or mismatching page is quarantined and refetched.

The exporter can call only the read tools in ``ALLOWED_TOOLS``.  It never runs
strategy replay, scenario evaluation, optimization, ranking or parameter search.
"""
from __future__ import annotations

import argparse, fcntl, hashlib, json, math, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.request import Request, urlopen

from .paging import TF_SECONDS, page_ranges
from .symbol_mapping import candidate_symbols, resolve_from_read_probes

ALLOWED_TOOLS = frozenset({"mt5_rates_range", "mt5_symbol_info", "mt5_quote", "mt5_terminal_info", "mt5_account_info"})
DEFAULT_SYMBOLS = (
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURJPY",
    "GBPJPY", "EURGBP", "AUDJPY", "CADJPY", "CHFJPY", "GBPAUD", "GBPCAD",
)
TIMEFRAMES = ("M5", "M15", "H1", "H4")

PAGE_SCHEMA = "native-mt5-page-v1"
LEDGER_SCHEMA = "native-mt5-page-ledger-v1"
SERIES_SCHEMA = "native-mt5-series-v2"
READINESS_SCHEMA = "native-mt5-export-readiness-v1"

# A leading empty span longer than this (with every page confirmed empty on
# retry) is treated as an explicit broker history limit, not a market closure.
BROKER_LIMIT_MIN_EMPTY_SECONDS = 7 * 86400
DEFAULT_READINESS = {
    "min_history_days": 183,             # matches the existing MINIMUM_6_MONTH_HISTORY concept
    "max_unexpected_missing_fraction": 0.01,
}


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- durable IO
def fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def durable_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_break = False
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as probe:
            probe.seek(-1, os.SEEK_END)
            needs_break = probe.read(1) != b"\n"     # torn tail from a crash: never glue onto it
    with open(path, "ab") as handle:
        if needs_break:
            handle.write(b"\n")
        handle.write(line.encode("utf-8") + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------- read bridge
class Bridge:
    """Read-only JSON-RPC client; refuses any tool outside ``ALLOWED_TOOLS``."""

    def __init__(self, url: str, timeout: float = 40.0, origin: str = "RESEARCH_EXPORT"):
        self.url, self.timeout, self.origin = url, timeout, origin

    def call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if tool not in ALLOWED_TOOLS:
            raise PermissionError(f"tool {tool!r} is not permitted for the read-only exporter")
        payload = json.dumps({"jsonrpc": "2.0", "id": int(time.time() * 1000), "method": "tools/call",
                              "params": {"name": tool, "arguments": dict(arguments)}}).encode()
        request = Request(self.url, data=payload, headers={"Content-Type": "application/json", "X-Bridge-Origin": self.origin})
        with urlopen(request, timeout=self.timeout) as response:
            outer = json.loads(response.read())
        result = outer["result"]
        if result.get("isError"):
            raise RuntimeError(result["content"][0]["text"])
        return json.loads(result["content"][0]["text"])


# ----------------------------------------------------------------- validation
def valid_bar(row: Mapping[str, Any]) -> bool:
    try:
        o, h, l, c = (float(row[k]) for k in ("open", "high", "low", "close"))
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
        return False
    return h >= max(o, c) and l <= min(o, c) and h >= l


def _weekday_hour(ts: int) -> tuple[int, int, datetime]:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return d.weekday(), d.hour, d


def classify_gap(missing_from: int, missing_to: int, *, leading: bool = False, trailing: bool = False) -> str:
    """Classify a missing half-open interval [missing_from, missing_to)."""
    span = missing_to - missing_from
    fw, fh, fd = _weekday_hour(missing_from)
    tw, th, _ = _weekday_hour(missing_to)
    start_ok = (fw == 4 and fh >= 19) or fw == 5 or leading
    end_ok = (tw == 6 and th >= 18) or (tw == 0 and th <= 6) or tw == 5 or trailing
    if start_ok and end_ok and span <= 64 * 3600:
        return "EXPECTED_WEEKLY_CLOSE"
    md_from = (fd.month, fd.day)
    to_d = datetime.fromtimestamp(missing_to - 1, timezone.utc)
    md_to = (to_d.month, to_d.day)
    holiday = lambda md: (md[0] == 12 and md[1] >= 24) or (md[0] == 1 and md[1] <= 2)
    if holiday(md_from) and holiday(md_to) and span <= 72 * 3600:
        return "EXPECTED_HOLIDAY_CLOSURE"
    return "UNEXPECTED_DATA_GAP"


def analyze_series(rows: list[Mapping[str, Any]], tf: str, start: int, end: int, *, raw_times: list[int] | None = None,
                   leading_pages_confirmed_empty: bool | None = None) -> dict[str, Any]:
    """Pure data-quality analysis shared by native finalize and legacy audit.

    ``leading_pages_confirmed_empty`` is True/False when page-level evidence exists and None when it cannot exist
    (legacy series): an unconfirmed leading span is never promoted to a broker history limit.
    """
    seconds = TF_SECONDS[tf]
    times = [int(x["time"]) for x in rows]
    raw = times if raw_times is None else raw_times
    duplicates = len(raw) - len(set(raw))
    ordering_pass = raw == sorted(raw) and len(set(raw)) == len(raw)
    invalid = [int(x["time"]) for x in rows if not valid_bar(x)]
    misaligned = sum(1 for t in times if t % seconds)
    spread_missing = sum(1 for x in rows if not isinstance(x.get("spread"), (int, float)) or x["spread"] < 0)
    spread_zero = bool(rows) and all(x.get("spread") == 0 for x in rows)
    gaps: list[dict[str, Any]] = []
    if times:
        first, last_t = times[0], times[-1]
        if first - start > BROKER_LIMIT_MIN_EMPTY_SECONDS:
            established = bool(leading_pages_confirmed_empty)
            evidence = ("LEADING_PAGES_EMPTY_CONFIRMED_ON_RETRY" if established else
                        "LEADING_SPAN_UNCONFIRMED_NO_PAGE_LEVEL_EVIDENCE" if leading_pages_confirmed_empty is None else
                        "LEADING_SPAN_NOT_FULLY_CONFIRMED")
            history = {"broker_limit_established": established, "earliest_available_timestamp": first,
                       "requested_start_timestamp": start, "evidence": evidence}
            start_covered = False
        else:
            history = {"broker_limit_established": False, "earliest_available_timestamp": first,
                       "requested_start_timestamp": start, "evidence": "REQUESTED_START_COVERED"}
            start_covered = True
            if first - start >= seconds:
                gaps.append({"from": start, "to": first, "missing_bars": (first - start) // seconds,
                             "classification": classify_gap(start, first, leading=True), "position": "LEADING"})
        for prev, nxt in zip(times, times[1:]):
            if nxt - prev > seconds:
                mf, mt = prev + seconds, nxt
                gaps.append({"from": mf, "to": mt, "missing_bars": (mt - mf) // seconds,
                             "classification": classify_gap(mf, mt), "position": "INTERIOR"})
        if end - (last_t + seconds) >= seconds:
            gaps.append({"from": last_t + seconds, "to": end, "missing_bars": (end - last_t - seconds) // seconds,
                         "classification": classify_gap(last_t + seconds, end, trailing=True), "position": "TRAILING"})
    else:
        history = {"broker_limit_established": False, "earliest_available_timestamp": None,
                   "requested_start_timestamp": start, "evidence": "NO_DATA_RETURNED"}
        start_covered = False
    unexpected_missing = sum(g["missing_bars"] for g in gaps if g["classification"] == "UNEXPECTED_DATA_GAP")
    expected_bars = max(1, (end - start) // seconds)
    failures = []
    if not rows:
        failures.append({"condition": "NO_BARS_RETURNED"})
    if not ordering_pass:
        failures.append({"condition": "CHRONOLOGICAL_ORDER_FAILED", "duplicates": duplicates})
    if invalid:
        failures.append({"condition": "OHLC_VALIDATION_FAILED", "invalid_bar_count": len(invalid), "first_invalid_time": invalid[0]})
    if rows and not (start_covered or history["broker_limit_established"]):
        failures.append({"condition": "HISTORY_START_NOT_ESTABLISHED", "detail": history["evidence"]})
    return {"failures": failures, "first_timestamp": times[0] if times else None, "last_timestamp": times[-1] if times else None,
            "bar_count": len(rows), "expected_bar_count_upper_bound": expected_bars, "duplicate_count": duplicates,
            "chronological_order_pass": ordering_pass, "ohlc_validation_pass": not invalid, "invalid_ohlc_count": len(invalid),
            "misaligned_bar_count": misaligned, "spread_field_missing_count": spread_missing, "spread_all_zero": spread_zero,
            "history": history, "gap_ledger": gaps,
            "gap_classification_complete": all(g.get("classification") for g in gaps),
            "gap_counts": {c: sum(1 for g in gaps if g["classification"] == c) for c in sorted({g["classification"] for g in gaps})},
            "unexpected_missing_bar_count": unexpected_missing, "unexpected_missing_fraction": unexpected_missing / expected_bars}


def adopt_legacy_series(out: Path, symbol: str, tf: str, start: int, end: int, source_commit: str = "UNKNOWN") -> dict[str, Any] | None:
    """Validate (never modify) a series written by the legacy paged_export layout: <out>/<SYMBOL>/<TF>.jsonl + .metadata.json.

    Returns series metadata (also written under <out>/_validated/) or None if no legacy series exists.  Legacy series carry
    only a whole-series hash: they have no page ledger, no per-page provenance and no confirmed-empty evidence.
    """
    data_path, meta_path = out / symbol / f"{tf}.jsonl", out / symbol / f"{tf}.metadata.json"
    if not data_path.is_file() or not meta_path.is_file():
        return None
    raw_bytes = data_path.read_bytes()
    try:
        legacy = json.loads(meta_path.read_text(encoding="utf-8"))
        rows = [json.loads(l) for l in raw_bytes.decode("utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return {"status": "FAILED_VALIDATION", "symbol": symbol, "timeframe": tf, "provenance_level": "LEGACY_SERIES_HASH_ONLY",
                "failures": [{"condition": "LEGACY_FILES_UNREADABLE"}]}
    failures = []
    if hashlib.sha256(raw_bytes).hexdigest() != legacy.get("data_hash"):
        failures.append({"condition": "LEGACY_DATA_HASH_MISMATCH", "note": "legacy series file does not match its own metadata hash (possibly mid-write or corrupt)"})
    if (legacy.get("requested_start_timestamp"), legacy.get("requested_end_timestamp")) != (start, end):
        failures.append({"condition": "LEGACY_RANGE_DIFFERS_FROM_PLAN", "legacy": [legacy.get("requested_start_timestamp"), legacy.get("requested_end_timestamp")]})
    analysis = analyze_series(rows, tf, start, end, leading_pages_confirmed_empty=None)
    failures.extend(analysis.pop("failures"))
    meta = {"schema": SERIES_SCHEMA, "symbol": symbol, "timeframe": tf, "source": "NATIVE_MT5",
            "provenance_level": "LEGACY_SERIES_HASH_ONLY", "requested_start_timestamp": start, "requested_end_timestamp": end,
            "status": "COMPLETE" if not failures else "FAILED_VALIDATION", "failures": failures, **analysis,
            "feed_fingerprints": [], "data_hash": hashlib.sha256(raw_bytes).hexdigest(), "legacy_retrieved_at": legacy.get("retrieved_at"),
            "legacy_page_counts": {"expected": legacy.get("expected_page_count"), "actual": legacy.get("actual_page_count")},
            "legacy_source_commit": legacy.get("source_commit"), "validated_by_commit": source_commit, "finalized_at": utc_now()}
    meta["metadata_hash"] = sha(canon({k: v for k, v in meta.items() if k != "finalized_at"}))
    durable_write(out / "_validated" / symbol / f"{tf}.validation.json", json.dumps(meta, indent=2, sort_keys=True) + "\n")
    return meta


# ------------------------------------------------------------------- pages
def page_request(symbol: str, tf: str, a: int, b: int, page_size: int, completed_only: bool) -> dict[str, Any]:
    return {"symbol": symbol, "timeframe": tf, "start_timestamp": int(a), "end_timestamp": int(b),
            "page_size": int(page_size), "completed_only": bool(completed_only)}


def build_page(request: dict[str, Any], provenance: dict[str, Any], rows: list[dict[str, Any]],
               dropped_out_of_range: int, state: str) -> dict[str, Any]:
    env = {"schema": PAGE_SCHEMA, "state": state, "request": request, "provenance": provenance,
           "row_count": len(rows), "first_time": rows[0]["time"] if rows else None,
           "last_time": rows[-1]["time"] if rows else None,
           "out_of_range_dropped": dropped_out_of_range, "data_hash": sha(canon(rows)), "rows": rows}
    env["envelope_hash"] = sha(canon(env))
    return env


def verify_page(path: Path, expected_request: Mapping[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
    try:
        env = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"UNREADABLE:{type(exc).__name__}", None
    if not isinstance(env, dict) or env.get("schema") != PAGE_SCHEMA:
        return False, "BAD_SCHEMA", None
    body = {k: v for k, v in env.items() if k != "envelope_hash"}
    if sha(canon(body)) != env.get("envelope_hash"):
        return False, "ENVELOPE_HASH_MISMATCH", None
    rows = env.get("rows")
    if not isinstance(rows, list) or sha(canon(rows)) != env.get("data_hash") or env.get("row_count") != len(rows):
        return False, "DATA_HASH_MISMATCH", None
    if env.get("request") != dict(expected_request):
        return False, "REQUEST_RANGE_MISMATCH", None
    if env.get("state") not in ("COMPLETE", "EMPTY_CONFIRMED") or (env["state"] == "EMPTY_CONFIRMED") != (not rows):
        return False, "BAD_STATE", None
    a, b = expected_request["start_timestamp"], expected_request["end_timestamp"]
    times = []
    try:
        times = [int(r["time"]) for r in rows]
    except (KeyError, TypeError, ValueError):
        return False, "BAD_ROW_TIME", None
    if times != sorted(set(times)) or any(not a <= t < b for t in times):
        return False, "ROW_ORDER_OR_RANGE_VIOLATION", None
    return True, "OK", env


def normalize_rows(rows: Iterable[Mapping[str, Any]], a: int, b: int) -> tuple[list[dict[str, Any]], int, int]:
    """Filter to [a, b), sort, dedupe. Returns (rows, dropped_out_of_range, conflicting_duplicates)."""
    by_time: dict[int, dict[str, Any]] = {}
    dropped = conflicts = 0
    for row in rows:
        t = int(row["time"])
        if not a <= t < b:
            dropped += 1
            continue
        clean = {k: row[k] for k in sorted(row)}
        if t in by_time and by_time[t] != clean:
            conflicts += 1
        by_time[t] = clean
    return [by_time[t] for t in sorted(by_time)], dropped, conflicts


# ------------------------------------------------------------------ ledger
def read_ledger(path: Path) -> tuple[list[dict[str, Any]], int]:
    entries, corrupt = [], 0
    if not path.exists():
        return entries, corrupt
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError
            entries.append(value)
        except ValueError:
            corrupt += 1
    return entries, corrupt


def ledger_event(series: dict[str, Any], event: str, a: int, b: int, **extra: Any) -> dict[str, Any]:
    return {"schema": LEDGER_SCHEMA, "event": event, "symbol": series["symbol"], "timeframe": series["timeframe"],
            "start_timestamp": a, "end_timestamp": b, "recorded_at": utc_now(), **extra}


# ------------------------------------------------------------------ series
class SeriesExporter:
    def __init__(self, out_dir: Path, symbol: str, tf: str, start: int, end: int, page_size: int,
                 bridge, feed: dict[str, Any], *, completed_only: bool = True, retries: int = 2,
                 empty_confirmations: int = 3, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = lambda m: None, source_commit: str = "UNKNOWN"):
        self.symbol, self.tf, self.start, self.end, self.page_size = symbol, tf, int(start), int(end), int(page_size)
        self.dir = out_dir / symbol / tf
        self.bridge, self.feed, self.completed_only = bridge, feed, completed_only
        self.retries, self.empty_confirmations, self.sleep, self.log = retries, empty_confirmations, sleep, log
        self.source_commit = source_commit
        self.ident = {"symbol": symbol, "timeframe": tf}
        self.stats = {"pages_planned": 0, "pages_verified_reused": 0, "pages_fetched": 0, "pages_invalidated": 0,
                      "pages_adopted": 0, "rates_calls": 0, "ledger_corrupt_lines": 0}

    # -- plan
    def _plan(self) -> list[tuple[int, int]]:
        if not 1 <= self.page_size <= 500:
            raise ValueError("page_size must be within 1..500")
        plan = page_ranges(self.start, self.end, self.tf, self.page_size)
        record = {"schema": "native-mt5-series-plan-v1", "symbol": self.symbol, "timeframe": self.tf,
                  "start_timestamp": self.start, "end_timestamp": self.end, "page_size": self.page_size,
                  "completed_only": self.completed_only, "page_count": len(plan),
                  "plan_hash": sha(canon(plan))}
        path = self.dir / "series_plan.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if {k: existing.get(k) for k in record if k != "schema"} != {k: record[k] for k in record if k != "schema"}:
                raise RuntimeError(f"PLAN_MISMATCH for {self.symbol} {self.tf}: existing plan differs from requested plan; "
                                   "use a new output directory rather than mixing ranges")
        else:
            durable_write(path, json.dumps(record, indent=2) + "\n")
        return plan

    def _request(self, a: int, b: int) -> dict[str, Any]:
        return page_request(self.symbol, self.tf, a, b, self.page_size, self.completed_only)

    def _page_path(self, a: int, b: int) -> Path:
        return self.dir / "pages" / f"{a}_{b}.json"

    def _quarantine(self, path: Path, reason: str) -> None:
        qdir = path.parent / "quarantine"
        qdir.mkdir(exist_ok=True)
        os.replace(path, qdir / f"{path.name}.{reason}.{int(time.time())}")
        fsync_dir(path.parent)

    # -- resume inspection
    def inspect(self, plan: list[tuple[int, int]]) -> dict[tuple[int, int], dict[str, Any]]:
        ledger_path = self.dir / "ledger.jsonl"
        entries, corrupt = read_ledger(ledger_path)
        self.stats["ledger_corrupt_lines"] = corrupt
        if corrupt:
            durable_append(ledger_path, canon(ledger_event(self.ident, "INTEGRITY", 0, 0, condition="LEDGER_CORRUPT_LINES_SKIPPED", count=corrupt)))
        latest: dict[tuple[int, int], dict[str, Any]] = {}
        for entry in entries:
            if "start_timestamp" in entry and entry.get("event") != "INTEGRITY":
                latest[(entry["start_timestamp"], entry["end_timestamp"])] = entry
        verified: dict[tuple[int, int], dict[str, Any]] = {}
        for a, b in plan:
            path = self._page_path(a, b)
            record = latest.get((a, b))
            claimed = record is not None and record.get("event") in ("PAGE_COMPLETE", "PAGE_EMPTY_CONFIRMED", "PAGE_ADOPTED")
            if not path.exists():
                if claimed:
                    durable_append(ledger_path, canon(ledger_event(self.ident, "PAGE_INVALIDATED", a, b, reason="PAGE_FILE_MISSING")))
                    self.stats["pages_invalidated"] += 1
                continue
            ok, reason, env = verify_page(path, self._request(a, b))
            if not ok:
                self._quarantine(path, reason)
                durable_append(ledger_path, canon(ledger_event(self.ident, "PAGE_INVALIDATED", a, b, reason=reason)))
                self.stats["pages_invalidated"] += 1
                self.log(f"{self.symbol} {self.tf} [{a},{b}) invalid page quarantined: {reason}")
                continue
            if claimed and record.get("envelope_hash") != env["envelope_hash"]:
                # A ledger record disagrees with a self-consistent page: distrust the pair and refetch.
                self._quarantine(path, "LEDGER_HASH_MISMATCH")
                durable_append(ledger_path, canon(ledger_event(self.ident, "PAGE_INVALIDATED", a, b, reason="LEDGER_HASH_MISMATCH")))
                self.stats["pages_invalidated"] += 1
                continue
            if not claimed:
                durable_append(ledger_path, canon(ledger_event(self.ident, "PAGE_ADOPTED", a, b, page_file=path.name,
                    data_hash=env["data_hash"], envelope_hash=env["envelope_hash"], row_count=env["row_count"],
                    state=env["state"], provenance=env["provenance"])))
                self.stats["pages_adopted"] += 1
            verified[(a, b)] = env
            self.stats["pages_verified_reused"] += 1
        return verified

    # -- retrieval
    def _fetch_rows(self, a: int, b: int) -> tuple[list[dict[str, Any]], int, int, int]:
        args = {"symbol": self.symbol, "timeframe": self.tf, "start_timestamp": a, "end_timestamp": b,
                "page_size": self.page_size, "completed_only": self.completed_only}
        attempts = 0
        empties = 0
        last: Exception | None = None
        while True:
            try:
                self.stats["rates_calls"] += 1
                response = self.bridge.call("mt5_rates_range", args)
                rows, dropped, conflicts = normalize_rows(response.get("rates", []), a, b)
                if conflicts:
                    raise RuntimeError(f"conflicting duplicate bars in page [{a},{b})")
                if rows:
                    return rows, dropped, conflicts, empties
                empties += 1
                if empties >= self.empty_confirmations:
                    return [], dropped, conflicts, empties
                self.sleep(2.0)
            except (PermissionError, KeyboardInterrupt):
                raise
            except Exception as exc:
                last = exc
                attempts += 1
                if attempts > self.retries:
                    raise RuntimeError(f"page retrieval failed for {self.symbol} {self.tf} [{a},{b}): {last}") from exc
                self.sleep(min(2 ** attempts, 8))

    def _retrieve(self, a: int, b: int) -> dict[str, Any]:
        rows, dropped, _, empties = self._fetch_rows(a, b)
        provenance = {"tool": "mt5_rates_range", "bridge_url": getattr(self.bridge, "url", "UNKNOWN"),
                      "feed_fingerprint": self.feed.get("fingerprint"), "account_server": self.feed.get("account_server"),
                      "retrieved_at": utc_now(), "empty_confirmation_attempts": empties, "source_commit": self.source_commit}
        state = "COMPLETE" if rows else "EMPTY_CONFIRMED"
        env = build_page(self._request(a, b), provenance, rows, dropped, state)
        path = self._page_path(a, b)
        durable_write(path, json.dumps(env, sort_keys=True) + "\n")          # 1) page durable
        durable_append(self.dir / "ledger.jsonl", canon(ledger_event(          # 2) then progress durable
            self.ident, "PAGE_COMPLETE" if rows else "PAGE_EMPTY_CONFIRMED", a, b, page_file=path.name,
            data_hash=env["data_hash"], envelope_hash=env["envelope_hash"], row_count=env["row_count"],
            state=state, provenance=provenance, out_of_range_dropped=dropped)))
        self.stats["pages_fetched"] += 1
        return env

    # -- finalize
    def _finalize(self, plan, pages: dict[tuple[int, int], dict[str, Any]]) -> dict[str, Any]:
        meta_path, data_path = self.dir / "series.metadata.json", self.dir / "series.jsonl"
        missing_pages = [list(r) for r in plan if r not in pages]
        base = {"schema": SERIES_SCHEMA, "symbol": self.symbol, "timeframe": self.tf, "source": "NATIVE_MT5",
                "boundary_semantics": "start <= candle_timestamp < end", "completed_only": self.completed_only,
                "requested_start_timestamp": self.start, "requested_end_timestamp": self.end,
                "page_size": self.page_size, "expected_page_count": len(plan), "verified_page_count": len(pages),
                "source_commit": self.source_commit, "finalized_at": utc_now()}
        if missing_pages:
            meta = {**base, "status": "INCOMPLETE", "failures": [{"condition": "PAGES_MISSING", "count": len(missing_pages),
                    "first_missing_range": missing_pages[0]}]}
            self._retract_complete_marker(meta_path)
            return meta
        rows, page_hashes, feeds = [], [], set()
        raw_times: list[int] = []
        for r in plan:
            env = pages[r]
            page_hashes.append(env["envelope_hash"])
            feeds.add(env["provenance"].get("feed_fingerprint"))
            raw_times.extend(int(x["time"]) for x in env["rows"])
            rows.extend(env["rows"])
        leading_empty = all(pages[r]["state"] == "EMPTY_CONFIRMED" for r in plan if rows and r[1] <= int(rows[0]["time"]))
        analysis = analyze_series(rows, self.tf, self.start, self.end, raw_times=raw_times,
                                  leading_pages_confirmed_empty=leading_empty)
        failures = list(analysis.pop("failures"))
        if len(feeds) != 1 or None in feeds:
            failures.append({"condition": "FEED_PROVENANCE_INCONSISTENT", "fingerprints": sorted(map(str, feeds))})
        body = "".join(canon(x) + "\n" for x in rows)
        meta = {**base, "status": "COMPLETE" if not failures else "FAILED_VALIDATION", "failures": failures, **analysis,
                "feed_fingerprints": sorted(map(str, feeds)), "page_hashes_hash": sha(canon(page_hashes)),
                "data_hash": sha(body), "data_file": "series.jsonl"}
        self._retract_complete_marker(meta_path)                       # never leave a stale COMPLETE beside new data
        durable_write(data_path, body)
        meta["metadata_hash"] = sha(canon({k: v for k, v in meta.items() if k != "finalized_at"}))
        durable_write(meta_path, json.dumps(meta, indent=2, sort_keys=True) + "\n")   # written last
        return meta

    @staticmethod
    def _retract_complete_marker(meta_path: Path) -> None:
        if meta_path.exists():
            os.replace(meta_path, meta_path.with_name("series.metadata.json.superseded"))
            fsync_dir(meta_path.parent)

    def existing_complete(self, plan, pages) -> dict[str, Any] | None:
        """Return metadata iff a durable COMPLETE marker still verifies against pages and data."""
        meta_path, data_path = self.dir / "series.metadata.json", self.dir / "series.jsonl"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            body = data_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None
        if meta.get("status") != "COMPLETE" or sha(body) != meta.get("data_hash"):
            return None
        if any(r not in pages for r in plan):
            return None
        if sha(canon([pages[r]["envelope_hash"] for r in plan])) != meta.get("page_hashes_hash"):
            return None
        check = {k: v for k, v in meta.items() if k not in ("metadata_hash", "finalized_at")}
        if sha(canon(check)) != meta.get("metadata_hash"):
            return None
        return meta

    def run(self) -> dict[str, Any]:
        self.dir.mkdir(parents=True, exist_ok=True)
        plan = self._plan()
        self.stats["pages_planned"] = len(plan)
        pages = self.inspect(plan)
        done = self.existing_complete(plan, pages)
        if done is not None:
            self.log(f"{self.symbol} {self.tf} already COMPLETE and verified; no retrieval")
            return {**done, "resume_stats": dict(self.stats)}
        for a, b in plan:
            if (a, b) in pages:
                continue
            try:
                pages[(a, b)] = self._retrieve(a, b)
            except (PermissionError, KeyboardInterrupt):
                raise
            except Exception as exc:
                self.log(f"{self.symbol} {self.tf} [{a},{b}) NOT retrieved: {exc}")
                durable_append(self.dir / "ledger.jsonl", canon(ledger_event(self.ident, "PAGE_FAILED", a, b, error=str(exc)[:300])))
                continue                                          # later pages may still succeed; series stays INCOMPLETE
        meta = self._finalize(plan, pages)
        return {**meta, "resume_stats": dict(self.stats)}


# --------------------------------------------------------------- readiness
def evaluate_readiness(expected_symbols: list[str], timeframes: tuple[str, ...], resolutions: Mapping[str, Any],
                       series: Mapping[tuple[str, str], Mapping[str, Any] | None], feed_probes: list[Mapping[str, Any]],
                       config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Pure function of persisted facts.  Never forced: any failed prerequisite yields false."""
    cfg = {**DEFAULT_READINESS, **(config or {})}
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def block(symbol, tf, condition, **detail):
        blocking.append({"symbol": symbol, "timeframe": tf, "condition": condition, **detail})

    fingerprints = {p.get("fingerprint") for p in feed_probes}
    if not feed_probes or None in fingerprints:
        block(None, None, "FEED_IDENTITY_UNVERIFIED", detail="a feed probe is missing or failed")
    elif len(fingerprints) != 1:
        block(None, None, "FEED_IDENTITY_CHANGED_DURING_EXPORT", fingerprints=sorted(map(str, fingerprints)))
    for symbol in expected_symbols:
        res = resolutions.get(symbol)
        if not res or not res.get("broker_symbol"):
            block(symbol, None, "SYMBOL_RESOLUTION_FAILED", detail=(res or {}).get("error"))
    for symbol in expected_symbols:
        for tf in timeframes:
            meta = series.get((symbol, tf))
            if meta is None:
                block(symbol, tf, "SERIES_NOT_EXPORTED")
                continue
            if meta.get("status") != "COMPLETE":
                block(symbol, tf, "SERIES_NOT_COMPLETE", status=meta.get("status"), failures=meta.get("failures"))
                continue
            first, last = meta["first_timestamp"], meta["last_timestamp"]
            days = (last - first) / 86400
            if days < cfg["min_history_days"]:
                block(symbol, tf, "INSUFFICIENT_HISTORY", days=round(days, 1), required_days=cfg["min_history_days"],
                      broker_limit_established=meta["history"]["broker_limit_established"])
            if meta.get("misaligned_bar_count"):
                block(symbol, tf, "TIMEFRAME_ALIGNMENT_FAILED", misaligned_bar_count=meta["misaligned_bar_count"])
            if meta.get("spread_field_missing_count"):
                block(symbol, tf, "SPREAD_FIELD_INVALID", missing_count=meta["spread_field_missing_count"])
            if meta.get("spread_all_zero"):
                warnings.append({"symbol": symbol, "timeframe": tf, "condition": "SPREAD_ALL_ZERO"})
            if not meta.get("gap_classification_complete"):
                block(symbol, tf, "GAP_CLASSIFICATION_INCOMPLETE")
            if meta.get("unexpected_missing_fraction", 0) > cfg["max_unexpected_missing_fraction"]:
                block(symbol, tf, "UNEXPECTED_GAP_FRACTION_EXCEEDED", unexpected_missing_bars=meta["unexpected_missing_bar_count"],
                      fraction=round(meta["unexpected_missing_fraction"], 5), limit=cfg["max_unexpected_missing_fraction"])
            fp = set(meta.get("feed_fingerprints", []))
            if meta.get("provenance_level") == "LEGACY_SERIES_HASH_ONLY":
                warnings.append({"symbol": symbol, "timeframe": tf, "condition": "PAGE_LEVEL_PROVENANCE_ABSENT_LEGACY_SERIES"})
            elif fingerprints and None not in fingerprints and len(fingerprints) == 1 and fp != {str(next(iter(fingerprints)))}:
                block(symbol, tf, "SERIES_FEED_DIFFERS_FROM_RUN_FEED", series_fingerprints=sorted(fp))
    return {"schema": READINESS_SCHEMA, "SAFE_TO_BEGIN_STAGED_RESEARCH": not blocking, "blocking": blocking,
            "warnings": warnings, "thresholds": cfg, "series_evaluated": len(expected_symbols) * len(timeframes),
            "derived_not_forced": True, "evaluated_at": utc_now()}


# --------------------------------------------------------------------- run
def probe_feed(bridge, phase: str) -> dict[str, Any]:
    probe: dict[str, Any] = {"phase": phase, "probed_at": utc_now(), "fingerprint": None}
    try:
        terminal, account = bridge.call("mt5_terminal_info", {}), bridge.call("mt5_account_info", {})
        identity = {"terminal_name": terminal.get("name"), "terminal_company": terminal.get("company"),
                    "terminal_build": terminal.get("build"), "terminal_server": terminal.get("server"),
                    "account_server": account.get("server")}
        if not identity["account_server"]:
            raise RuntimeError("account server not reported")
        probe.update({"account_server": identity["account_server"], "identity": identity, "fingerprint": sha(canon(identity))})
    except Exception as exc:
        probe["error"] = f"{type(exc).__name__}: {exc}"
    return probe


def resolve_symbol(bridge, canonical: str) -> dict[str, Any]:
    responses, quotes = {}, {}
    for candidate in candidate_symbols(canonical):
        for store, tool in ((responses, "mt5_symbol_info"), (quotes, "mt5_quote")):
            try:
                store[candidate] = bridge.call(tool, {"symbol": candidate})
            except Exception:
                store[candidate] = None
    try:
        broker, info = resolve_from_read_probes(canonical, responses, quotes)
        return {"canonical": canonical, "broker_symbol": broker, "symbol_info": dict(info), "resolved_at": utc_now()}
    except LookupError as exc:
        return {"canonical": canonical, "broker_symbol": None, "error": str(exc), "resolved_at": utc_now()}


def parse_ts(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def run_export(bridge, out: Path, symbols: list[str], timeframes: tuple[str, ...], start: int, end: int, page_size: int,
               *, retries: int = 2, empty_confirmations: int = 3, sleep=time.sleep, log=print, source_commit="UNKNOWN") -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    lock = open(out / ".export.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("another exporter holds the lock on this output directory")
    plan_record = {"schema": "native-mt5-export-plan-v1", "symbols": symbols, "timeframes": list(timeframes),
                   "start_timestamp": start, "end_timestamp": end, "page_size": page_size, "read_only": True}
    plan_path = out / "export_plan.json"
    if plan_path.exists():
        existing = json.loads(plan_path.read_text(encoding="utf-8"))
        if existing != plan_record:
            raise SystemExit("PLAN_MISMATCH: export_plan.json differs from the requested plan; use a new output directory")
    else:
        durable_write(plan_path, json.dumps(plan_record, indent=2) + "\n")

    probes_path = out / "feed_probes.jsonl"
    start_probe = probe_feed(bridge, "start")
    durable_append(probes_path, canon(start_probe))
    if start_probe["fingerprint"] is None:
        log(f"feed identity probe failed: {start_probe.get('error')}")
    res_path = out / "symbol_resolutions.json"
    resolutions = json.loads(res_path.read_text(encoding="utf-8")) if res_path.exists() else {}
    for symbol in symbols:
        if not (resolutions.get(symbol) or {}).get("broker_symbol"):
            resolutions[symbol] = resolve_symbol(bridge, symbol)
            durable_write(res_path, json.dumps(resolutions, indent=2, sort_keys=True) + "\n")
    series_meta: dict[tuple[str, str], Mapping[str, Any] | None] = {}
    totals = {"rates_calls": 0, "pages_fetched": 0, "pages_verified_reused": 0, "pages_invalidated": 0, "pages_adopted": 0}
    for symbol in symbols:
        broker = resolutions[symbol].get("broker_symbol")
        for tf in timeframes:
            if not broker:
                series_meta[(symbol, tf)] = None
                continue
            exporter = SeriesExporter(out, broker, tf, start, end, page_size, bridge, start_probe, retries=retries,
                                      empty_confirmations=empty_confirmations, sleep=sleep, log=log, source_commit=source_commit)
            meta = exporter.run()
            series_meta[(symbol, tf)] = meta
            for k in totals:
                totals[k] += meta["resume_stats"].get(k, 0)
            log(f"{symbol} {tf}: {meta['status']} bars={meta.get('bar_count')} stats={meta['resume_stats']}")
            durable_write(out / "progress.json", json.dumps({"updated_at": utc_now(), "last_series": f"{symbol}/{tf}",
                          "series_done": sum(1 for v in series_meta.values() if v), "totals": totals}, indent=2) + "\n")
    end_probe = probe_feed(bridge, "end")
    durable_append(probes_path, canon(end_probe))
    feed_probes = [json.loads(l) for l in probes_path.read_text().splitlines() if l.strip()]
    # Probes from earlier resumed runs of this same export are included: a changed feed must be visible.
    readiness = evaluate_readiness(symbols, timeframes, {s: {**resolutions[s]} for s in symbols}, series_meta, feed_probes)
    durable_write(out / "readiness.json", json.dumps(readiness, indent=2) + "\n")
    manifest = {"schema": "native-mt5-paged-export-v2", "read_only": True, "broker_writes": 0, "plan": plan_record,
                "series": {f"{s}/{t}": {k: (m or {}).get(k) for k in ("status", "bar_count", "first_timestamp", "last_timestamp",
                           "data_hash", "metadata_hash", "gap_counts", "history")} for (s, t), m in series_meta.items()},
                "run_totals": totals, "source_commit": source_commit, "generated_at": utc_now(),
                "SAFE_TO_BEGIN_STAGED_RESEARCH": readiness["SAFE_TO_BEGIN_STAGED_RESEARCH"]}
    manifest["manifest_hash"] = sha(canon({k: v for k, v in manifest.items() if k != "generated_at"}))
    durable_write(out / "export_manifest.json", json.dumps(manifest, indent=2) + "\n")
    lock.close()
    return {"readiness": readiness, "manifest": manifest}


def run_audit_legacy(bridge, out: Path, symbols: list[str], timeframes: tuple[str, ...], start: int, end: int,
                     *, log=print, source_commit="UNKNOWN") -> dict[str, Any]:
    """Validate a legacy-layout export in place without retrieving any rates and without touching its files.

    Uses only two tiny read-only probes (feed identity, symbol resolution).  Everything it writes lives under
    <out>/_validated/.  Series the legacy exporter has not finished are reported as SERIES_NOT_EXPORTED.
    """
    vdir = out / "_validated"
    vdir.mkdir(parents=True, exist_ok=True)
    probes = [probe_feed(bridge, "audit")]
    durable_append(vdir / "feed_probes.jsonl", canon(probes[0]))
    resolutions = {s: resolve_symbol(bridge, s) for s in symbols}
    durable_write(vdir / "symbol_resolutions.json", json.dumps(resolutions, indent=2, sort_keys=True) + "\n")
    series_meta: dict[tuple[str, str], Mapping[str, Any] | None] = {}
    for symbol in symbols:
        broker = resolutions[symbol].get("broker_symbol")
        for tf in timeframes:
            meta = adopt_legacy_series(out, broker, tf, start, end, source_commit) if broker else None
            series_meta[(symbol, tf)] = meta
            log(f"{symbol} {tf}: {(meta or {}).get('status', 'NOT_EXPORTED')}")
    feed_probes = [json.loads(l) for l in (vdir / "feed_probes.jsonl").read_text().splitlines() if l.strip()]
    readiness = evaluate_readiness(symbols, timeframes, resolutions, series_meta, feed_probes)
    readiness["mode"] = "LEGACY_AUDIT_NO_RETRIEVAL"
    durable_write(vdir / "readiness.json", json.dumps(readiness, indent=2) + "\n")
    return {"readiness": readiness, "series": series_meta}


def main() -> None:
    parser = argparse.ArgumentParser(description="READ-ONLY resumable paged MT5 historical export")
    parser.add_argument("--start", required=True, help="UTC ISO timestamp")
    parser.add_argument("--end", required=True, help="UTC ISO timestamp, exclusive")
    parser.add_argument("--output", required=True)
    parser.add_argument("--mcp-url", default="http://127.0.0.1:22350/mcp")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--symbols", nargs="*", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--timeframes", nargs="*", default=list(TIMEFRAMES))
    parser.add_argument("--audit-legacy", action="store_true",
                        help="validate an existing legacy-layout export in place; performs no rates retrieval and writes only <output>/_validated")
    args = parser.parse_args()
    start, end = parse_ts(args.start), parse_ts(args.end)
    if start >= end:
        raise SystemExit("invalid range")
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        commit = "UNKNOWN"
    if args.audit_legacy:
        result = run_audit_legacy(Bridge(args.mcp_url), Path(args.output), list(args.symbols), tuple(args.timeframes), start, end,
                                  log=lambda m: print(m, flush=True), source_commit=commit)
        r = result["readiness"]
        print(json.dumps({"SAFE_TO_BEGIN_STAGED_RESEARCH": r["SAFE_TO_BEGIN_STAGED_RESEARCH"], "blocking_count": len(r["blocking"]),
                          "blocking_sample": r["blocking"][:10], "broker_writes": 0}, indent=2))
        return
    result = run_export(Bridge(args.mcp_url), Path(args.output), list(args.symbols), tuple(args.timeframes), start, end,
                        args.page_size, retries=args.retries, log=lambda m: print(m, flush=True), source_commit=commit)
    r = result["readiness"]
    print(json.dumps({"SAFE_TO_BEGIN_STAGED_RESEARCH": r["SAFE_TO_BEGIN_STAGED_RESEARCH"], "blocking": r["blocking"][:20],
                      "blocking_count": len(r["blocking"]), "broker_writes": 0}, indent=2))


if __name__ == "__main__":
    main()
