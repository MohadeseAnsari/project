-- 018: allow the delivery state deleted_from_channel.
--
-- The state machine already declared it (app/services/transitions.py) but the
-- column's CHECK constraint (migration 005) did not, so an operator recording
-- "the published message was deleted from the channel" would have hit a
-- database error instead of a status change. Applied migrations are never
-- edited: the constraint is dropped and widened here.
--
-- No data change: every existing row keeps its status.

ALTER TABLE deliveries DROP CONSTRAINT ck_deliveries_status;

ALTER TABLE deliveries ADD CONSTRAINT ck_deliveries_status
    CHECK (status IN (
        'awaiting_confirmation', 'pending', 'sending', 'sent',
        'failed', 'deleted_from_channel'
    ));