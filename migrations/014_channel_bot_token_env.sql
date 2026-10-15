-- Per-channel bot token override (variable name, never the token).
--
-- Purpose: a second channel on the same platform can use its own bot without
-- code changes. The column stores the ENVIRONMENT VARIABLE NAME holding the
-- token; the platform-wide BALE_/TELEGRAM_BOT_TOKEN stays the fallback.
-- Additive only: one nullable column, existing rows keep NULL (fallback).
ALTER TABLE channels ADD COLUMN bot_token_env VARCHAR(100);
