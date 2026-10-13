-- 016: full record lifecycle statuses (approved, published).
--
-- Records previously stayed status='review' after approval, distinguished only
-- by approved_at/published_at timestamps. That is why the panel kept showing
-- "نیازمند تأیید" for records whose deliveries were already sent: the status
-- column never told the truth. This migration makes the lifecycle explicit:
-- ready -> processing -> review -> approved -> published (+ rejected, failed).
--
-- Applied migrations are never edited: the old CHECK is dropped and a wider
-- one added here (additive — existing rows keep their values, then the
-- backfill moves already-approved/published rows to their true states so the
-- KPI counts are exact from the first boot after upgrade.

ALTER TABLE main DROP CONSTRAINT ck_main_status;

ALTER TABLE main ADD CONSTRAINT ck_main_status
    CHECK (status IN ('ready', 'processing', 'review', 'approved', 'published', 'failed', 'rejected'));

-- Backfill in lifecycle order: a row with published_at is published (it was
-- delivered); otherwise a row with approved_at is approved.
UPDATE main SET status = 'published' WHERE status = 'review' AND published_at IS NOT NULL;
UPDATE main SET status = 'approved'  WHERE status = 'review' AND approved_at  IS NOT NULL;
