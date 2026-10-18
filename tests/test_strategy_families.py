from copy import deepcopy
from unittest.mock import patch
import pytest
from core.strategies.families import family_for,family_strategy_ids,catalog
from strategy_backtest.library.catalog import DEFINITIONS,EXISTING
from strategy_backtest.library.expansion import DEFINITIONS as NEW
from platform_api.signals import CanonicalSignalRepository,_project,_project_lite


def test_registry_covers_catalog_without_guessing_unknown_ids():
    for d in DEFINITIONS+NEW:assert family_for(d.strategy_id)==d.family
    for e in EXISTING:assert family_for(e['strategy_id'])==e['family']
    assert family_for('LIBRARY:ICT_JUDAS_SWING')=='ICT'
    assert family_for('ICT_NOT_REGISTERED')=='Unclassified'
    assert family_for('PRICE_ACTION_SUPPORT_RESISTANCE_RETEST')=='Price Action'
    assert family_for('KOJO_STRUCTURE_RECLAIM_V3')=='Price Action'


def test_projection_adds_display_family_without_rewriting_identity_or_payload():
    row={'strategy_id':'SMC_ORDER_BLOCK_RETRACEMENT','strategy_version':'V2','parameter_set_ref':'frozen','strategy_metadata':{'family':'UNTRUSTED_OVERRIDE'}}
    before=deepcopy(row)
    for project in (_project,_project_lite):
        out=project(row)
        assert out['strategy_family']=='SMC' and out['strategy_version']=='V2' and out['parameter_set_ref']=='frozen'
    assert row==before

@pytest.mark.parametrize('family',['ICT','SMC','Unclassified'])
def test_family_filter_applies_before_page_and_count_with_bound_ids(family):
    repo=CanonicalSignalRepository()
    with patch.object(repo,'_query',return_value=[]) as query,patch.object(repo,'_count',return_value=0) as count,patch.object(repo,'_execution_audit',return_value={}):
        repo.list_signals({'family':family,'limit':'25','offset':'25'})
    sql,args=query.call_args.args
    assert ('<> ALL(%s)' if family=='Unclassified' else '= ANY(%s)') in sql
    assert args[0]==family_strategy_ids(family) and args[-2:]==(26,25)
    assert count.call_args.args[1]==(family_strategy_ids(family),)
    with pytest.raises(ValueError):repo.list_signals({'family':"ICT' OR true"})
