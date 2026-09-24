-- Keep the canonical /signals projection bounded when hydrating publication state.
-- Without this lookup index, the lateral publication join scans the entire outbox
-- once per returned signal and can exceed the Console request deadline.
CREATE INDEX IF NOT EXISTS outbox_signal_publication_lookup_idx
    ON platform.outbox_events (aggregate_type, aggregate_id, event_type, created_at DESC, event_id)
    INCLUDE (publish_status, published_at);
