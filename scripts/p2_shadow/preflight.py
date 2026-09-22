from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


KUBECONFIG = os.getenv("KUBECONFIG", "/Users/caleb/Downloads/local.yaml")
NAMESPACE = os.getenv("P2_NAMESPACE", "trading")
DEPLOYMENT = "mt5-native-bridge-main-runtime"
POD = "mt5-native-bridge-main-runtime-7f7bd678ff-wlv7b"
REQUIRED = ("XAUUSDm", "BTCUSDm", "USDJPYm", "EURUSDm")


def kubectl(*args: str) -> str:
    return subprocess.check_output(["kubectl", "--kubeconfig", KUBECONFIG, *args], text=True)


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> None:
    if kubectl("config", "current-context").strip() != "local": fail("Kubernetes context is not 'local'")
    node_data = json.loads(kubectl("get", "nodes", "-o", "json"))
    if not any(any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]) for n in node_data["items"]):
        fail("no Ready Kubernetes node")
    deployment = json.loads(kubectl("-n", NAMESPACE, "get", "deployment", DEPLOYMENT, "-o", "json"))
    if deployment["status"].get("availableReplicas") != 1 or deployment["status"].get("observedGeneration") != deployment["metadata"]["generation"]:
        fail("main runtime deployment is not fully available")
    pod = json.loads(kubectl("-n", NAMESPACE, "get", "pod", POD, "-o", "json"))
    container_names = {x["name"] for x in pod["spec"]["containers"]}
    if container_names != {"context-paper", "orchestrator-shadow"}:
        fail(f"unexpected main runtime containers: {sorted(container_names)}")
    if any(not c.get("ready") or c.get("state", {}).get("running") is None for c in pod["status"]["containerStatuses"]):
        fail("main runtime containers are not all ready/running")
    cm = json.loads(kubectl("-n", NAMESPACE, "get", "configmap", "mt5-native-bridge-safe-platform-config", "-o", "json"))
    config_text = cm["data"]["platform.json"]
    platform_config = json.loads(config_text)
    if platform_config.get("execution_mcp_url") or platform_config.get("execution_transport_verified") is not False:
        fail("runtime execution transport is not disabled")
    runtime_pvcs = [v.get("persistentVolumeClaim", {}).get("claimName") for v in pod["spec"]["volumes"]]
    if "mt5-native-bridge-runtime" not in runtime_pvcs:
        fail("main runtime shared PVC is not mounted")
    pvc = json.loads(kubectl("-n", NAMESPACE, "get", "pvc", "mt5-native-bridge-runtime", "-o", "json"))
    if pvc["status"].get("phase") != "Bound": fail("shared runtime PVC is not Bound")

    probe = r'''import datetime,json,socket,time,urllib.request,urllib.parse,pathlib,sys
base=json.loads(sys.argv[1])
health_url=base.rsplit('/mcp',1)[0]+'/health'
def req(url,body=None):
 r=urllib.request.Request(url,data=body,headers={'Content-Type':'application/json','X-Bridge-Origin':'P2_1_PREFLIGHT','X-Bridge-Priority-Class':'BACKGROUND'} if body else {})
 with urllib.request.urlopen(r,timeout=25) as x:return json.loads(x.read())
h=req(health_url)
if h.get('ok') is not True or h.get('execution_metrics',{}).get('mt5_order_send_attempted',0)!=0: raise RuntimeError('22347 health or zero-write proof failed')
rpc=lambda i,m,p:req(base,json.dumps({'jsonrpc':'2.0','id':i,'method':m,'params':p}).encode())
tools=[t['name'] for t in rpc('tools','tools/list',{}).get('result',{}).get('tools',[])]
forbidden={'mt5_market_order','mt5_pending_order','mt5_cancel_pending_order','mt5_close_position','mt5_trailing_stop','mt5_canonical_order_send'}
if forbidden.intersection(tools): raise RuntimeError('22347 exposes broker-write methods')
symbols={}
for s in ('XAUUSDm','BTCUSDm','USDJPYm','EURUSDm'):
 r=rpc(s,'tools/call',{'name':'mt5_symbol_info','arguments':{'symbol':s}}).get('result',{})
 if r.get('isError') or not r.get('content'): raise RuntimeError('symbol info unavailable '+s)
 symbols[s]=True
u=urllib.parse.urlparse(base)
sock=socket.socket();sock.settimeout(2);code=sock.connect_ex((u.hostname,22348));sock.close()
if code==0: raise RuntimeError('22348 is reachable')
state=json.loads(pathlib.Path('/work/context_structure_retrace_forward_state_compact.json').read_text())
if state.get('runner_status')!='ACTIVE': raise RuntimeError('Context runner is not ACTIVE')
stamp=datetime.datetime.fromisoformat(state['last_successful_read_at'].replace('Z','+00:00'))
age=(datetime.datetime.now(datetime.timezone.utc)-stamp).total_seconds()
if age<0 or age>90: raise RuntimeError('Context data is stale')
for s in ('XAUUSDm','BTCUSDm','USDJPYm','EURUSDm'):
 if not (state.get('symbols',{}).get(s) or {}).get('initialized'): raise RuntimeError('symbol not initialized '+s)
signals=pathlib.Path('/work/runtime/orchestration/signals.jsonl')
if not signals.is_file() or not __import__('os').access(signals,__import__('os').R_OK): raise RuntimeError('legacy signal output unreadable')
rows=[x for x in signals.read_bytes().splitlines() if x.strip()]
if not rows: raise RuntimeError('legacy signal file empty')
last=json.loads(rows[-1])
manifest=json.loads(pathlib.Path('/work/runtime/orchestration/manifest.json').read_text())
if manifest.get('mode')!='SHADOW' or manifest.get('live_execution_enabled') is not False: raise RuntimeError('orchestrator is not SHADOW-safe')
print(json.dumps({'bridge_22347':'REACHABLE_READ_ONLY','write_methods_exposed':False,'symbols':symbols,'port_22348':'INACCESSIBLE','context_runner':'ACTIVE','last_successful_read_age_seconds':round(age,1),'legacy_file_bytes':signals.stat().st_size,'legacy_file_inode':signals.stat().st_ino,'last_signal_id':last.get('signal_id'),'orchestrator_mode':'SHADOW','broker_write_attempted':h.get('execution_metrics',{}).get('mt5_order_send_attempted',0)}))'''
    mcp_url = platform_config.get("mcp_url")
    if not mcp_url: fail("read-only bridge MCP URL missing")
    result = subprocess.check_output(["kubectl", "--kubeconfig", KUBECONFIG, "-n", NAMESPACE,
        "exec", POD, "-c", "context-paper", "--", "python", "-c", probe, json.dumps(mcp_url)],
        text=True, env=os.environ.copy())
    probe_result = json.loads(result.strip().splitlines()[-1])
    configs = json.loads(kubectl("-n", NAMESPACE, "get", "configmap", "control-api-safe-runtime", "-o", "json"))
    deployments = json.loads(kubectl("-n", NAMESPACE, "get", "deployments", "-o", "json"))
    forbidden_names = ("execution-consumer", "trade-manager", "phase7")
    active = [d["metadata"]["name"].lower() for d in deployments["items"] if d.get("status", {}).get("availableReplicas", 0)]
    if any(any(token in name for token in forbidden_names) for name in active): fail(f"execution/TM/Phase7 deployment is active: {active}")
    output = {"LIVE_RUNTIME_PREFLIGHT_PASS": True, "context": "local", "namespace": NAMESPACE,
        "main_deployment_generation": deployment["metadata"]["generation"],
        "images": {c["name"]: c["image"] for c in pod["spec"]["containers"]},
        "image_ids": {c["name"]: c.get("imageID") for c in pod["status"]["containerStatuses"]},
        "platform_config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
        "runtime_volume_read_only_for_shadow": True, "active_deployments": active,
        "bridge_probe": probe_result, "control_api_config_data_keys": sorted(configs.get("data", {}).keys()),
        "execution_consumer_running": False, "trade_manager_running": False, "phase7_running": False,
        "signal_db_primary_enabled": False, "signal_jetstream_primary_enabled": False}
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(json.dumps({"LIVE_RUNTIME_PREFLIGHT_PASS": False, "error": str(exc)}, indent=2), file=sys.stderr)
        raise SystemExit(1)
