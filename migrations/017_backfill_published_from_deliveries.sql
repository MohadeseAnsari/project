-- 017: backfill the lifecycle for articles delivered before published_at existed.
--
-- Migration 016 backfilled from the timestamps, but those timestamps only
-- cover sends that happened after they were introduced (013). Deliveries
-- that were already 'sent' back then left their record sitting in 'review'
-- with no published_at — exactly the state the panel used to render as
-- "نیازمند تأیید" although nothing was pending at all (43 rows here).
--
-- Derive the truth from the deliveries instead: an article whose deliveries
-- are ALL sent has been published. Articles with any delivery still
-- unsent (pending/sending/failed/awaiting_confirmation) are left alone —
-- for those the state genuinely is not "published" yet.
--
-- Ordering: approved_at is stamped only for articles that were never
-- approved (they predate the review decision being recorded), and
-- published_at reuses the earliest send time so the timeline is real
-- rather than "the time this migration ran".

ALTER TABLE main DROP CONSTRAINT ck_main_status;

ALTER TABLE main ADD CONSTRAINT ck_main_status
    CHECK (status IN ('ready', 'processing', 'review', 'approved', 'published', 'failed', 'rejected'));

WITH fully_delivered AS (
    SELECT d.main_id,
           min(d.sent_at) AS first_sent_at
    FROM deliveries d
    GROUP BY d.main_id
    HAVING count(*) = count(*) FILTER (WHERE d.status = 'sent')
)
UPDATE main m
SET status = 'published',
    published_at = COALESCE(m.published_at, f.first_sent_at),
    approved_at = COALESCE(
        m.approved_at,
        (SELECT min(a.created_at)
         FROM audit_logs a
         WHERE a.main_id = m.id AND a.action = 'approve')
    )
FROM fully_delivered f
WHERE m.id = f.main_id
  AND m.status IN ('review', 'approved');