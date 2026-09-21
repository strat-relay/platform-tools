# Environment

Observed environment on 2026-09-16:

- macOS host
- Python 3.14.0
- NumPy 2.4.3
- pandas 3.0.1
- requests 2.32.5
- local bridge `http://127.0.0.1:22347`
- MT5 terminal plus attached `MT5TradingBridge.mq5` EA

The repository does not currently have a lockfile or `pyproject.toml`; do not
blindly generate one during a live experiment. The installed environment is the
known working environment. Before a future dependency change, capture a full
package inventory and run the complete suite.

No credentials, account IDs or passwords belong in this repository. MT5/EA
connection state remains in the terminal environment.
