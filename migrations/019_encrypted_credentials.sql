-- 019: credentials managed in the panel, stored encrypted.
--
-- Until now a model row could only name an environment variable
-- (`api_key_env`), so adding a provider meant an env change and a restart —
-- and a row whose variable happened to hold another provider's key sent that
-- key to the new base_url (observed as an HTTP 401 from the gateway). Bot
-- tokens had the same shape.
--
-- These columns hold the secret itself, Fernet-encrypted, so an operator can
-- manage keys from the panel without touching the host. The master key is
-- never stored here: it comes from CREDENTIALS_KEY, or a 0600 gitignored file
-- generated on first boot.
--
-- `api_model` is the provider's real model id, separate from the row's
-- display `name` (which doubles as the display label).
--
-- `last_tested_at` / `last_test_ok` / `last_test_error` / `last_test_latency_ms`
-- record the outcome of the connection probe (endpoint + HTTP status only,
-- never the key).
--
-- Additive: new nullable columns, no existing row is modified here. The
-- existing `api_key_env` values are left untouched; `data/credentials_seed.py`
-- migrates any that are actually keys rather than variable names.

ALTER TABLE models ADD COLUMN api_key_encrypted TEXT;
ALTER TABLE models ADD COLUMN api_model VARCHAR(200);
ALTER TABLE models ADD COLUMN last_tested_at TIMESTAMP;
ALTER TABLE models ADD COLUMN last_test_ok BOOLEAN;
ALTER TABLE models ADD COLUMN last_test_error TEXT;
ALTER TABLE models ADD COLUMN last_test_latency_ms INTEGER;

ALTER TABLE channels ADD COLUMN bot_token_encrypted TEXT;