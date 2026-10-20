"""Explicit forward runtime dispatch; default offline catalog and V1 keys stay frozen."""
from .selection import DEFINITIONS_BY_SELECTOR
from .donchian_v2 import DEFINITION, DonchianV2Runtime
from .runtime import ComposedResearchRuntime

DONCHIAN_V2_KEY = 'donchian_breakout@V2'
DEFINITIONS_BY_FORWARD_SELECTOR = {**DEFINITIONS_BY_SELECTOR, DONCHIAN_V2_KEY: DEFINITION}


def resolve_forward_definition(key):
    try:return DEFINITIONS_BY_FORWARD_SELECTOR[key]
    except KeyError:raise ValueError('Unknown explicit forward strategy selector') from None


def forward_runtime(key, *, dataset_id, artifact_digest):
    definition = resolve_forward_definition(key)
    if key == DONCHIAN_V2_KEY:
        return DonchianV2Runtime(dataset_id=dataset_id,artifact_digest=artifact_digest)
    return ComposedResearchRuntime(definition,dataset_id=dataset_id,artifact_digest=artifact_digest)
