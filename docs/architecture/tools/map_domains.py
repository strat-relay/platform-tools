#!/usr/bin/env python3
"""Map every tracked file to a StratRelay target domain.

READ-ONLY.  Input : docs/extraction/data/file_classification.csv (A1 manifest)
            Output: docs/architecture/data/module_domain_map.csv
                    docs/architecture/data/domain_summary.json

    python3 docs/architecture/tools/map_domains.py

The rules encode the ownership decisions in ADR-0001.  Nothing here imports
repository code or touches runtime state, brokers, PostgreSQL, NATS or MT5.

Domain codes
    core/market            MarketDataProvider port, instrument catalog, price semantics
    core/strategy          strategies, versions, runners, streams, freeze manifests
    core/signals           EntrySignal canonicalisation, replay/eligibility guard, publication gate
    core/trade_management  ManagedTrade, TradeManagerDecision, policies, observation
    core/performance       closed trades, series, snapshots, evidence
    personal_execution     routing/authorisation, risk, execution intents, reconciliation, ownership
    adapters/mt5           platform-side MT5 adapters implementing the ports
    ops_api                internal operations API (evolution of Control API)
    kernel                 ids, config, runtime paths, persistence, messaging, transitional stores
    research               research / backtest / evidence production
    bridge                 mt5-native-bridge repository
    commerce               (stratrelay-platform) - nothing exists in this repo today
    legacy | migration | programme | docs
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path.cwd()
SRC = ROOT / "docs" / "extraction" / "data" / "file_classification.csv"
OUT = ROOT / "docs" / "architecture" / "data"
PKG = "src/trading_platform/"

# (domain, note[, split_into])
EXACT: dict[str, tuple] = {
    # ---- root runtime modules
    "signal_orchestrator.py": ("personal_execution",
        "THREE jobs in one process: (1) canonicalise strategy events -> EntrySignal [core/signals]; (2) per-account sizing, tradeability, order_check, live arming [personal_execution/routing]; (3) append distribution_queue/delivery_status [replaced by publication gate + stratrelay distribution]",
        "core/signals: poll_once, load_adapters, live_classification, manifest | personal_execution/routing: route_signal, live_enable, live_audit, account_verification | drop: distribution_queue/delivery_status"),
    "live_execution_consumer.py": ("personal_execution", "REAL execution engine, arming, smoke tests, audits; MT5 comms behind BrokerClient (A1 doc 05)", ""),
    "paper_engine.py": ("core/strategy", "paper/reference execution model shared by strategies (reference-trade semantics)", ""),
    "paper_runner.py": ("adapters/mt5", "call_bridge() is a hand-rolled market-data client; replaced by MarketDataProvider; CLI is legacy", ""),
    "strategy_report_format.py": ("core/performance", "formats strategy reports; feeds ops read models", ""),
    "context_structure_retrace_forward.py": ("core/strategy", "Context strategy runner; must depend on MarketDataProvider, not bridge_read", ""),
    "context_structure_retrace_compact_state.py": ("core/strategy", "runner state projection", ""),
    "context_structure_retrace_phase2.py": ("core/strategy", "strategy phase-2 analysis", ""),
    "context_structure_retrace_phase7_observer.py": ("core/trade_management", "Phase 7 passive management observer = Trade Manager research/prospective observer", ""),
    "context_structure_retrace_cross_sanity.py": ("research", "", ""),
    "liquidity_displacement.py": ("core/strategy", "Liquidity Displacement detector (hashed by runner: byte-identical in Stage 1)", ""),
    "liquidity_displacement_forward.py": ("core/strategy", "base Liquidity runner; shells out to mt5_read_once under Wine (bypasses MarketDataProvider)", ""),
    "liquidity_displacement_entry_forward.py": ("core/strategy", "Liquidity per-instrument variant runners (xau33/btc25/usdjpy25)", ""),
    "liquidity_displacement_btc25_sweep_overlap_audit.py": ("research", "", ""),
    # ---- orchestration
    "orchestration/__init__.py": ("core/signals", "package name `orchestration` is retired (see 01 §names)", ""),
    "orchestration/models.py": ("core/signals", "StrategySignal -> EntrySignal; AccountSnapshot/SizingDecision are personal_execution",
        "core/signals: StrategySignal | personal_execution: AccountSnapshot, SizingDecision | kernel/ids: stable_id"),
    "orchestration/registry.py": ("core/strategy", "StrategyRegistry -> strategy catalog; PortfolioRegistry -> personal_execution/portfolio",
        "core/strategy: StrategyRegistry | personal_execution: PortfolioRegistry"),
    "orchestration/replay_guard.py": ("core/signals", "startup market-watermark guard: must gate PUBLICATION as well as execution (never publish a replayed signal)", ""),
    "orchestration/liquidity_instances.py": ("core/strategy", "stream/instance definitions + live publisher; instance == Strategy x Instrument variant", ""),
    "orchestration/risk.py": ("personal_execution", "RiskSizingEngine (account-level)", ""),
    "orchestration/tradeability.py": ("personal_execution", "pre-broker tradeability gate; the market-open/freshness subset may be reused as a publication-eligibility check", ""),
    "orchestration/gateway.py": ("personal_execution", "BrokerDataGateway (read coalescing/freshness); not imported by any production module, only test_bridge_lifecycle", ""),
    "orchestration/config.py": ("kernel", "config loader; hard-coded bridge endpoint defaults", ""),
    "orchestration/storage.py": ("kernel", "TRANSITIONAL JSONL store (signals, route_decisions, sizing_decisions, distribution_queue, delivery_status) -> Postgres outbox", ""),
    "orchestration/brokers/__init__.py": ("adapters/mt5", "", ""),
    "orchestration/brokers/mt5_shadow.py": ("adapters/mt5", "MT5 read adapter; account-snapshot shaping -> personal_execution, symbol metadata -> core/market",
        "adapters/mt5: transport | personal_execution: account snapshot service | core/market: symbol metadata mapper"),
    "orchestration/config/platform.json": ("kernel", "SPLIT config: strategies -> core/strategy catalog; accounts/portfolios/demo_* -> personal_execution; symbol_mappings -> core/market instrument catalog; endpoints -> kernel",
        "core/strategy: strategies | personal_execution: accounts, portfolios, demo_* | core/market: symbol_mappings | kernel: endpoints"),
    "orchestration/config/risk_policy.json": ("personal_execution", "", ""),
    "orchestration/config/tradeability_policy.json": ("personal_execution", "", ""),
    "orchestration/config/liquidity_variant_registration_plan.json": ("core/strategy", "", ""),
    # ---- execution
    "execution/__init__.py": ("personal_execution", "", ""),
    "execution/models.py": ("personal_execution", "ExecutionIntent / ExecutionDecision / market snapshot", ""),
    "execution/demo.py": ("personal_execution", "arming, account authorisation, virtual bankroll", ""),
    "execution/risk_policy.py": ("personal_execution", "fail-closed REAL risk-policy resolution", ""),
    "execution/storage.py": ("kernel", "TRANSITIONAL JSONL store", ""),
    "execution/adapters.py": ("personal_execution", "BrokerReadAdapter port; calls private provider._read (fix)", ""),
    "execution/demo_broker.py": ("adapters/mt5", "wire + MT5 retcode semantics -> adapters/mt5; arming guards -> personal_execution/broker_gate",
        "adapters/mt5: wire, retcode semantics | personal_execution/broker_gate: arming guards"),
    # ---- trade manager
    "trade_manager/central.py": ("personal_execution", "SPLIT: OwnershipRegistry, BrokerStateStream, ManagementProposal, authorize() are broker/account concepts -> personal_execution; nothing in it is Trade Manager policy",
        "personal_execution/ownership: OwnershipRegistry | personal_execution/broker_state: BrokerStateStream | personal_execution/translation: ManagementProposal, authorize, authorize_pending_proposals"),
    "trade_manager/semantics.py": ("core/market", "canonical bid/ask price semantics for managing a position", ""),
    "trade_manager/fanout.py": ("kernel", "TRANSITIONAL file-based sequenced stream + checkpoints -> NATS JetStream + consumer checkpoints", ""),
    "trade_manager/stream_consumer.py": ("core/trade_management", "shared-stream Trade Manager adapter; consumes transitional file stream", ""),
    "trade_manager/storage.py": ("kernel", "TRANSITIONAL JSONL ledger", ""),
    "trade_manager/observation_storage.py": ("kernel", "TRANSITIONAL JSONL observation/decision store", ""),
    # ---- control api / ops
    "control_api/__init__.py": ("ops_api", "", ""),
    "control_api/__main__.py": ("ops_api", "", ""),
    "control_api/app.py": ("ops_api", "read-only Ops API; embeds MCP BridgeReader (-> BrokerClient read port)", ""),
    "control_api/observability.py": ("ops_api", "imports four strategy runner modules to read status (latent cycle) -> read from DB/read models", ""),
}

PREFIX: list[tuple[str, tuple]] = [
    ("orchestration/adapters/", ("core/signals", "strategy-ledger -> EntrySignal adapters", "")),
    ("trade_manager/reports/", ("research", "frozen management discrepancy/replay evidence for one XAUUSD trade", "")),
    ("trade_manager/", ("core/trade_management", "", "")),
    ("context_structure_retrace/", ("core/strategy", "Context strategy package (ledger/outcomes/provenance also feed performance evidence)", "")),
    ("postgres/migrations/", ("kernel", "SCHEMA OWNERSHIP MUST BE SPLIT trading vs commercial (05_DATA_OWNERSHIP.md)", "")),
    ("postgres/", ("kernel", "trading DB persistence layer", "")),
    ("strategies/", ("core/strategy", "strategy documentation / outputs", "")),
    ("research/", ("research", "", "")),
    ("deploy/prod/", ("ops_api", "Ops API production deploy (tunnel + Access)", "")),
    ("deploy/research/", ("migration", "", "")),
    ("scripts/watch_", ("ops_api", "operations observers", "")),
    ("scripts/mt5_stack", ("bridge", "A1: split bridge half / platform half", "")),
    ("scripts/start_mt5", ("bridge", "", "")),
    ("scripts/stop_mt5", ("bridge", "", "")),
    ("scripts/backup_runtime", ("kernel", "runtime backup; replaced by DB backups", "")),
    ("archive/", ("legacy", "", "")),
    ("docs/ADR/", ("core/strategy", "strategy ADR-001..020 (numbering is the strategy series; architecture ADRs live in docs/architecture)", "")),
    ("docs/extraction/", ("programme", "A1 extraction manifest", "")),
    ("docs/architecture/", ("programme", "A2 architecture", "")),
]

DOCS: dict[str, tuple] = {
    "docs/DISTRIBUTION_PIPELINE.md": ("core/signals", "SUPERSEDED: auto-distribution of every canonical signal contradicts the explicit publication boundary"),
    "docs/STRATEGY_SIGNAL_SCHEMA.md": ("core/signals", ""), "docs/SIGNAL_ORCHESTRATOR.md": ("core/signals", "describes the three-way orchestrator; split per ADR"),
    "docs/STRATEGY_REGISTRY.md": ("core/strategy", ""), "docs/RESULTS_REGISTRY.md": ("core/performance", "evidence classifications = seed of the provenance model"),
    "docs/RESEARCH_HISTORY.md": ("research", ""), "docs/LARGE_EVIDENCE_ARTIFACTS.md": ("core/performance", ""),
    "docs/PHASE7_POSITION_MANAGEMENT.md": ("core/trade_management", ""),
    "docs/PORTFOLIO_ACCOUNT_MODEL.md": ("personal_execution", ""), "docs/EXECUTION_INTENT_SCHEMA.md": ("personal_execution", ""),
    "docs/DEMO_EXECUTION.md": ("personal_execution", ""), "docs/LIVE_ARMING_AND_KILL_SWITCH.md": ("personal_execution", ""),
    "docs/LIVE_EXECUTION_CONSUMER.md": ("personal_execution", ""), "docs/BROKER_RECONCILIATION.md": ("personal_execution", ""),
    "docs/RISK_SIZING_ENGINE.md": ("personal_execution", ""),
    "docs/CONTROL_API.md": ("ops_api", ""), "docs/PROD_DEPLOY.md": ("ops_api", ""),
    "docs/POSTGRES_SYSTEM_OF_RECORD.md": ("kernel", ""), "docs/PHASE6_POSTGRES_RECONCILIATION.md": ("migration", ""),
    "docs/PHASE6_893_FORENSICS.md": ("migration", ""), "docs/DOCKER_DEPLOYMENT.md": ("kernel", ""), "docs/DATA_AND_STATE.md": ("kernel", ""),
    "docs/ARCHITECTURE.md": ("docs", "pre-product architecture; superseded by docs/architecture"),
}

TEST_DOMAIN = (("test_signal_orchestrator", "core/signals"), ("test_execution_consumer", "personal_execution"),
               ("test_trade_manager", "core/trade_management"), ("test_context_structure_retrace", "core/strategy"),
               ("test_liquidity_displacement", "core/strategy"), ("test_paper_engine", "core/strategy"),
               ("test_phase6", "migration"), ("test_postgres", "kernel"), ("test_bridge", "bridge"), ("test_symbol_snapshot", "bridge"))
TESTS_DIR = {"test_control_api": "ops_api", "test_liquidity_instances": "core/strategy", "test_risk_policy": "personal_execution",
             "test_tradeability": "personal_execution", "test_startup_replay_guard": "core/signals", "test_account_margin_mode": "personal_execution",
             "test_trade_manager_central": "personal_execution", "test_strategy_observability": "ops_api",
             "test_mt5_stack": "bridge", "test_multitimeframe_liquidity_sniper": "research"}

# explicit domain-aligned targets where the generic rule would collide or mislead
TARGET_OVERRIDE = {
    "signal_orchestrator.py": PKG + "personal_execution/routing/signal_orchestrator.py",   # SPLIT: see split_into
    "live_execution_consumer.py": PKG + "personal_execution/engine/live_execution_consumer.py",
    "trade_manager/central.py": PKG + "personal_execution/management_translation/central.py",  # SPLIT
    "orchestration/__init__.py": PKG + "core/signals/__init__.py",
    "orchestration/models.py": PKG + "core/signals/models.py",                                # SPLIT
    "orchestration/registry.py": PKG + "core/strategy/catalog.py",                            # SPLIT
    "orchestration/config.py": PKG + "kernel/config/orchestration_config.py",
    "orchestration/storage.py": PKG + "kernel/transitional/orchestration_jsonl_store.py",
    "execution/storage.py": PKG + "kernel/transitional/execution_jsonl_store.py",
    "trade_manager/storage.py": PKG + "kernel/transitional/trade_manager_jsonl_ledger.py",
    "trade_manager/observation_storage.py": PKG + "kernel/transitional/trade_manager_observation_store.py",
    "trade_manager/fanout.py": PKG + "kernel/transitional/observation_fanout_stream.py",
    "orchestration/config/platform.json": PKG + "kernel/config/platform.json",                # SPLIT
    "orchestration/brokers/__init__.py": PKG + "adapters/mt5/__init__.py",
    "orchestration/brokers/mt5_shadow.py": PKG + "adapters/mt5/read_adapter.py",              # SPLIT
    "execution/demo_broker.py": PKG + "adapters/mt5/execution_adapter.py",                    # SPLIT
    "paper_runner.py": PKG + "adapters/mt5/legacy_call_bridge.py",                            # SPLIT
    "execution/adapters.py": PKG + "personal_execution/broker_access.py",
    "orchestration/gateway.py": PKG + "personal_execution/broker_access_gateway.py",
    "trade_manager/semantics.py": PKG + "core/market/price_semantics.py",
    "strategy_report_format.py": PKG + "core/performance/strategy_report_format.py",
    "orchestration/replay_guard.py": PKG + "core/signals/replay_guard.py",
    "orchestration/liquidity_instances.py": PKG + "core/strategy/liquidity/streams.py",
    "orchestration/risk.py": PKG + "personal_execution/risk/sizing_engine.py",
    "execution/risk_policy.py": PKG + "personal_execution/risk/risk_policy.py",
    "orchestration/tradeability.py": PKG + "personal_execution/routing/tradeability.py",
    "orchestration/config/risk_policy.json": PKG + "personal_execution/risk/risk_policy.json",
    "orchestration/config/tradeability_policy.json": PKG + "personal_execution/routing/tradeability_policy.json",
    "execution/demo.py": PKG + "personal_execution/arming.py",
    "execution/models.py": PKG + "personal_execution/models.py",
    "orchestration/config/liquidity_variant_registration_plan.json": PKG + "core/strategy/liquidity/variant_registration_plan.json",
}

# domain -> package path in trading-platform (Stage 2, domain aligned)
DOMAIN_PATH = {"core/market": PKG + "core/market/", "core/strategy": PKG + "core/strategy/", "core/signals": PKG + "core/signals/",
               "core/trade_management": PKG + "core/trade_management/", "core/performance": PKG + "core/performance/",
               "personal_execution": PKG + "personal_execution/", "adapters/mt5": PKG + "adapters/mt5/", "ops_api": PKG + "ops_api/",
               "kernel": PKG + "kernel/"}


def domain_for(path: str, cls: str) -> tuple[str, str, str]:
    name = path.split("/")[-1]
    if path in EXACT:
        d = EXACT[path]
        return d[0], d[1], d[2] if len(d) > 2 else ""
    if cls == "MT5_BRIDGE":
        return "bridge", "A1: leaves for mt5-native-bridge", ""
    if cls == "LEGACY":
        return "legacy", "", ""
    if cls == "MIGRATION_TOOL":
        return "migration", "", ""
    if path in DOCS:
        return DOCS[path][0], DOCS[path][1], ""
    for prefix, d in PREFIX:
        if path.startswith(prefix):
            return d[0], d[1], d[2]
    if path.startswith("tests/") or name.startswith("test_"):
        stem = name.rsplit(".", 1)[0]
        if stem in TESTS_DIR:
            return TESTS_DIR[stem], "test", ""
        for prefix, dom in TEST_DOMAIN:
            if name.startswith(prefix):
                return dom, "test", ""
    if path.startswith("docs/"):
        return "docs", "cross-cutting operational doc; split per domain when repos exist", ""
    if name.startswith("context_structure_retrace_") or name.startswith("liquidity_displacement_"):
        return "core/strategy", "runner working file / manifest / summary (mutable state committed as source; see A1 S5)", ""
    if name in {"compose.yaml", "requirements-postgres.txt"} or name.startswith("Dockerfile"):
        return "kernel", "", ""
    if name in {"README.md", "AGENT_STATUS.md", ".gitignore"}:
        return "docs", "", ""
    return "unmapped", "", ""


def target_path(path: str, domain: str, cls: str, a1_stage2: str) -> tuple[str, str]:
    """-> (repo, domain-aligned path)"""
    if domain == "bridge":
        return "mt5-native-bridge", a1_stage2
    if domain == "legacy":
        return "trading-platform", "research/legacy/" + path
    if domain == "migration":
        return "trading-platform", "tools/migration/" + path
    if domain == "research":
        return "trading-platform", path if path.startswith("research/") else "research/" + path
    if domain in {"docs", "programme"}:
        return "trading-platform", path
    if path in TARGET_OVERRIDE:
        return "trading-platform", TARGET_OVERRIDE[path]
    if path.startswith("orchestration/adapters/"):
        return "trading-platform", PKG + "core/signals/adapters/" + path.removeprefix("orchestration/adapters/")
    if path.startswith("postgres/"):
        return "trading-platform", PKG + "kernel/persistence/" + path.removeprefix("postgres/")
    if domain in DOMAIN_PATH:
        base = DOMAIN_PATH[domain]
        if path.startswith("tests/") or path.split("/")[-1].startswith("test_"):
            return "trading-platform", "tests/" + domain.replace("/", "_") + "/" + path.split("/")[-1]
        rel = path
        for pre in ("orchestration/adapters/", "orchestration/brokers/", "orchestration/config/", "orchestration/", "execution/",
                    "trade_manager/", "control_api/", "context_structure_retrace/", "postgres/", "strategies/", "deploy/prod/", "scripts/"):
            if rel.startswith(pre):
                rel = rel[len(pre):]
                break
        return "trading-platform", base + rel
    return "trading-platform", path


def main() -> None:
    rows = list(csv.DictReader(SRC.open()))
    out, summary, by_repo = [], defaultdict(Counter), Counter()
    for r in rows:
        dom, note, split = domain_for(r["path"], r["class"])
        repo, tp = target_path(r["path"], dom, r["class"], r["stage2_path"])
        if r["mixed"] == "1" and not split and dom != "bridge":
            split = "A1 mixed-file finding: " + r["note"]
        out.append({"path": r["path"], "a1_class": r["class"], "a1_mixed": r["mixed"], "target_domain": dom,
                    "target_repo": repo, "target_path_domain_aligned": tp, "split_into": split,
                    "a1_stage2_path": r["stage2_path"], "live_reachable": r["live_reachable"], "note": note})
        summary[dom][r["class"]] += 1
        by_repo[repo] += 1
    seen: dict[str, str] = {}
    for o in out:
        key = (o["target_repo"], o["target_path_domain_aligned"])
        if key in seen and not o["target_path_domain_aligned"].startswith(("research/legacy/", "tools/migration/")):
            raise SystemExit(f"target path collision: {key}: {seen[key]} vs {o['path']}")
        seen[key] = o["path"]
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "module_domain_map.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0])); w.writeheader(); w.writerows(out)
    doc = {"files": len(out), "by_domain": {d: sum(c.values()) for d, c in sorted(summary.items())},
           "by_repo": dict(by_repo), "split_files": sorted(o["path"] for o in out if o["split_into"]),
           "unmapped": [o["path"] for o in out if o["target_domain"] == "unmapped"]}
    (OUT / "domain_summary.json").write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    md = ["# Domain map tables (generated by tools/map_domains.py)\n",
          "## Files per target domain\n", "| domain | files |", "|---|--:|"]
    md += [f"| `{d}` | {n} |" for d, n in sorted(doc["by_domain"].items(), key=lambda kv: -kv[1])]
    md += ["", "## Files needing structural change (split across domains, or embedded MT5 client replaced by a port)\n", "| file | primary domain | split |", "|---|---|---|"]
    md += [f"| `{o['path']}` | `{o['target_domain']}` | {(o['split_into'] or o['note']).replace(' | ', '; ')} |" for o in out if o["split_into"]]
    md += ["", "## Python modules by domain (non-test)\n"]
    for d in sorted({o["target_domain"] for o in out}):
        mods = [o for o in out if o["target_domain"] == d and o["path"].endswith(".py") and not o["path"].startswith(("tests/", "test_"))
                and not o["path"].split("/")[-1].startswith("test_")]
        if mods and d not in {"legacy", "research", "migration"}:
            md += [f"### `{d}` ({len(mods)})", ""] + [f"- `{o['path']}` -> `{o['target_path_domain_aligned'].removeprefix('src/trading_platform/')}`" for o in mods] + [""]
    (OUT / "domain_tables.md").write_text("\n".join(md) + "\n")
    print(json.dumps({k: v for k, v in doc.items() if k != "split_files"}, indent=2, sort_keys=True))
    print("split files:", len(doc["split_files"]))


if __name__ == "__main__":
    main()
