-- Explicit publishing timestamps on records (no new status).
--
-- Purpose: `approve` keeps main.status = 'review' so the state machine is
-- untouched (CHECK constraint unchanged), but approved articles must be
-- distinguishable from unreviewed ones. `approved_at` is stamped on approval;
-- `published_at` is stamped by the sender on the first successful send.
-- "Needs review" therefore means status='review' AND approved_at IS NULL.
-- Additive only: two nullable columns, existing rows keep NULL.
ALTER TABLE main ADD COLUMN approved_at TIMESTAMP;
ALTER TABLE main ADD COLUMN published_at TIMESTAMP;
