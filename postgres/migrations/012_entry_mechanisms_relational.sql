-- Entry mechanisms are independently queryable child facts, not a scalar or
-- JSON blob. P2 remains pre-cutoff; this creates no historical runtime rows.
ALTER TABLE strategy.entry_signals DROP COLUMN entry_mechanism;
ALTER TABLE strategy.entry_signals DROP COLUMN source_ref;
ALTER TABLE strategy.entry_signals DROP COLUMN runtime_provenance;
ALTER TABLE strategy.entry_signals
    ADD COLUMN source_id text,
    ADD COLUMN source_offset bigint CHECK (source_offset IS NULL OR source_offset >= 0),
    ADD COLUMN evidence_class text,
    ADD COLUMN cutoff_id text;

CREATE INDEX entry_signals_source_cursor_idx
    ON strategy.entry_signals (source_id, source_offset);
CREATE INDEX entry_signals_evidence_class_idx
    ON strategy.entry_signals (evidence_class);

CREATE TABLE strategy.entry_signal_mechanisms (
    entry_signal_id text NOT NULL REFERENCES strategy.entry_signals(signal_id) ON DELETE CASCADE,
    mechanism text NOT NULL CHECK (length(btrim(mechanism)) > 0),
    position integer NOT NULL CHECK (position >= 0),
    PRIMARY KEY (entry_signal_id, mechanism),
    UNIQUE (entry_signal_id, position)
);

CREATE INDEX entry_signal_mechanisms_mechanism_idx
    ON strategy.entry_signal_mechanisms (mechanism, entry_signal_id);
