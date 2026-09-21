"""One fixed engine-validation configuration; not a search space."""
NEUTRAL_CONFIG = {
    "h4_context": "BASIC_STRUCTURE",
    "h1_context": "BASIC_DIRECTIONAL_LOCATION",
    "m15": {"liquidity_lookback": 5, "sweep_depth_atr": 0.10, "reclaim_delay": 2,
             "displacement_body_atr": 0.50, "displacement_window": 5, "bos_delay": 2,
             "structure_lookback": 5, "confirmation": "CLOSE"},
    "m5": {"family": "MICRO_BOS_CHOCH", "reference": "M5_MICRO_STRUCTURE", "depth": 0.15,
           "expiration": 5, "stop_anchor": "M5_SWEEP_EXTREME", "stop_buffer_atr": 0.10,
           "target_r": 1.25, "max_hold_minutes": 120},
}
