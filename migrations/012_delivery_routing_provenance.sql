-- Delivery routing provenance (explainable sends).
--
-- Purpose: each delivery records which category and which route level
-- produced it (`route_source` is one of 'category', 'field', 'fallback'),
-- snapshotted at queue time so later route edits cannot rewrite history.
-- Additive only: two nullable columns, existing rows keep NULL (shown as
-- "—" in the panel) — no data loss, no backfill of pre-provenance sends.

ALTER TABLE deliveries ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE deliveries ADD COLUMN route_source VARCHAR(20);
