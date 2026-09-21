# Linux research deployment preparation

This is a portable, offline-only preparation package for an Ubuntu/Debian VM
(8 vCPU, 32 GB RAM, 250 GB SSD). It has not been deployed or started.

Install Python 3.11 or 3.12 in a VM-local virtual environment, then install
`requirements-research.txt`. Install the optional PostgreSQL file only when
offline import/validation is explicitly scheduled. The package intentionally
does not install MetaTrader5, Wine, MT5 terminals, execution bridges, or
broker clients.

The VM must fail closed if `RESEARCH_BRIDGE_URL` is non-empty or contains an
execution endpoint (`22348`, `execution`, `OrderSend`, or an MT5 write route).
Research jobs consume copied historical files. The absent Mac research bridge
at `127.0.0.1:22350` is an external export dependency and is not started by
this package.

## Startup/checks

There is no service startup command in this stage. The safe sequence for a
future VM is: verify the copy manifest and SHA-256 checksums; create a VM-local
venv; install the dependency file; set `RESEARCH_MODE=OFFLINE` and
`BROKER_WRITES=0`; verify no broker endpoint is configured; run unit tests and
offline replay/backtest entry points manually; write outputs under
`/srv/mt5-research/artifacts`.

Health means: Python version supported, dependency import checks pass, source
and dataset checksums match, `RESEARCH_MODE=OFFLINE`, `BROKER_WRITES=0`, no
execution endpoint configured, and no MT5/Wine process is present. A healthy
research VM does not imply production or broker health.

See `../../artifacts/migration/dataset-copy-plan.md` and
`../../artifacts/migration/production-transport-design.md` for the copy and
future production transport decisions.
