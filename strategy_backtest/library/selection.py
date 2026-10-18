"""Explicit version selectors; unqualified keys retain their frozen V1 meaning."""
from .catalog import BY_KEY
from .expansion import DEFINITIONS

EXPANSION_KEYS=tuple(d.key+'@'+d.version for d in DEFINITIONS)
DEFINITIONS_BY_SELECTOR={**BY_KEY,**{d.key+'@'+d.version:d for d in DEFINITIONS}}


def resolve_definition(selector):
    try:return DEFINITIONS_BY_SELECTOR[selector]
    except KeyError:raise ValueError('Unknown explicit library strategy selector') from None
