-- Rename the unversioned subject written by authority_store.py before the .v1
-- suffix was enforced.  The six affected outbox_events rows were stuck in FAILED
-- state (unknown subject); renaming them to the registered .v1 subject lets the
-- relay retry and publish them normally.
UPDATE platform.outbox_events
   SET subject = 'system.status_changed.v1'
 WHERE subject = 'system.status_changed';
