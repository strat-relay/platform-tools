"""Additive, opt-in research versions. Frozen original catalog is unchanged."""
from .catalog import Definition

DEFINITIONS = (
    Definition('judas_swing','ICT','Judas Swing','judas_sequence',
        'First London session sweep, later reclaim, then later displacement and five-bar close break; opposite range target.',
        version='V2',session_tz='Europe/London',range_start=0,range_end=300,trade_start=420,trade_end=600,target_policy='OPPOSITE_RANGE'),
    Definition('power_of_three','ICT','Power of Three','po3_sequence',
        'New York accumulation, first manipulation sweep, later reclaim, then displacement beyond opposite accumulation edge.',
        session_tz='America/New_York',range_start=0,range_end=360,trade_start=420,trade_end=720),
    Definition('order_block_retracement','SMC','Order Block Retracement','order_block_sequence',
        'Confirmed H1 structural bias; displacement breaks prior channel; last opposing candle forms block; first later touch must reject.',version='V2'),
)
BY_REF={f'{d.strategy_id}@{d.version}':d for d in DEFINITIONS}
