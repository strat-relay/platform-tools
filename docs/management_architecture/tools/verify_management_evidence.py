#!/usr/bin/env python3
"""Read-only evidence checker for the A6 observation / Trade Manager analysis.

Every load-bearing factual claim in docs/management_architecture is a check against pinned source.
The platform is read from the current checkout; the bridge (only for the stack registry and the
write-admission function) is read from a git commit, never its working tree.

    PYTHONDONTWRITEBYTECODE=1 python3 docs/management_architecture/tools/verify_management_evidence.py
    ... --write     # also data/management_evidence_check.json

Behavioural checks run pure Trade Manager code in a subprocess against a temporary directory.
No runtime file, broker, MT5, PostgreSQL, NATS or Kubernetes is touched.
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
BRIDGE_REPO = Path(os.environ.get("MT5_BRIDGE_REPO", "/Users/caleb/mt5-native-bridge"))
BRIDGE_COMMIT = "5d4b018857794da8bcf1a9161876c1dc6fe31f73"
OUT = PLATFORM / "docs" / "management_architecture" / "data" / "management_evidence_check.json"
results: list[dict] = []


def plat(path: str) -> str:
    return (PLATFORM / path).read_text(encoding="utf-8")


def bridge(path: str) -> str:
    return subprocess.run(["git", "-C", str(BRIDGE_REPO), "show", f"{BRIDGE_COMMIT}:{path}"],
                          check=True, capture_output=True, text=True).stdout


def grep(pattern: str, *paths: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(PLATFORM), "grep", "-n", "-E", pattern, "--", *(paths or ("*.py",))],
                         capture_output=True, text=True).stdout
    return [x for x in out.splitlines() if x and not x.startswith(("test_", "tests/"))]


def check(cid: str, claim: str, ok: bool, evidence: str) -> None:
    results.append({"id": cid, "claim": claim, "result": "PASS" if ok else "FAIL", "evidence": evidence})


def func_src(source: str, name: str, cls: str | None = None) -> str:
    tree = ast.parse(source)
    scope = tree.body
    if cls:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls).body
    node = next(n for n in scope if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node) or ""


def static_checks() -> None:
    obs = plat("context_structure_retrace_phase7_observer.py")
    sc = plat("trade_manager/stream_consumer.py")
    fan = plat("trade_manager/fanout.py")
    eng = plat("trade_manager/engine.py")
    cen = plat("trade_manager/central.py")
    con = plat("live_execution_consumer.py")

    call = re.search(r"market_envelope\((.*?)\)\n", obs, re.S).group(1)
    check("M1", "the Phase 7 observer publishes MARKET_OBSERVATION with bid=None and ask=None (it never calls mt5_quote)",
          "bid=None, ask=None" in call and not re.search(r'call_bridge\([^)]*"mt5_quote"', obs), "market_envelope(...) call in poll()")
    users = [x for x in grep(r"market_envelope\(|SharedObservationPublisher\(|position_event\(") if not x.startswith("trade_manager/fanout.py")]
    check("M2", "the observer is the only production caller of market_envelope, position_event and SharedObservationPublisher",
          users and all(x.startswith("context_structure_retrace_phase7_observer.py") for x in users), "; ".join(users)[:220])
    events = re.findall(r'position_event\("(\w+)"', obs)
    check("M3", "the observer publishes lifecycle events of type POSITION_UPDATED only (never OPENED or CLOSED)", set(events) == {"POSITION_UPDATED"}, str(events))
    check("M4", "the Trade Manager consumer discards every MARKET_OBSERVATION lacking bid or ask (MISSING_QUOTE)",
          'event.get("bid") is None or event.get("ask") is None' in sc and "MISSING_QUOTE" in sc, "SharedStreamTradeManager.process_once")
    check("M5", "the consumer reads an m1_history field that no publisher sets",
          'event.get("m1_history"' in sc and "m1_history" not in fan and "m1_history" not in obs, "grep m1_history")
    read_src = func_src(fan, "read", "FanoutConsumer")
    check("M6", "FanoutConsumer.read persists its checkpoint before returning rows, and process_once uses the default persist=True (at-most-once)",
          "if persist:" in read_src and "checkpoint_path.write_text" in read_src and "self.consumer.read()" in sc, "fanout.py read(); stream_consumer.process_once")
    start_src = func_src(sc, "start", "SharedStreamTradeManager")
    check("M7", "each start() resets started_at to now; main() calls start() without an argument; _eligible() filters positions by started_at; positions live only in memory",
          "started_at or datetime.now" in start_src and "manager.start()" in sc and "_parse_time(created) >= _parse_time(self.started_at)" in sc
          and "self.positions: dict" in sc, "stream_consumer.py")
    check("M8", "policy decisions are computed only when mode == REAL_MANAGEMENT; only non-HOLD decisions are persisted (as proposals); Phase2TradeManager has no state store here",
          re.search(r'if self\.mode == "REAL_MANAGEMENT":\s+decision = self\.policy_manager\.evaluate', sc) is not None
          and 'decision.get("action") != "HOLD"' in sc and "Phase2TradeManager()" in sc, "stream_consumer.process_once")
    pol = plat("trade_manager/policies.py")
    check("M9", "the only registered management policy has every action disabled (UNVALIDATED)",
          pol.count('"enabled": False') >= 5 and "context_v1_experiment" in pol and "register_strategy_default(context_v1_experiment())" in pol, "policies.py")
    check("M10", "TM version identity today is a label: no hash of code, parameters or resolution in policies.py / phase2.py",
          not re.search(r"sha256|hashlib|hash\(", pol + plat("trade_manager/phase2.py")), "policies.py, phase2.py")
    engine_actions = set(re.search(r"ACTIONS = \(([^)]*)\)", eng).group(1).replace('"', "").replace("\n", " ").replace(" ", "").split(",")) - {""}
    allowed = set(re.search(r"ALLOWED_ACTIONS = frozenset\(\{([^}]*)\}\)", cen).group(1).replace('"', "").replace(" ", "").split(","))
    mgmt = func_src(con, "process_management_intents")
    supported = set(re.findall(r'action == "(\w+)"', mgmt))
    check("M11", "three action vocabularies: engine (8), central proposals (4 broker-flavoured), executor-supported (CLOSE_POSITION, TRAIL_STOP only)",
          len(engine_actions) == 8 and allowed == {"MOVE_TO_BREAKEVEN", "TRAIL_STOP", "PARTIAL_CLOSE", "CLOSE_POSITION"} and supported == {"CLOSE_POSITION", "TRAIL_STOP"},
          f"engine={sorted(engine_actions)} central={sorted(allowed)} executor={sorted(supported)}")
    check("M12", "no decision path consumes M1 bars, ATR, or staleness inputs the consumer could supply",
          "atr" not in plat("trade_manager/observation.py").lower().replace("structure_snapshot", "")
          and 'market.get("stale")' in eng and '"stale"' not in sc and "age_ms" not in sc, "observation.py, stream_consumer.py")
    check("M13", "position lifecycle events carry no MFE/MAE, so excursion state is not accumulated across observations in the stream consumer",
          "mfe" not in re.search(r"def position_event.*?return payload", fan, re.S).group(0).lower(), "fanout.position_event fields")
    adapter = plat("orchestration/adapters/context_structure_retrace.py")
    liq = plat("orchestration/adapters/liquidity_displacement.py")
    models = plat("orchestration/models.py")
    check("M14", "EntrySignal (StrategySignal) carries economic_position_id, entry_opportunity_id, decision_time and geometry; both adapters emit it only after a reference fill; Liquidity has economic_position_id=None",
          all(f in models for f in ("economic_position_id", "entry_opportunity_id", "decision_time", "entry_price", "stop_price", "target_price"))
          and 'entry_type="MARKET_PAPER_OBSERVATION"' in adapter and "economic_position_id=None" in liq
          and 'if not fill_timestamp' in liq, "orchestration/models.py + adapters")
    rec = re.search(r"def record_real_execution.*?append_unique", cen, re.S).group(0)
    check("M15", "ownership rows record signal_id/intent_id/broker_position_id but no economic_position_id (the only join to a Phase 7 position is via signal_id)",
          "signal_id" in rec and "economic_position_id" not in rec, "central.record_real_execution")
    contracts = plat("infrastructure/messaging/contracts.py")
    ddl3 = plat("postgres/migrations/003_future_contracts.sql")
    check("M16", "V1.2 has no trade.* / management subjects; the trade_management schema has only observation/decision/checkpoint stubs (no managed_trade, no tm_version)",
          not re.search(r'"(trade|management|signal\.management)\.', contracts) and "trade_management.observations" in ddl3
          and "managed_trade" not in ddl3 and "tm_version" not in ddl3, "contracts.py, 003_future_contracts.sql")
    check("M17", "provider-agnostic collectors exist in trade_manager (CausalObservationCollector, ProspectiveExperimentCollector) with injected position/market sources",
          "class CausalObservationCollector" in plat("trade_manager/collector.py") and "class ProspectiveExperimentCollector" in plat("trade_manager/prospective.py"), "collector.py, prospective.py")
    used = [x for x in grep(r"CausalObservationCollector|ProspectiveExperimentCollector") if not x.startswith("trade_manager/")]
    check("M18", "neither collector is instantiated by any production module", not used, "; ".join(used) or "no non-test users outside trade_manager/")
    check("M19", "the orchestrator loop calls authorize_pending_proposals() (proposal sink is in the orchestrator process)",
          "authorize_pending_proposals()" in plat("signal_orchestrator.py"), "signal_orchestrator.py")
    check("M20", "TM paths are cwd-relative (observation stream, observations store, activation file)",
          'root: str = "runtime/trade_manager/observation_stream"' in sc and 'ObservationStore("runtime/trade_manager/observations")' in sc
          and 'default="runtime/trade_manager/activation.json"' in sc, "stream_consumer.py")
    ordering = plat("trade_manager/observation.py")
    check("M21", "observation_id in the causal observer hashes (position, timestamp, bid, ask, m1_time, m5_time): deterministic, no publisher sequence",
          '("economic_position_id", "timestamp", "bid", "ask", "m1_time", "m5_time")' in ordering, "observation.finalize_observation_id")
    check("M22", "the stream envelope observation_id hashes source (incl. sequence=0 at creation) and the publisher assigns the real sequence afterwards",
          'payload["observation_id"] = _id(' in fan and 'event.setdefault("source", {})["sequence"] = self.sequence' in fan, "fanout.py")

    reg = json.loads(bridge("scripts/mt5_stack_services.json"))["services"]
    tm_cmd = reg.get("trade_manager", {}).get("command", "")
    check("M23", "the stack registry (bridge 5d4b018) runs the Trade Manager as stream_consumer in REAL_MANAGEMENT mode and lists no Phase 7 observer and no collector service",
          "stream_consumer" in tm_cmd and "REAL_MANAGEMENT" in tm_cmd and not any("phase7" in json.dumps(v).lower() for v in reg.values())
          and not any("collector" in k for k in reg), tm_cmd[:110])
    server = bridge("mt5_bridge/server.py")
    node = next(n for n in ast.parse(server).body if isinstance(n, ast.FunctionDef) and n.name == "enforce_broker_write_boundary")
    ns: dict = {"EXECUTION_ONLY": True}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "f", "exec"), ns)  # noqa: S102 (pure function)
    def probe(tool: str) -> str:
        try:
            ns["enforce_broker_write_boundary"](tool, "REAL_EXECUTION", None)
            return "ALLOWED"
        except PermissionError as exc:
            return str(exc)
    p = {t: probe(t) for t in ("mt5_close_position", "mt5_trailing_stop", "mt5_canonical_order_send")}
    check("M24", "the bridge refuses REAL close and trailing writes (only the canonical order send is admitted for REAL_EXECUTION)",
          p == {"mt5_close_position": "EXPLICIT_DEMO_OR_SMOKE_MODE_REQUIRED", "mt5_trailing_stop": "EXPLICIT_DEMO_OR_SMOKE_MODE_REQUIRED", "mt5_canonical_order_send": "ALLOWED"}, json.dumps(p, sort_keys=True))


BEHAVIOUR = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, ".")
from trade_manager.observation import CausalObserver
from trade_manager.phase2 import Phase2TradeManager
from trade_manager import central
pos = {"strategy_id": "CONTEXT_STRUCTURE_RETRACE_V1", "setup_id": "S1", "economic_position_id": "ae53cb8a7e76f0a82f19", "symbol": "XAUUSDm",
       "direction": "LONG", "entry": 100.0, "original_stop": 99.0, "original_target": 103.0, "size": None, "status": "OPEN",
       "entry_time": "2026-09-17T10:00:00+00:00", "current_stop": 99.0}
obs = CausalObserver().observe(pos, {"bid": 105.0, "ask": 105.2}, [], [], "2026-09-17T10:30:00+00:00")["observation"]
d = Phase2TradeManager().evaluate(pos, obs, timestamp=obs["timestamp"])
forced = dict(d, action="CLOSE_POSITION")
prop = central.ManagementProposal.from_decision(forced, pos, 1).as_dict()
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    reg = central.OwnershipRegistry(td / "own.jsonl"); st = central.BrokerStateStream(td / "bs.json")
    reg.record_real_execution({"execution_intent_id": "I1", "strategy_id": pos["strategy_id"], "strategy_instance_id": "phase6", "signal_id": "SIG_x",
                               "broker_symbol": "XAUUSDm", "approved_volume": 0.03}, {"ok": True, "ticket": "777", "position_id": "777", "account_position_mode": "HEDGING"})
    st.save(account={"position_mode": "HEDGING"}, positions=[{"ticket": "777", "symbol": "XAUUSDm", "strategy_id": pos["strategy_id"], "direction": "LONG", "current_stop": 99.0, "volume": 0.03}])
    ok, reason, _ = central.authorize(prop, state=st.load(), registry=reg)
print(json.dumps({"action": d["action"], "reasons": d["reason_codes"], "current_R": d["current_R"], "policy": d.get("management_policy_id"),
                  "proposal_identity": prop["position_identity"], "authorized": ok, "authorize_reason": reason}))
'''


def behavioural_checks() -> None:
    with tempfile.TemporaryDirectory() as rt:
        env = dict(os.environ, TRADING_PLATFORM_RUNTIME_DIR=rt, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", BEHAVIOUR], cwd=PLATFORM, env=env, capture_output=True, text=True)
    try:
        r = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        r = {}
    check("M25", "behaviour: at +5R with the default registry the Phase 2 Trade Manager returns HOLD / NO_MANAGEMENT_CHANGE (no management action exists today)",
          r.get("action") == "HOLD" and r.get("reasons") == ["NO_MANAGEMENT_CHANGE"] and r.get("current_R") == 5.0, json.dumps(r)[:200])
    check("M26", "behaviour: a forced non-HOLD decision becomes a proposal whose position_identity is the economic_position_id, and authorize() rejects it POSITION_NOT_FOUND",
          r.get("proposal_identity") == "ae53cb8a7e76f0a82f19" and r.get("authorized") is False and r.get("authorize_reason") == "POSITION_NOT_FOUND", json.dumps(r)[:200])


def main() -> int:
    static_checks()
    behavioural_checks()
    for r in results:
        print(f"{r['result']:4}  {r['id']:<4} {r['claim']}")
    failed = [r for r in results if r["result"] != "PASS"]
    head = subprocess.run(["git", "-C", str(PLATFORM), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"\n{len(results) - len(failed)}/{len(results)} checks pass; platform={head} bridge={BRIDGE_COMMIT[:7]}")
    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"bridge_commit": BRIDGE_COMMIT, "checks": results}, indent=2, sort_keys=True) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
