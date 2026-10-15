-- Human-confirmed field categories (rules suggest, humans decide).
--
-- Purpose: `category_source` marks whether a row's category came from the
-- rules engine ('rules', the default for every existing row) or was frozen
-- by a human ('curated'). Re-classification permanently skips curated rows,
-- so reviewer decisions survive rule updates. Additive only: one column with
-- a default, no existing rows touched.
ALTER TABLE fields ADD COLUMN category_source VARCHAR(20) NOT NULL DEFAULT 'rules';
