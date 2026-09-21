-- P2 signal lifecycle substrate.  Legacy files remain authoritative; these
-- columns are shadow records until a separately approved signal cutover.
ALTER TABLE strategy.candidates ADD COLUMN IF NOT EXISTS lifecycle_state text NOT NULL DEFAULT 'CANDIDATE_DETECTED';
ALTER TABLE strategy.candidates DROP CONSTRAINT IF EXISTS candidates_lifecycle_state_check;
ALTER TABLE strategy.candidates ADD CONSTRAINT candidates_lifecycle_state_check
    CHECK (lifecycle_state IN ('CANDIDATE_DETECTED','EVALUATED','REJECTED','ENTRY_SIGNAL_CREATED'));

ALTER TABLE strategy.signals ADD COLUMN IF NOT EXISTS lifecycle_state text NOT NULL DEFAULT 'ENTRY_SIGNAL_CREATED';
ALTER TABLE strategy.signals DROP CONSTRAINT IF EXISTS signals_lifecycle_state_check;
ALTER TABLE strategy.signals ADD CONSTRAINT signals_lifecycle_state_check
    CHECK (lifecycle_state IN ('CANDIDATE_DETECTED','EVALUATED','REJECTED','ENTRY_SIGNAL_CREATED'));
