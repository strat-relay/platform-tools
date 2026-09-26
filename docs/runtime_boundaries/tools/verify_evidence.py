#!/usr/bin/env python3
"""Read-only evidence checker for the A5 runtime-boundary analysis.

Every factual claim in docs/runtime_boundaries that could be wrong is expressed as a check
against pinned source.  The bridge is read from a *git commit* (never the working tree, which
holds live runtime files); the platform is read from the current checkout.

    python3 docs/runtime_boundaries/tools/verify_evidence.py            # print table, exit 1 on FAIL
    python3 docs/runtime_boundaries/tools/verify_evidence.py --write    # also data/evidence_check.json

Nothing is imported from the bridge.  The one behavioural check on bridge code extracts a single
pure function with ``ast`` and evaluates it in an isolated namespace.  The OD-01 reproduction runs
in a subprocess against a temporary runtime directory.  No broker, MT5, PostgreSQL, NATS or
Kubernetes access; no runtime file is read or written.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PLATFORM = Path.cwd()
# No default: a developer-machine-specific absolute path must never be embedded in a tracked file
# (tests/test_platform_boundary.py::test_no_bridge_checkout_path_is_embedded enforces exactly
# this). Whoever runs this script points it at their own bridge checkout explicitly.
_bridge_repo_env = os.environ.get("MT5_BRIDGE_REPO")
if not _bridge_repo_env:
    raise SystemExit("MT5_BRIDGE_REPO must be set to a local mt5-native-bridge checkout path")
BRIDGE_REPO = Path(_bridge_repo_env)
BRIDGE_COMMIT = "5d4b018857794da8bcf1a9161876c1dc6fe31f73"
OUT = PLATFORM / "docs" / "runtime_boundaries" / "data" / "evidence_check.json"

results: list[dict] = []


def git_show(commit: str, path: str, repo: Path = BRIDGE_REPO) -> str:
    return subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"], check=True,
                          capture_output=True, text=True).stdout


def plat(path: str) -> str:
    return (PLATFORM / path).read_text(encoding="utf-8")


def check(cid: str, claim: str, passed: bool, evidence: str) -> None:
    results.append({"id": cid, "claim": claim, "result": "PASS" if passed else "FAIL", "evidence": evidence})


def platform_grep(pattern: str, *paths: str) -> list[str]:
    cmd = ["git", "-C", str(PLATFORM), "grep", "-n", "-E", pattern, "--", *(paths or ("*.py",))]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    return [x for x in out.splitlines() if x]


def extract_function(source: str, name: str) -> ast.FunctionDef:
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise LookupError(name)


def run_bridge_checks() -> None:
    server = git_show(BRIDGE_COMMIT, "mt5_bridge/server.py")
    life = git_show(BRIDGE_COMMIT, "mt5_bridge/lifecycle.py")
    ea = git_show(BRIDGE_COMMIT, "ea/MT5TradingBridge.mq5")

    enqueue_src = ast.get_source_segment(server, extract_function(server, "enqueue_tool")) or ""
    idem_lines = [ln.strip() for ln in enqueue_src.splitlines() if "idempotency" in ln]
    check("B1", "the bridge never uses idempotency_key for de-duplication (lifecycle has no reference; enqueue_tool only copies it into the command dict)",
          "idempotency" not in life and idem_lines == ['"idempotency_key": args.get("idempotency_key", ""),'],
          f"lifecycle.py mentions={life.count('idempotency')}; enqueue_tool lines={idem_lines}")
    poll = server[server.index("elif command[\"tool\"] == \"mt5_canonical_order_send\":"):server.index("elif command[\"tool\"] == \"mt5_order_check\":")]
    check("B2", "the EA never receives idempotency_key or request_fingerprint (poll line for canonical send)",
          "idempotency" not in poll and "fingerprint" not in poll, "canonical branch of GET /poll builds fields from canonical_fields + canonical_request_text only")
    auth_words = [w for w in ("hmac", "authorization", "bearer", "x-api-key", "signature", "jwt", "compare_digest") if w in server.lower()]
    check("B3", "the bridge performs no caller authentication (mode is a self-declared header)", not auth_words,
          f"auth-related words found in server.py: {auth_words}")

    fn = extract_function(server, "enforce_broker_write_boundary")
    ns: dict = {"EXECUTION_ONLY": True}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "enforce_broker_write_boundary", "exec"), ns)  # noqa: S102 (pure function)
    def probe(tool: str, mode: str | None, smoke: str | None = None, execution_only: bool = True) -> str:
        ns["EXECUTION_ONLY"] = execution_only
        try:
            ns["enforce_broker_write_boundary"](tool, mode, smoke)
            return "ALLOWED"
        except PermissionError as exc:
            return str(exc)
    matrix = {f"{t}|{m}": probe(t, m, "SMOKE-1" if m == "REAL_SMOKE_TEST" else None)
              for t in ("mt5_canonical_order_send", "mt5_close_position", "mt5_trailing_stop", "mt5_market_order")
              for m in ("REAL_EXECUTION", "DEMO_EXECUTION", "REAL_SMOKE_TEST", None)}
    check("B4", "on the execution listener REAL_EXECUTION is allowed ONLY for mt5_canonical_order_send; REAL close/trailing are refused",
          matrix["mt5_canonical_order_send|REAL_EXECUTION"] == "ALLOWED"
          and matrix["mt5_close_position|REAL_EXECUTION"] == "EXPLICIT_DEMO_OR_SMOKE_MODE_REQUIRED"
          and matrix["mt5_trailing_stop|REAL_EXECUTION"] == "EXPLICIT_DEMO_OR_SMOKE_MODE_REQUIRED",
          json.dumps(matrix, sort_keys=True))
    timeout_block = server[server.index("if not event.wait(wait_timeout):"):server.index("with lock:\n        result = results.get(request_id)")]
    check("B5", "on waiter timeout mt5_canonical_order_send takes the terminal lifecycle.timeout() path (not caller_timeout, which is reserved for five legacy write tools)",
          "mt5_canonical_order_send" not in timeout_block.split("lifecycle.caller_timeout")[0] and "lifecycle.timeout(request_id)" in timeout_block,
          "caller_timeout tuple: " + re.search(r'if name in \{([^}]*)\}', timeout_block).group(1).replace("\n", " ")[:170])
    dispatch = life[life.index("def dispatch"):life.index("def coalesced")]
    check("B6", "dispatch() is the single cancellation point: only QUEUED requests dispatch; expiry is checked there; no fence/generation check exists",
          "!= \"QUEUED\"" in dispatch and "deadline_at" in dispatch and "generation" not in dispatch and "fence" not in dispatch.lower(),
          "RequestLifecycle.dispatch (lifecycle.py): status QUEUED -> DISPATCHED under lifecycle.lock")
    check("B7", "no bridge status/lookup endpoint exists for a request or idempotency key (only /health, /poll, /result, /mcp)",
          set(re.findall(r'parsed\.path (?:==|!=) "(/[a-z]*/?)"', server)) == {"/health", "/poll", "/result/", "/mcp"},
          "paths handled: " + str(sorted(set(re.findall(r'parsed\.path (?:==|!=) "(/[a-z]*/?)"', server)))))
    read_fns = ea[ea.index("string PositionsJson()"):ea.index("string HistoryJson")] + ea[ea.index("string HistoryJson"):ea.index("string HistoryJson") + 900]
    check("B8", "EA position/order/history reads do NOT expose comment, magic, order or position identifiers (so a send tag cannot be read back)",
          not re.search(r"COMMENT|MAGIC|POSITION_IDENTIFIER|DEAL_ORDER|DEAL_POSITION_ID", read_fns), "Positions/Orders/History JSON field sets inspected")
    check("B9", "EA polls at most once per MinimumPollMilliseconds (default 5000) and WebRequest timeout defaults to 35000 ms",
          "input int MinimumPollMilliseconds = 5000;" in ea and "input int WebRequestTimeoutMs = 35000;" in ea, "EA inputs")
    check("B10", "a bridge restart orphans (does not resurrect) non-terminal requests; the in-memory queue is not rebuilt",
          "ORPHANED" in life and "not reconstructed" in life, "RequestLifecycle._load_existing_events docstring/behaviour")


def run_platform_checks() -> None:
    uses = platform_grep(r"Mt5ExecutionClient|contracts\.mt5_bridge import|from contracts\.mt5_bridge")
    non_def = [x for x in uses if not x.startswith(("contracts/mt5_bridge/", "tests/"))]
    check("P1", "the production execution path does not use contracts.mt5_bridge.Mt5ExecutionClient (only Mt5ReadClient in paper_runner)",
          all("Mt5ReadClient" in x or "canonical_request" in x for x in non_def), "; ".join(non_def)[:200])
    src = plat("live_execution_consumer.py")
    tree = ast.parse(src)
    process = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "process_intents")
    seg = ast.get_source_segment(src, process) or ""
    check("P2", "in process_intents the broker submit happens BEFORE the terminal execution_decisions append (no durable pre-send record)",
          seg.index("submit_canonical_market_order") < seg.rindex('store.append("execution_decisions"'), "source order inside process_intents")
    check("P3", "idempotency key is stable_id('REALORDER', {execution_intent_id, account_context_id}) - per intent, not per attempt",
          'stable_id("REALORDER", {"execution_intent_id"' in src, "live_execution_consumer.process_intents")
    dsrc = plat("execution/demo_broker.py")
    check("P4", "canonical sends set X-Bridge-Max-Age-Ms=5000 and use a 10 s client timeout; only TimeoutError maps to SUBMISSION_ACK_UNCERTAIN",
          dsrc.count('"X-Bridge-Max-Age-Ms": "5000"') >= 2 and "timeout=10" in dsrc and "SUBMISSION_ACK_UNCERTAIN_RECONCILE_REQUIRED" in dsrc, "execution/demo_broker.py")
    check("P5", "any exception during submit is recorded as DRY_RUN_REJECTED with broker_write_blocked=True (uncertain outcomes are recorded as rejections)",
          'decision, reason = "DRY_RUN_REJECTED", str(exc)' in src and 'details["broker_write_blocked"] = True' in src, "live_execution_consumer inner except")
    mgmt = platform_grep(r"urlopen\(request, timeout=30\)", "live_execution_consumer.py")
    check("P6", "management close/trail bypass the adapter: raw urlopen with X-Execution-Mode REAL_EXECUTION, no idempotency key, no max-age",
          bool(mgmt) and '"X-Execution-Mode": "REAL_EXECUTION"' in src, "; ".join(mgmt))
    writers = platform_grep(r"record_real_execution\(|record_management_result\(", "*.py")
    writers = [w for w in writers if not w.startswith(("trade_manager/central.py", "test_", "tests/"))]
    check("P7", "the ownership ledger is written only by the execution consumer",
          len(writers) == 2 and all(w.startswith("live_execution_consumer.py") for w in writers), "; ".join(writers))
    refresh = platform_grep(r"refresh_from_provider\(", "*.py")
    check("P8", "broker_state.json is refreshed by the execution consumer loop",
          any(w.startswith("live_execution_consumer.py") for w in refresh), "; ".join(refresh)[:200])
    trade_refs = platform_grep(r"tradeability_decisions", "*.py")
    storage = plat("orchestration/storage.py")
    check("P9", "OD-01: 'tradeability_decisions' has exactly one writer, no reader, and is not a declared OrchestrationStore stream",
          len(trade_refs) == 1 and "signal_orchestrator.py:214" in trade_refs[0] and "tradeability_decisions" not in storage, "; ".join(trade_refs))
    check("P10", "OD-02: 'real_execution_resume_generations' has no reference anywhere in the current tree",
          not platform_grep(r"real_execution_resume_generations", "*"), "git grep over all tracked files")
    check("P11", "REAL intents are created from signals + own risk policy + resume gate; create_intents' REAL branch never reads sizing_decisions",
          "REAL sizing is intentionally owned here" in src, "live_execution_consumer.create_intents comment + branch")
    resume_writers = platform_grep(r"live_execution_resume_generation", "*.py")
    resume_writers = [x for x in resume_writers if not x.startswith(("tests/", "test_"))]
    check("P12", "live_execution_resume_generation (orchestration state) is only READ (control_api); no production writer",
          len(resume_writers) == 1 and resume_writers[0].startswith("control_api/app.py"), "; ".join(resume_writers))
    sql = plat("postgres/migrations/008_stratrelay_foundation.sql")
    check("P13", "foundation: acquire_ownership() is compare-and-increment with no expiry test; no function validates a generation on a write path",
          "expires_at" in sql and "expires_at <" not in sql and not re.search(r"FUNCTION platform\.(assert|validate|require)_", sql), "008_stratrelay_foundation.sql")
    check("P14", "foundation: execution.intents.status has no CHECK/state machine and there is no fence/generation column on intents or results",
          not re.search(r"execution\.intents \([^;]*status text NOT NULL CHECK", sql, re.S) and "generation" not in sql[sql.index("execution.intents"):sql.index("execution.results")], "008 execution.intents")
    check("P15", "schema overlap: 003 defines execution.execution_intents + execution.broker_attempts while 008 defines execution.intents",
          "execution.execution_intents" in plat("postgres/migrations/003_future_contracts.sql") and "execution.intents" in sql, "003 vs 008")
    ctx = plat("context_structure_retrace_forward.py")
    names = re.search(r"DECISION_FUNCTION_NAMES = \(([^)]*)\)", ctx).group(1)
    check("P16", "Context freeze guard fingerprints five decision functions only; persistence helpers are outside it",
          all(n in names for n in ("_geometry", "make_setup", "_fill", "_process_bar", "process_symbol"))
          and not any(n in names for n in ("append_event", "save_state", "atomic_json", "write_heartbeat", "load_state")), names)
    io_names = {"open", "write_text", "read_text", "replace", "unlink", "atomic_json", "save_state", "append_event", "_persist_setup", "write_heartbeat"}
    decision_io: dict[str, list[str]] = {}
    for node in ast.parse(ctx).body:
        if isinstance(node, ast.FunctionDef) and node.name in ("_geometry", "make_setup", "_fill", "_process_bar", "process_symbol"):
            found = set()
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    f = call.func
                    nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                    if nm in io_names:
                        found.add(nm)
            decision_io[node.name] = sorted(found)
    check("P23", "Context frozen decision functions reach persistence ONLY through module-global append_event/_persist_setup (no direct file API) - a seam outside the fingerprint",
          set().union(*map(set, decision_io.values())) <= {"append_event", "_persist_setup"} and len(decision_io) == 5, json.dumps(decision_io, sort_keys=True))
    entry = plat("liquidity_displacement_entry_forward.py")
    check("P24", "the four Liquidity instances already reuse the unmodified base runner by reassigning its module-level IO constants (STATE, EVENTS, DAILY, SUMMARY, MANIFEST, PIDFILE, HEARTBEAT, STOP)",
          all(f"base.{n} =" in entry for n in ("STATE", "EVENTS", "DAILY", "SUMMARY", "MANIFEST", "PIDFILE", "HEARTBEAT", "STOP")), "liquidity_displacement_entry_forward.py:62-71")
    check("P25", "Context append_event embeds behaviour (recovery tagging, identity dedupe, counters) before the file write, so a wrapper must sink bytes below it rather than replace it",
          all(t in ctx[ctx.index("def append_event"):ctx.index("def write_heartbeat")] for t in ("_ACTIVE_RECOVERY_CONTEXT", "_event_already_written", 'state["counters"]["events"] += 1')), "append_event body")
    import hashlib
    def decision_hash(source: str) -> tuple[str, int]:
        names = ("_geometry", "make_setup", "_fill", "_process_bar", "process_symbol")
        lines = source.splitlines(keepends=True)
        parts = {}
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                parts[node.name] = "".join(lines[start - 1:node.end_lineno])
        return hashlib.sha256("\n".join(n + "\n" + parts[n] for n in names if n in parts).encode()).hexdigest(), len(parts)
    frozen = re.search(r'FROZEN_DECISION_CODE_HASH = "([0-9a-f]{64})"', ctx).group(1)
    hashes = {}
    for commit in ("65f527b", "80e558e", "edff21b", "868f36f", "c6482c3"):
        try:
            hashes[commit] = decision_hash(git_show(commit, "context_structure_retrace_forward.py"))[0][:12]
        except subprocess.CalledProcessError:
            hashes[commit] = "unavailable"
    check("P26", "the Context decision-code fingerprint (recomputed with ast, equal to FROZEN_DECISION_CODE_HASH at HEAD) is identical at the initial commit and after four later edits to the same file",
          decision_hash(ctx)[0] == frozen and set(hashes.values()) == {frozen[:12]}, json.dumps(hashes, sort_keys=True) + " vs " + frozen[:12])
    pubs = [x for x in platform_grep(r"SharedObservationPublisher\(", "*.py") if not x.startswith(("test_", "tests/"))]
    check("P27", "the shared observation stream that the Trade Manager consumes has exactly one publisher in the repository: the Phase 7 observer (module-level SharedObservationPublisher)",
          len(pubs) == 1 and pubs[0].startswith("context_structure_retrace_phase7_observer.py"), "; ".join(pubs))
    check("P17", "Context assert_frozen documents that persistence may evolve without changing V1 decisions",
          "provenance metadata can evolve without changing V1 decisions" in ctx, "assert_frozen comment")
    liq_manifest_uses = platform_grep(r"runner_sha256", "*.py")
    check("P18", "Liquidity runner_sha256 is recorded in manifests but never compared (single occurrence); enforcement is source_hash of liquidity_displacement.py only",
          len(liq_manifest_uses) == 1 and 'source_hash()' in plat("liquidity_displacement_forward.py"), "; ".join(liq_manifest_uses))
    p7 = plat("context_structure_retrace_phase7_observer.py")
    check("P19", "OD-03: Phase 7 observer reads the legacy full state file the runner declares immutable after cutover",
          'PHASE6_STATE = ROOT / "context_structure_retrace_forward_state.json"' in p7 and "legacy full state is immutable after cutover" in ctx, "phase7 PHASE6_STATE vs runner comment")
    ev = plat("core/strategies/evaluation/models.py")
    check("P20", "Evaluation.evaluation_hash covers provenance and runtime_version (so wrapper identity placed there changes the hash)",
          '"provenance": dict(self.provenance)' in ev and "runtime_version" in ev[ev.index("def to_dict", ev.index("class Evaluation")):], "core/strategies/evaluation/models.py")
    check("P21", "platform.json configures execution_mode REAL_EXECUTION; demo_* keys exist (code support, not deployment evidence)",
          json.loads(plat("orchestration/config/platform.json")).get("execution_mode") == "REAL_EXECUTION", "orchestration/config/platform.json")


def repro_od01() -> None:
    script = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
import test_signal_orchestrator as t
so = t.so
class P(t.FakeProvider):
    def quote(self, symbol): return {"symbol": symbol, "bid": 100.0, "ask": 100.05, "freshness_state": "FRESH", "quote_age_ms": 10}
with tempfile.TemporaryDirectory() as td:
    store = t.OrchestrationStore(Path(td))
    cfg = json.loads(json.dumps(t.DEFAULT_CONFIG)); cfg["portfolios"][0]["strategy_ids"] = ["TEST"]
    so.route_signal(store, t.signal(), cfg, P())
    print(json.dumps([{k: r.get(k) for k in ("decision", "reason", "error")} for r in store.rows("sizing_decisions")]))
'''
    with tempfile.TemporaryDirectory() as rt:
        env = dict(os.environ, TRADING_PLATFORM_RUNTIME_DIR=rt, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", script], cwd=PLATFORM, env=env, capture_output=True, text=True)
    last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "[]"
    try:
        rows = json.loads(last)
    except json.JSONDecodeError:
        rows = []
    check("P22", "OD-01 reproduction: route_signal with a quote-capable provider yields SKIPPED / MISSING_ACCOUNT_DATA / error 'tradeability_decisions'",
          bool(rows) and all(r.get("decision") == "SKIPPED" and r.get("reason") == "MISSING_ACCOUNT_DATA" and r.get("error") == "'tradeability_decisions'" for r in rows), last[:200])


def main() -> int:
    run_bridge_checks()
    run_platform_checks()
    repro_od01()
    width = max(len(r["id"]) for r in results)
    for r in results:
        print(f"{r['result']:4}  {r['id']:<{width}}  {r['claim']}")
    failed = [r for r in results if r["result"] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks pass; bridge={BRIDGE_COMMIT[:7]} platform={subprocess.run(['git','-C',str(PLATFORM),'rev-parse','--short','HEAD'],capture_output=True,text=True).stdout.strip()}")
    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"bridge_commit": BRIDGE_COMMIT, "checks": results}, indent=2, sort_keys=True) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
