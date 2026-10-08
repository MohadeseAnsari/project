-- Real model configuration for the review panel.
--
-- Purpose: let the panel choose the LLM per deployment without code changes.
-- `api_key_env` stores the ENVIRONMENT VARIABLE NAME holding the key (never
-- the key itself); `base_url` overrides the provider endpoint per model.
-- `temperature`/`max_output_tokens` tune generation; `description` is a human
-- note for the panel. Additive only: new nullable columns, no existing rows
-- touched.

ALTER TABLE models ADD COLUMN base_url VARCHAR(500);
ALTER TABLE models ADD COLUMN api_key_env VARCHAR(100);
ALTER TABLE models ADD COLUMN temperature DOUBLE PRECISION;
ALTER TABLE models ADD COLUMN max_output_tokens INTEGER;
ALTER TABLE models ADD COLUMN description TEXT;
