-- Production seed: TM-NONE-1, the first canonical TradeManagerVersion (A6 07 section 5, A7 04
-- section 2, docs/p4_2_managed_trade/README.md). Deterministic and idempotent: this is the
-- exact manifest already frozen and golden-vector-tested in trade_management/versions.py
-- (TM_NONE_1_MANIFEST) - the migration embeds its computed manifest/manifest_hash/tm_version_id
-- as literals so the seed can never silently drift from what the application code would itself
-- compute (tests/test_trade_management_versions_and_ids.py:test_golden_vector_for_tm_none_1
-- pins the same hash). Re-running this file is a no-op (ON CONFLICT DO NOTHING on the primary
-- key); apply_migrations() also only ever runs it once per checksum.
--
-- Behavior once bound: action = HOLD only, no broker effects, management publication is always
-- WITHHELD(NOT_ACTIONABLE_HOLD) (trade_management/publication_gate.py). No strategy-performance
-- claim is made or implied by registering this version.
--
-- TM-LEGACY-0 is intentionally NOT seeded here: it is not required by the first P4.2 activation
-- path (mission section 3) and remains a documented, deferred follow-up.
--
-- code_manifest carries the placeholder "computed-at-registration" for trade_management/tm_none.py,
-- matching TM_NONE_1_MANIFEST's own documented simplification (no runtime code-manifest
-- verification is implemented yet - see docs/p4_2_managed_trade/README.md's scope-reduction
-- list); swapping in a real file hash is deferred together with that verification, so the seed
-- here stays byte-identical to what tests/test_trade_management_versions_and_ids.py already
-- proves is stable.
INSERT INTO trade_management.trade_manager_version
    (tm_version_id, evaluator_id, label, manifest, manifest_hash, status)
VALUES (
    'TMV_ecaca5f080f9f79bb18cc936',
    'tm-none.v1',
    'TM-NONE-1',
    '{"action_vocabulary_version":"tm-actions.v1","arithmetic":{"r_multiple_definition":"(price-entry)/|entry-initial_stop| signed by direction","rounding":"none; no derived-float rounding performed by TM-NONE"},"code_manifest":{"trade_management/tm_none.py":"computed-at-registration"},"decision_schema_version":"trade-manager-decision.v1","evaluator_id":"tm-none.v1","manifest_schema":"tm-version-manifest.v1","observation_spec":{"ema_period":null,"ema_tolerance":null,"late_event_rule":"IGNORE_NEVER_EVALUATE","max_market_age_ms":60000,"quote_required":true,"structure_tolerance":null,"swing_lookback_left":null,"swing_lookback_right":null,"timeframes_consumed":[]},"policy_bundle":[],"price_semantics":{"reference":"close side = bid for LONG / ask for SHORT; mark = mid","version":"ps.v1"},"reason_code_registry_version":"tm-reasons.v1","resolution_table":[],"scope":{"instruments":["*"],"strategies":["*"]}}'::jsonb,
    'ecaca5f080f9f79bb18cc936fb3867420c416eb372f998f3ec7c175369b5ac0d',
    'FROZEN'
)
ON CONFLICT (tm_version_id) DO NOTHING;

-- Publication eligibility record (A7 04 section 4 "operational facts... live in append-only
-- records"): TM-NONE-1 never produces an actionable decision, so SHADOW_ONLY is definitionally
-- correct and permanent for this version - not a placeholder pending promotion.
INSERT INTO trade_management.tm_version_promotion
    (tm_version_id, publication_eligibility, decided_by, evidence_ref)
SELECT 'TMV_ecaca5f080f9f79bb18cc936', 'SHADOW_ONLY', 'migration:014_tm_none_1_seed',
       'TM-NONE-1 only ever produces HOLD; never actionable, never publishable by construction'
WHERE NOT EXISTS (
    SELECT 1 FROM trade_management.tm_version_promotion
    WHERE tm_version_id = 'TMV_ecaca5f080f9f79bb18cc936' AND publication_eligibility = 'SHADOW_ONLY'
);
