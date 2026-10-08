-- Category -> channel routing (the business chain made explicit).
--
-- Purpose: an article reaches a channel through field -> category ->
-- channels. `field_channel_routes` stays as the field-level exception table
-- and keeps working; this table is the primary mapping the sender consults
-- first, with field routes and then all-active channels as documented
-- fallbacks (see app/messaging/dispatcher.py).
--
-- Seed: every category routes to every currently active channel. This is
-- deliberately permissive so publishing behavior does not change on deploy;
-- the owner narrows the mapping per category in the panel afterwards.

CREATE TABLE category_channel_routes (
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (category_id, channel_id)
);

CREATE INDEX ix_category_channel_routes_channel_id ON category_channel_routes (channel_id);

INSERT INTO category_channel_routes (category_id, channel_id)
SELECT c.id, ch.id
FROM categories c
CROSS JOIN channels ch
WHERE ch.is_active
ON CONFLICT DO NOTHING;
