-- Keep the two English category descriptions out of the Persian panel.
UPDATE categories SET description = 'پزشکی حیوانات، بهداشت دام، تشخیص و درمان بیماری‌های حیوانی'
WHERE name = 'دامپزشکی';

UPDATE categories SET description = 'بدون تطابق با دسته‌های ISCED؛ به‌طور خودکار منتشر نمی‌شود.'
WHERE name = 'other';
