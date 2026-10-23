"""Database-driven policy, independent of strategy names; fake broker only."""
import unittest

from execution_v2.eligibility import instance_policy_reason, research_restricted
from execution_v2.fakes import FakeBroker
from tests.test_execution_v2_worker import seeded_conn, make_worker, run


def record(strategy='arbitrary-record'):
    return {'strategy_id': strategy, 'source_provenance': {'execution_mode_revision': 3}}


def approved_row(strategy='arbitrary-record', **changes):
    return {'declared_strategy_id': strategy, 'online': True, 'parent_enabled': True,
            'execution_mode': 'LIVE', 'execution_eligible': True, 'mode_revision': 3,
            'attributes': {}, 'parent_attributes': {}, **changes}


class DataDrivenExecutionPolicyTests(unittest.TestCase):
    def test_arbitrary_strategy_identity_does_not_change_policy(self):
        for sid in ('renamed-v7', 'new-plugin', 'any-other-record'):
            self.assertIsNone(instance_policy_reason(record(sid), [approved_row(sid)]))

    def test_missing_and_ambiguous_policy_deny(self):
        self.assertEqual(instance_policy_reason(record(), []), 'INSTANCE_EXECUTION_POLICY_MISSING')
        self.assertEqual(instance_policy_reason(record(), [approved_row(), approved_row()]),
                         'INSTANCE_EXECUTION_POLICY_AMBIGUOUS')

    def test_gate_values_and_current_revision_are_enforced(self):
        for changes, reason in [({'declared_strategy_id': 'another'}, 'INSTANCE_STRATEGY_IDENTITY_MISMATCH'),
                                ({'online': False}, 'INSTANCE_OR_PARENT_DISABLED'),
                                ({'parent_enabled': False}, 'INSTANCE_OR_PARENT_DISABLED'),
                                ({'execution_mode': 'SHADOW'}, 'INSTANCE_NOT_LIVE'),
                                ({'execution_eligible': False}, 'INSTANCE_NOT_EXECUTION_ELIGIBLE'),
                                ({'execution_eligible': None}, 'INSTANCE_NOT_EXECUTION_ELIGIBLE'),
                                ({'mode_revision': 2}, 'INSTANCE_EXECUTION_REVISION_MISMATCH')]:
            with self.subTest(changes=changes):
                self.assertEqual(instance_policy_reason(record(), [approved_row(**changes)]), reason)

    def test_generic_research_restrictions_are_not_strategy_specific(self):
        for doc in ({'research_only': True}, {'shadow_only': True}, {'production_eligible': False},
                    {'execution_eligible': False}, {'classification': 'RETROSPECTIVE_RESEARCH_EVIDENCE'}):
            self.assertTrue(research_restricted(doc))
            self.assertEqual(instance_policy_reason(record(), [approved_row(parent_attributes=doc)]),
                             'STRATEGY_NOT_EXECUTION_ENABLED')
        self.assertTrue(research_restricted({'research_only': 'false'}))
        self.assertTrue(research_restricted({'execution_eligible': None}))

    def assert_no_broker_effect(self, conn):
        broker = FakeBroker(mode='fill')
        outcome = run(make_worker(conn, broker=broker), conn, broker)
        self.assertEqual(outcome.status, 'BLOCKED')
        self.assertEqual(broker.calls, 0)
        self.assertEqual(len(conn.tables['execution_v2.execution_intent']), 0)
        self.assertEqual(len(conn.tables['execution_v2.execution_attempt']), 0)
        return outcome

    def test_missing_policy_cannot_insert_intent_or_reach_broker(self):
        conn = seeded_conn(strategy_id='previously-unseen')
        conn.instance_policies.clear()
        self.assertEqual(self.assert_no_broker_effect(conn).detail, 'INSTANCE_EXECUTION_POLICY_MISSING')

    def test_ineligible_policy_cannot_insert_intent_or_reach_broker(self):
        conn = seeded_conn(strategy_id='previously-unseen')
        key = ('previously-unseen', 'fixture-instance')
        row = list(conn.instance_policies[key][0]); row[3] = False
        conn.instance_policies[key] = [tuple(row)]
        self.assertEqual(self.assert_no_broker_effect(conn).detail, 'INSTANCE_NOT_EXECUTION_ELIGIBLE')

    def test_current_approval_cannot_override_immutable_research_evidence(self):
        conn = seeded_conn(strategy_metadata={'research_only': True})
        self.assertEqual(self.assert_no_broker_effect(conn).detail, 'STRATEGY_NOT_EXECUTION_ENABLED')

    def test_old_shadow_signal_remains_blocked_with_current_live_policy(self):
        conn = seeded_conn(source_provenance={'execution_mode_at_decision': 'SHADOW',
                                             'execution_mode_revision': 0},
                           strategy_metadata={'execution_mode': 'SHADOW', 'execution_mode_revision': 0})
        self.assertEqual(self.assert_no_broker_effect(conn).detail, 'SIGNAL_NOT_LIVE_AT_DECISION')


if __name__ == '__main__':
    unittest.main()
