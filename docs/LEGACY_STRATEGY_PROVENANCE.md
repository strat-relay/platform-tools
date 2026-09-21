# Legacy strategy provenance

Stage 1 keeps historical paths and bytes unchanged. The following strategy
sources were inventoried because their code hashes are embedded in manifests,
forward state, validation, or replay identity. `old_sha256` and
`platform_sha256` are identical at extraction.

| source path | old SHA-256 | platform SHA-256 | identity implication |
|---|---|---|---|
| `context_structure_retrace_forward.py` | `b52893c9fc5630d40ded5789d71937edd51ace78497cc7e7f720f03be124c679` | same | preserved |
| `context_structure_retrace_phase2.py` | `fdb2d876965e8c31eecf4f10c2ca1af99450559941a22a7ee6c8ec212559883e` | same | preserved |
| `context_structure_retrace_phase7_observer.py` | `2d921d25d6dec2294b7c5f3b78a7bd2c91b7dfbded15263c01d55a9886b9796d` | same | preserved |
| `context_structure_retrace_compact_state.py` | `26cafb735986a615a1f84cdc30d2a384546230d43f3f6ad915f680dfffc2bed3` | same | preserved |
| `liquidity_displacement.py` | `4f22747b5654e123fd6be49dc820aa58f2bad5c166f42f8c9e445fc6debe88ea` | same | preserved |
| `liquidity_displacement_forward.py` | `2a188165c457f37c47eacfc4a283fb22f8fa9976ba46d0c1661b4584c211c6c6` | same | preserved |
| `liquidity_displacement_entry_forward.py` | `f35a2c71a85e9a46f8e66e3acedcf7dfbbf8a5214d0c41f82f5c3c1bd2d90ffc` | same | preserved |
| `liquidity_displacement_v1_variant_a.py` | `9864e5be59161ce0328998a71083f55b107f56ee9f5b71a6116cfa05c41cb5e1` | same | preserved |

The four Liquidity parameter variants remain separate legacy artifacts. No
StrategyDefinition, primitive registry, or Strategy Studio runtime was added.
Stage 2 namespacing requires an explicit re-freeze because path-sensitive
identity may change.
