-- A channel must not inherit the news that already existed before it existed.
--
-- The bug the owner hit: adding a fourth channel made the sender enqueue the
-- WHOLE archive to it, because ensure_pending_deliveries asks "does this
-- article have an active channel with no delivery row yet?" — and a brand new
-- channel has no delivery rows for anything, ever. The owner asked for the
-- opposite: "when I create a new channel, don't send the previous news in the
-- database from the first. From now on, whenever I create a new thesis to be
-- generated news by the model, send the news for it simultaneously with the
-- other channels."
--
-- channels.activated_at is the fix. An article is a delivery target for a
-- channel only when the article was created at or after the channel was
-- activated, so:
--
--   * a channel added today receives news generated from now on;
--   * switching a channel OFF and back ON moves its activated_at forward, so
--     re-enabling does not resurrect the backlog either;
--   * existing channels keep their historical meaning: activated_at is seeded
--     from created_at rather than from now(), so nothing that is already
--     published changes shape.
--
-- Additive only: a new nullable-then-filled column, no data dropped. The
-- activation rule is enforced in app/messaging/dispatcher.py, which is the one
-- place the delivery decision is made -- resolve_channels() in Python and
-- _has_undelivered_channel() in SQL must agree, exactly as they already did.

ALTER TABLE channels ADD COLUMN IF NOT EXISTS activated_at TIMESTAMP;

UPDATE channels SET activated_at = created_at WHERE activated_at IS NULL;

ALTER TABLE channels ALTER COLUMN activated_at SET DEFAULT now();

ALTER TABLE channels ALTER COLUMN activated_at SET NOT NULL;

COMMENT ON COLUMN channels.activated_at IS
  'News generated at or after this moment is delivered here. Refreshed when the channel is switched ON, so a new or re-enabled channel never inherits the backlog.';