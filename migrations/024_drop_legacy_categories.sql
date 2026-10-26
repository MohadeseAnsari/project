-- Remove the pre-ISCED rule categories for good.
--
-- Migration 023 kept the retired rule categories as `deprecated = TRUE`
-- rows so historical FKs could resolve. They are no longer part of the
-- vocabulary the LLM answers with, so this migration detaches their data and
-- deletes the rows:
--
--   * fields.category_id is remapped to the closest ISCED-F 2013 category
--     (it is legacy metadata, but losing the grouping would empty the panel's
--     field buckets);
--   * main.category_id / deliveries.category_id are remapped the same way,
--     which keeps the 3 legacy records and their delivery history readable;
--   * fields in categories with no honest ISCED equivalent (علوم پایه,
--     ورزش و تربیت بدنی, and the sports/rehab entries that sat in the old
--     education bucket) become NULL — the new design explicitly allows an
--     unclassified field row;
--   * category_channel_routes and main_category_matches go with the rows via
--     ON DELETE CASCADE.
--
-- Everything happens in the migration runner's transaction, so a failure
-- leaves the old vocabulary intact. The mapping is name-based because IDs are
-- not stable across environments.

CREATE TEMP TABLE legacy_category_map (
    old_name TEXT PRIMARY KEY,
    new_name TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO legacy_category_map (old_name, new_name) VALUES
    ('پزشکی و علوم سلامت',       'بهداشت'),
    ('مهندسی و فناوری',          'مهندسی و حرفه‌های مهندسی'),
    ('هنر و معماری',             'هنر'),  -- architecture split out below
    ('علوم اجتماعی و اقتصاد',    'علوم اجتماعی و رفتاری'),
    ('علوم انسانی',              'علوم انسانی (به‌جز زبان‌ها)'),
    ('علوم تربیتی و روان‌شناسی',  'علوم تربیتی و آموزش'),  -- psychology split below
    ('کامپیوتر و فناوری اطلاعات', 'فناوری اطلاعات و ارتباطات'),
    ('حقوق و علوم سیاسی',        'حقوق'),
    ('کشاورزی و منابع طبیعی',    'کشاورزی'),
    ('رسانه و ارتباطات',         'روزنامه‌نگاری و اطلاع‌رسانی'),
    ('علوم کامپیوتر',            'فناوری اطلاعات و ارتباطات');

-- "هنر و معماری" covers two ISCED fields. Architecture-looking names move to
-- معماری و ساخت‌وساز; the rest still fall through to هنر via the map above.
-- (Conservation of artefacts is deliberately left as art, not architecture.)
UPDATE fields f
SET category_id = target.id
FROM categories old
JOIN categories target ON target.name = 'معماری و ساخت‌وساز'
WHERE f.category_id = old.id
  AND old.name = 'هنر و معماری'
  AND (
        f.name LIKE '%معماری%'
     OR f.name LIKE '%شهرسازی%'
     OR f.name LIKE '%شهر%'
     OR f.name LIKE '%ابنیه%'
     OR f.name LIKE '%بافت%'
     OR f.name LIKE '%بازسازی%'
  );

-- "علوم تربیتی و روان‌شناسی" also mixes two fields. Sports/rehab entries that
-- were filed there have no ISCED equivalent at all, so they are detached
-- rather than guessed into بهداشت.
UPDATE fields f
SET category_id = NULL
FROM categories old
WHERE f.category_id = old.id
  AND old.name = 'علوم تربیتی و روان‌شناسی'
  AND (
        f.name LIKE '%تربیت بدنی%'
     OR f.name LIKE '%ورزش%'
     OR f.name LIKE '%توانبخشی%'
     OR f.name LIKE '%ارتوپدی%'
     OR f.name LIKE '%کاردیولوژی%'
  );

-- Psychology, counselling and cognitive science belong to the social/behavioural
-- field, not to education.
UPDATE fields f
SET category_id = target.id
FROM categories old
JOIN categories target ON target.name = 'علوم اجتماعی و رفتاری'
WHERE f.category_id = old.id
  AND old.name = 'علوم تربیتی و روان‌شناسی'
  AND (
        f.name LIKE '%روان%'
     OR f.name LIKE '%مشاور%'
     OR f.name LIKE '%شناختی%'
  );

-- Categories with no honest equivalent: leave the fields uncategorized.
UPDATE fields f
SET category_id = NULL
FROM categories old
WHERE f.category_id = old.id
  AND old.name IN ('علوم پایه', 'ورزش و تربیت بدنی');

-- Remap everything still attached to a legacy category.
UPDATE fields f
SET category_id = target.id
FROM categories old
JOIN legacy_category_map m ON m.old_name = old.name
JOIN categories target ON target.name = m.new_name
WHERE f.category_id = old.id;

UPDATE main rec
SET category_id = target.id
FROM categories old
JOIN legacy_category_map m ON m.old_name = old.name
JOIN categories target ON target.name = m.new_name
WHERE rec.category_id = old.id;

UPDATE deliveries d
SET category_id = target.id
FROM categories old
JOIN legacy_category_map m ON m.old_name = old.name
JOIN categories target ON target.name = m.new_name
WHERE d.category_id = old.id;

-- Safety net: anything left pointing at a retired row becomes uncategorized
-- (deliveries would also go NULL via ON DELETE SET NULL).
UPDATE fields f
SET category_id = NULL
WHERE f.category_id IN (SELECT id FROM categories WHERE deprecated);

UPDATE main rec
SET category_id = NULL
WHERE rec.category_id IN (SELECT id FROM categories WHERE deprecated);

UPDATE deliveries d
SET category_id = NULL
WHERE d.category_id IN (SELECT id FROM categories WHERE deprecated);

DELETE FROM categories WHERE deprecated;

-- No retired rows may survive (custom categories are allowed, so the total is
-- not fixed at 30).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM categories WHERE deprecated) THEN
        RAISE EXCEPTION 'legacy categories survived migration 024';
    END IF;
END $$;
