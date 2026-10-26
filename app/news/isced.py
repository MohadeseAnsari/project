"""Canonical ISCED-F 2013 intermediate fields — the classification vocabulary.

The 29 exact Persian names below are the ONLY categories the LLM may answer
with (plus the ``other`` fallback). Every name must match the migration
(``migrations/023_isced_categories.sql``) character for character — ZWNJ
(U+200C) included — because the worker rejects any name that is not a live,
non-deprecated row in the ``categories`` table, and tests compare both sides.

The prompt block is built once at import time so the system prompt stays a
static string (an ``Agent`` is cached per system prompt).
"""

#: ``(name, description)`` tuples in ISCED-F 2013 order.
ISCED_CATEGORIES: list[tuple[str, str]] = [
    ("برنامه‌ها و مدارک پایه", "برنامه‌های آموزشی عمومی و پایه که پایه سواد و مهارت‌های اولیه را فراهم می‌کنند"),
    ("سواد و مهارت‌های پایه حساب", "خواندن، نوشتن، حساب کردن و مهارت‌های پایه یادگیری"),
    ("مهارت‌های فردی و رشد شخصی", "توسعه مهارت‌های شخصی، رفتاری و اجتماعی"),
    ("علوم تربیتی و آموزش", "تعلیم و تربیت، آموزش‌وپرورش، برنامه‌ریزی درسی، آموزش معلمان"),
    ("هنر", "هنرهای تجسمی، نمایشی، موسیقی، صنایع دستی، طراحی و رسانه‌های هنری"),
    ("علوم انسانی (به‌جز زبان‌ها)", "فلسفه، الهیات، تاریخ، باستان‌شناسی، مطالعات فرهنگی، دین"),
    ("زبان‌ها", "آموزش و پژوهش در زبان‌های خارجی، زبان‌شناسی، ادبیات"),
    ("علوم اجتماعی و رفتاری", "جامعه‌شناسی، روان‌شناسی، علوم سیاسی، انسان‌شناسی"),
    ("روزنامه‌نگاری و اطلاع‌رسانی", "روزنامه‌نگاری، رسانه، کتابداری، آرشیو، موزه‌داری"),
    ("کسب‌وکار و مدیریت", "مدیریت، بازرگانی، حسابداری، مالی، بازاریابی، کارآفرینی"),
    ("حقوق", "حقوق، فقه، قضا، حقوق بین‌الملل، جرم‌شناسی"),
    ("علوم زیستی و رشته‌های وابسته", "زیست‌شناسی، بیوشیمی، میکروبیولوژی، ژنتیک، زیست‌فناوری"),
    ("محیط زیست", "علوم محیط زیست، بوم‌شناسی، حفاظت از طبیعت، مدیریت پسماند"),
    ("علوم فیزیکی", "فیزیک، شیمی، نجوم، زمین‌شناسی، علوم جوی"),
    ("ریاضیات و آمار", "ریاضی، آمار، تحلیل عددی، ریاضیات کاربردی"),
    ("فناوری اطلاعات و ارتباطات", "علوم کامپیوتر، مهندسی نرم‌افزار، شبکه، امنیت، هوش مصنوعی"),
    ("مهندسی و حرفه‌های مهندسی", "مهندسی برق، مکانیک، عمران، مواد، هوافضا، شیمی"),
    ("تولید و فرآوری", "تولید صنعتی، فرآوری مواد، کنترل کیفیت، اتوماسیون"),
    ("معماری و ساخت‌وساز", "معماری، شهرسازی، عمران، نقشه‌کشی، مدیریت ساخت"),
    ("کشاورزی", "زراعت، باغبانی، دامپروری، گیاه‌پزشکی، خاک‌شناسی"),
    ("جنگلداری", "جنگل‌داری، مدیریت جنگل، حفاظت از جنگل"),
    ("شیلات", "شیلات، آبزی‌پروری، مدیریت منابع آبزی"),
    ("دامپزشکی", "دامپزشکی، پزشکی حیوانات، بیماری‌های دامی"),
    ("بهداشت", "پزشکی، پرستاری، دندانپزشکی، داروسازی، مامایی، بهداشت عمومی"),
    ("رفاه اجتماعی", "مددکاری اجتماعی، مشاوره، خدمات اجتماعی، رفاه"),
    ("خدمات شخصی", "خدمات گردشگری، هتلداری، آرایشگری، خدمات خانگی"),
    ("خدمات بهداشت و ایمنی شغلی", "بهداشت حرفه‌ای، ایمنی کار، ارگونومی"),
    ("خدمات امنیتی", "امنیت، پلیس، آتش‌نشانی، دفاع شهروندی"),
    ("خدمات حمل‌ونقل", "حمل‌ونقل، دریانوردی، هوانوردی، رانندگی"),
]

#: The fallback category: everything that fits none of the 29 fields. It is a
#: real row (records can reference it) but never auto-publishes — a record
#: whose only classification is ``other`` waits for a reviewer.
OTHER_CATEGORY = "other"

#: Exactly the names the LLM is allowed to answer with.
ISCED_CATEGORY_NAMES: list[str] = [name for name, _desc in ISCED_CATEGORIES] + [OTHER_CATEGORY]


def _category_block() -> str:
    """Numbered ``name — description`` list for the system prompt."""
    return "\n".join(
        f"{number}. {name} — {description}"
        for number, (name, description) in enumerate(ISCED_CATEGORIES, start=1)
    )
