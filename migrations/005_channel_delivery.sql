-- Outbound channel delivery.
--
-- Purpose: track WHICH channel each generated article was published to, and
-- fan a single field out to one or more channels over time.
--
-- Design notes:
-- - `channels` holds a platform + destination chat id. No credentials are
--   stored here: bot tokens live in configuration (app/config.py), because a
--   channel is a destination, not a credential.
-- - `field_channel_routes` is the one-to-many field -> channel mapping. Today
--   every field routes to the single Bale channel; adding Telegram (or a
--   second Bale channel) later is a row insert, not a code change.
-- - `deliveries` is the per-(article, channel) send record. It is the unit the
--   sender Worker claims, so "processed but not sent" is a query over this
--   table rather than a flag on `main` (which would not survive one field
--   fanning out to several channels).
--
-- The `awaiting_confirmation` status is intentionally present but unused: it
-- reserves a slot for the planned human-in-the-loop confirmation step so the
-- enum does not have to change when that flow lands.

CREATE TABLE channels (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(20) NOT NULL,
    chat_id BIGINT NOT NULL,
    username VARCHAR(255),
    title VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT ck_channels_platform CHECK (platform IN ('bale', 'telegram')),
    CONSTRAINT uq_channels_platform_chat UNIQUE (platform, chat_id)
);

CREATE TABLE field_channel_routes (
    field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (field_id, channel_id)
);

CREATE INDEX ix_field_channel_routes_channel_id ON field_channel_routes (channel_id);

CREATE TABLE deliveries (
    id SERIAL PRIMARY KEY,
    main_id INTEGER NOT NULL REFERENCES main(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    platform_message_id BIGINT,
    error_message TEXT,
    claimed_at TIMESTAMP,
    next_attempt_at TIMESTAMP NOT NULL DEFAULT now(),
    sent_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    -- One delivery per (article, channel): re-running the dispatcher can never
    -- duplicate a pending send for a destination that already has one.
    CONSTRAINT uq_deliveries_main_channel UNIQUE (main_id, channel_id),
    CONSTRAINT ck_deliveries_status CHECK (
        status IN ('pending', 'sending', 'sent', 'failed', 'awaiting_confirmation')
    )
);

-- The sender Worker's claim query filters on (status, next_attempt_at) and
-- orders FIFO, so it needs exactly this index.
CREATE INDEX ix_deliveries_status_next_attempt ON deliveries (status, next_attempt_at);
CREATE INDEX ix_deliveries_channel_id ON deliveries (channel_id);

-- Seed the single Bale channel this deployment publishes to today.
INSERT INTO channels (platform, chat_id, username, title)
VALUES ('bale', 5371341124, '@newschanneltest123', 'IranDoc News');

-- Every field routes to that one channel. This is the "all fields -> one
-- channel" state the project starts in; per-field routes are inserted later to
-- split fields across channels.
INSERT INTO field_channel_routes (field_id, channel_id)
SELECT f.id, c.id
FROM fields f
CROSS JOIN channels c
WHERE c.platform = 'bale'
  AND c.chat_id = 5371341124
ON CONFLICT DO NOTHING;
