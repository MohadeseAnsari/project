-- Item 6: every active channel is a delivery target.
--
-- The owner redefined the core logic: "when I write a thesis and the model
-- generates the news, the news should be sent to all channels". The previous
-- chain (category route, else field route, else all active) meant that adding
-- a category->channel mapping SILENTLY STOPPED delivery to every other
-- channel, which is the opposite of the stated intent.
--
-- The tables stay: category_channel_routes keeps recording which channel is
-- each category's designated news channel (shown in the panel, used as
-- delivery provenance), and field_channel_routes is simply no longer read.
-- Dropping them would be a destructive migration for no functional gain.
--
-- route_source gains a value: 'all' for a channel reached by broadcast rather
-- than by a category mapping. deliveries.route_source is a free VARCHAR(20)
-- with no CHECK constraint, so no constraint change is needed.

COMMENT ON COLUMN deliveries.route_source IS
  'Provenance: category = this category''s designated channel, all = broadcast to every active channel.';
