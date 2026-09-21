# Recovery runbook

This runbook assumes the Mac was fully powered off. It does not reset or
backfill prospective data.

## Exact post-reboot sequence

```sh
cd /Users/caleb/mt5-native-bridge
curl -fsS http://127.0.0.1:22347/health
python3 context_structure_retrace_forward.py health
python3 context_structure_retrace_forward.py audit-order-isolation
python3 context_structure_retrace_forward.py report
python3 context_structure_retrace_forward.py start --interval 15 --symbols XAUUSDm BTCUSDm USDJPYm EURUSDm
python3 context_structure_retrace_forward.py health
python3 context_structure_retrace_forward.py report
```

Before the `start` line, open MT5, confirm the Exness connection, attach the
compiled EA, and confirm WebRequest permission. If the bridge is not running:

```sh
python3 bridge.py
```

Run it in a separate terminal and recheck `/health`.

## Safe recovery cases

- **Normal restart:** inspect `status`, `health` and `report`; start only if no
  healthy PID owns the lock.
- **Unclean shutdown / stale PID:** verify `ps -p <pid>` returns no process,
  then remove only the stale PID file and start. Do not remove state/ledger.
- **MT5 unavailable:** leave the runner stopped or let its read-error handling
  continue; restore MT5/EA/network, then inspect the gap events.
- **Bridge unavailable:** start/reconnect `bridge.py` and EA; do not create a
  second runner to compensate.
- **State exists but process does not:** this is recoverable; verify the
  manifest/hash, preserve state, then start once.
- **Process exists but heartbeat is stale:** inspect `ps`, bridge health and
  logs first. Do not start a duplicate.
- **Corrupt state:** stop before touching it, copy state/ledger/manifest to a
  timestamped backup, and escalate. Never overwrite with an empty state.
- **Data gap:** preserve `DATA_GAP_DETECTED`; compare live/recovered tags and
  do not silently call recovered observations live.

The runner's lock rejects a second active process. `start` resumes from the
persisted cursors; it must not reset them. Recovery quality is limited by the
known gap-recovery observability weakness documented in `KNOWN_LIMITATIONS.md`.

## Minimum backup before manual intervention

Copy, without editing originals: state JSON, JSONL ledger, manifest,
heartbeat, PID metadata, summary, runner source, reusable package, and this
documentation. Use the read-only helper:

```sh
scripts/backup_runtime.sh
```

Do not back up credentials or terminal secrets.

## Phase 7 recovery

After reboot, verify Phase 6 first and do not reset its state. If the Phase 7
PID is absent or stale, verify its separate state and restart only the
observer:

```sh
python3 context_structure_retrace_phase7_observer.py health
python3 context_structure_retrace_phase7_observer.py audit-order-isolation
python3 context_structure_retrace_phase7_observer.py start --interval 15
```

Phase 7 resumes from its own idempotent event ledger. Never merge its ledger
into Phase 6 or backfill pre-freeze market paths as prospective evidence.

## Orchestrator recovery

After confirming Phase 6 and Phase 7 are still healthy, use the orchestrator's
own lock/state only:

```sh
python3 signal_orchestrator.py audit-order-isolation
python3 signal_orchestrator.py health
python3 signal_orchestrator.py shadow-start --interval 15
```

The orchestrator checkpoints canonical signal identity before downstream
account reads. A broker/account-read timeout therefore cannot duplicate a
signal after restart. It never resets Phase 6/7 cursors or state.
# Dry-run execution consumer recovery

After reboot, verify the existing frozen runners first. Then inspect
`runtime/execution/state.json` and run `python3 live_execution_consumer.py
health`. Do not delete execution state or orchestrator ledgers. Start only
with `python3 live_execution_consumer.py start --interval 15` after the
read-only safety audit passes. Phase 1 cannot submit broker orders.
