"""Immutable display taxonomy. Never changes identity, evidence or execution."""
from functools import lru_cache
import json
from pathlib import Path

@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name('family_catalog.json').read_text())


def family_for(strategy_id):
    # LIBRARY: is an existing catalog alias, not a new strategy identity.
    key=str(strategy_id or '').removeprefix('LIBRARY:')
    return catalog()['strategies'].get(key,'Unclassified')


def family_strategy_ids(family):
    known=catalog()['strategies']
    if family!='Unclassified' and family not in known.values():raise ValueError('Unknown strategy family')
    return [alias for sid,value in known.items() if family=='Unclassified' or value==family for alias in (sid,'LIBRARY:'+sid)]
