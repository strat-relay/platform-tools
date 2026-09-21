# LIQUIDITY_DISPLACEMENT_SCALP_V1 status

Status: active extended forward paper testing.

Last observed runner snapshot: XAUUSDm active, PID 79056, 15-second polling,
13 tracked signals. Confirm live state with `status` before relying on this
snapshot.

Source SHA: `4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea`

Primary commands:

```sh
python3 liquidity_displacement_forward.py status
python3 liquidity_displacement_forward.py health
python3 liquidity_displacement_forward.py watch
python3 liquidity_displacement_forward.py trades
python3 liquidity_displacement_forward.py checkpoint
python3 liquidity_displacement_forward.py stop
```

Do not change strategy parameters, manifests, or state namespaces during the
forward sample. See `README.md` for design and output details.
