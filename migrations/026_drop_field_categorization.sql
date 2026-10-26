-- Field-level classification is retired.
--
-- Classification is decided by the LLM during generation and stored in
-- main_category_matches; a reviewer can replace those matches manually on the
-- record. The fields table now only provides the stable dedup identity for the
-- thesis discipline text (name/normalized_name/source), so the legacy category
-- columns and the per-field channel routing table have no live consumer.
--
-- main.field_id is intentionally kept: it links a record to that dedup
-- identity. It is not used for categorization or routing.

DROP TABLE IF EXISTS field_channel_routes;

ALTER TABLE fields
    DROP COLUMN IF EXISTS category_id,
    DROP COLUMN IF EXISTS matched_keyword,
    DROP COLUMN IF EXISTS category_source;
