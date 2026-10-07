-- Normalize the unversioned event type written by authority_store.py before the
-- .v1 suffix was enforced.  NATS calls this value a subject, but the canonical
-- outbox schema stores it in event_type; there is no subject column.
UPDATE platform.outbox_events
   SET event_type = 'system.status_changed.v1'
 WHERE event_type = 'system.status_changed';
