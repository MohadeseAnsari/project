-- Store the matched keyword for each field (explainable classification).
--
-- Purpose: `classify_with_reason` returns (category, keyword, rule); this
-- column persists the keyword so the panel can show WHY a field got its
-- category without re-running the classifier. Additive only: one nullable
-- column, no existing rows touched (backfill via scripts/reclassify_fields.py).
ALTER TABLE fields ADD COLUMN matched_keyword VARCHAR(200);
