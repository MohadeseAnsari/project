"""Deterministic, rule-based field classification.

Matching is token-aware, not first-match-wins: the normalized name is split
into tokens and every keyword is tested, so a short keyword can never win
from inside a longer token (``مو`` inside ``موزه``) and a more specific
phrase always beats a broader one (``صنایع دستی`` beats ``صنایع``).

Rules, in order:
1. Negative keywords veto a category outright (genuine collisions, e.g. the
   genetics compound ``مولکولاروسیتوژنتیک`` must not fall to پزشکی).
2. A multi-token keyword (``تربیت بدنی``) matches only as an exact token
   sequence.
3. A single-token keyword of 4 characters or fewer (``مو``, ``آب``) matches
   only as a WHOLE token — never as a substring of a longer token.
4. A longer single-token keyword may also match as a substring, but a
   substring match always scores below a whole-token match.
5. Specificity decides: candidates score by matched-keyword character
   length (then whole-token beats substring); the category list order below
   is ONLY the documented tie-break for equal scores.

``classify_with_reason`` returns ``(category, matched_keyword, rule_index)``
so the panel can show WHY a field got its category; ``classify_field`` is
the plain-category facade the Worker and seed script use. Unmatched fields
fall back to ``other``. The LLM is never used here.
"""

import re
import unicodedata

# Arabic/Persian letter variants folded to one form for matching only;
# storage normalization (app.fields.normalization) is untouched.
_VARIANT_MAP = str.maketrans({
    "آ": "ا", "أ": "ا", "إ": "ا",
    "ي": "ی", "ئ": "ی",
    "ك": "ک",
    "ة": "ه",
    "ؤ": "و",
    "ء": "",
    "\u200c": "", "\u200b": "", "\u200d": "",
})
_DIGIT_MAP = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_EDGE_PUNCT = "()[]{}<>«»\"'’‘.,;:!?؟…-–—/\\|*+@#$%^&~=_\xad"


def _fold_tokens(text: str) -> list[str]:
    """Fold text into match tokens: variants, digits, case, punctuation.

    ZWNJ is deleted (not a boundary), so ``برنامه‌ریزی`` and the keyword
    ``برنامهریزی`` fold identically; edge punctuation is stripped so
    ``(رادیولوژی)`` matches ``رادیولوژی``.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_DIGIT_MAP).translate(_VARIANT_MAP).lower()
    tokens = []
    for raw in text.split():
        token = raw.strip(_EDGE_PUNCT)
        if token:
            tokens.append(token)
    return tokens


def _contains_sequence(tokens: list[str], seq: list[str]) -> bool:
    """Whether ``seq`` appears as an exact contiguous subsequence of ``tokens``."""
    if len(seq) > len(tokens):
        return False
    return any(
        tokens[i:i + len(seq)] == seq for i in range(len(tokens) - len(seq) + 1)
    )


# Negative keywords veto the category when found as a folded substring.
# Kept tiny on purpose: each entry documents one genuine collision where the
# longest-match rule alone would publish to the wrong audience.
_NEGATIVES: dict[str, list[str]] = {
    # Museum management is an art discipline, not social-science management.
    "علوم اجتماعی و اقتصاد": ["موزه"],
    # A genetics compound, not a pathology department.
    "پزشکی و علوم سلامت": ["مولکولاروسیتوژنتیک"],
    # Landscape architecture is architecture, even with an engineering prefix.
    "مهندسی و فناوری": ["منظر"],
}

_RULES = [
    ("کامپیوتر و فناوری اطلاعات", [
        "هوش مصنوعی", "یادگیری ماشین", "یادگیری عمیق", "داده‌کاوی",
        "پردازش زبان طبیعی", "پردازش تصویر", "بینایی ماشین",
        "شبکه‌های عصبی", "الگوریتم", "ساختار داده", "پایگاه داده",
        "امنیت اطلاعات", "رمزنگاری", "امنیت شبکه", "حمله سایبری",
        "گواهینامه دیجیتال", "تجارت الکترونیک", "دولت الکترونیک",
        "شهر هوشمند", "اینترنت اشیا", "رایانش ابری", "رایانش لبه‌ای",
        "بلاکچین", "قرارداد هوشمند", "ارز دیجیتال",
        "اطلاعات", "حفاظت اطلاعات", "هوشبری",
    ]),
    ("مهندسی و فناوری", [
        "مهندسی", "فناوری", "مکانیک", "برق", "الکترونیک", "کامپیوتر",
        "نرم‌افزار", "شبکه", "معماری کامپیوتر", "رباتیک", "اتوماسیون",
        "صنایع", "تولید", "مواد", "متالورژی", "پلیمر", "نانو",
        "شیمیایی", "پتروشیمی", "نفت", "گاز", "انرژی", "خورشیدی", "بادی",
        "هسته‌ای", "الکترونیک قدرت", "مخابرات", "رادیو", "مایکروویو",
        "کنترل", "ابزار دقیق", "مکاترونیک", "هوافضا", "هواپیما", "فضا",
        "ساختوساز", "سازه", "زلزلی", "بتن", "آسفال", "راه", "ریلی",
        "ترافیک", "عمران", "آب", "آبفشان", "سرریز", "تصفیه", "فاضلاب",
        "محیط زیست", "پسماند", "بازیافت", "گردشگری سازه", "ساختمان",
        "فناوری معماری", "مهندسی معماری",
        "تکنولوژی", "پرتودرمانی", "تصویر برداری تشدید مغناطیسی",
        "گردش خون", "فوتونیک", "اکولو پلاستیک",
        "پدافند غیرعامل", "علوم اداری", "انتظامی",
        "تهیه‌کنندگی", "ایمنی‌شناسی",
    ]),
    ("علوم انسانی", [
        "فلسفه", "کلام", "عرفان", "تاریخ", "جغرافیا", "زبان", "ادبیات",
        "فارسی", "عربی", "انگلیسی", "ترجمه", "لغت‌شناسی", "برجسته‌شناسی",
        "فرهنگ", "هنرهای نمایشی", "ایران‌شناسی", "زبان‌شناسی",
        "رسانه‌شناسی",
        "علم اطلاعات", "کتابداری", "باستان‌شناسی", "باستان‌شناسی و هنر",
        "پژوهشهای قرآنی", "پژوهشهای اسلامی", "پژوهشهای اهل بیت",
        "پژوهشهای امام", "پژوهشهای حکمت", "پژوهشهای کلام",
        "پژوهشهای ادبی", "پژوهشهای تاریخی", "پژوهشهای هنری",
        "پژوهشهای اجتماعی", "پژوهشهای اقتصادی", "پژوهشهای سیاسی",
        "پژوهشهای زبان", "پژوهشهای ترجمه", "پژوهشهای فرهنگی",
        "پژوهشهای فلسفی", "پژوهشهای اسلامی", "پژوهشهای دینی",
        "پژوهشهای مذهبی", "پژوهشهای فقهی", "پژوهشهای اصولی",
        "پژوهشهای حدیثی", "پژوهشهای تفسیری", "پژوهشهای روایی",
        "الهیات", "معارف اسلامی", "فقه", "قرآن و علوم",
        "باستان‌سنجی",
    ]),
    ("هنر و معماری", [
        "معماری داخلی", "معماری منظر", "معماری شهری",
        "معماری", "هنر", "نقاشی", "طراحی", "گرافیک", "فیلم", "سینما",
        "موسیقی", "تئاتر", "نمایشی", "رقص", "عکاسی", "صنایع دستی",
        "سفال", "سرامیک", "منبت", "چوب", "منسوجات", "فرش", "چاپ",
        "تصویرسازی", "انیمیشن", "بازی‌سازی", "طراحی داخلی",
        "منظر", "باغ", "فضای سبز", "شهرسازی", "بازسازی", "حفظ آثار",
        "مرمت", "آثار تاریخی", "آثار باستانی", "موزه", "گالری",
        "نقد هنر", "زیبایی‌شناسی", "فلسفه هنر", "هنرهای",
        "آهنگسازی", "بازیگری", "گریم", "نمایش عروسکی", "کارگردانی نمایش",
        "ارتباط تصویری", "باستانسنجی",
    ]),
    ("علوم اجتماعی و اقتصاد", [
        "اقتصاد", "اجتماعی", "جامعه‌شناسی",
        "روابط بین‌الملل", "مدیریت", "حسابداری", "مالی", "بانکداری",
        "بازاریابی", "کسبوکار", "کارآفرینی", "توسعه", "برنامه‌ریزی",
        "منطقه‌ای", "شهری", "روستایی", "مسکن", "بهسازی",
        "توسعه پایدار", "توسعه منطقه‌ای", "گردشگری", "طبیعت‌گردی",
        "مدیریت بحران", "مدیریت شهری", "مدیریت روستایی",
        "دندان‌پزشکی اجتماعی",
        "مطالعات جهان", "دفاعی استراتژیک", "مطالعات دفاعی",
    ]),
    ("علوم تربیتی و روان‌شناسی", [
        "تربیت", "آموزش", "آموزش بهداشت", "پرورش", "مدیریت آموزشی", "برنامه‌ریزی درسی",
        "سنجش", "ارزشیابی", "روان‌شناسی", "مشاوره", "توانبخشی",
        "نیازمندان", "استعداد تحصیلی", "پرورش کودک", "کودک", "کودکان",
        "نوجوان", "روان‌درمانی", "تحلیل رفتار", "روان‌سنجی", "سنجش هوش",
        "عصب‌روان‌شناسی", "شناخت", "یادگیری", "انگیزه", "خلاقیت",
    ]),
    ("پزشکی و علوم سلامت", [
        "پزشکی", "دندانپزشکی", "پرستاری", "مامایی", "داروسازی",
        "فیزیوتراپی", "رادیولوژی", "آزمایشگاهی", "بیماری", "جراحی",
        "اطفال", "زنان", "روانپزشکی", "چشم", "گوش", "حلق", "بینی", "مغز",
        "اعصاب", "قلب", "عروق", "استخوان", "مفصل", "پوست", "مو", "تغذیه",
        "سلامت", "بهداشت", "اپیدمیولوژی", "آناتومی", "فیزیولوژی", "شیمی بالینی",
        "گیاهان دارویی", "طب سنتی", "گوارش", "کلیه", "مجاری ادرار", "غدد",
        "تخصص", "بیهوشی", "مراقبت", "تصویربرداری", "سونوگرافی", "رادیوتراپی",
        "شنوایی‌سنجی", "بینایی‌سنجی", "بینائی‌سنجی", "ژنتیک پزشکی", "پاتولوژی", "خون‌شناسی",
        "ایمونولوژی", "میکروب‌شناسی", "انگل‌شناسی", "سم‌شناسی", "سم‌پروری",
        "فناوری پزشکی", "نانوفناوری پزشکی", "تولید دارو", "تولید تجهیزات پزشکی", "تجهیزات پزشکی",
        "مهندسی پزشکی", "زیست‌شناسی مولکولی", "زیست‌شناسی", "سلولی",
        "زیست‌فناوری", "نانوتکنولوژی پزشکی", "پژوهش‌های بالینی", "پژوهش‌های آزمایشگاهی",
        "بهداشت محیط", "ارتقای بهداشت", "بیمه بیماری", "پیشگیری",
        "آندیورولوژی", "اتولوژی", "نورواتولوژی", "اختلالات کف لگن",
        "استرابیسم", "استریوتاکسی", "اورولوژی", "پریناتولوژی",
        "روماتولوژی", "رینولوژی", "لارینگولوژی", "نفرولوژی",
        "هماتولوژی", "گلوکوم", "ویتره ورتین", "سگمان خلفی",
        "اکوکاردیوگرافی", "پرتو درمانی", "خون و سرطان", "سوختگی",
        "درد", "دردهای مزمن", "طب اورژانس", "طب تسکینی", "طب خواب",
        "طب روانتنی", "طب سوزنی", "طب مادر", "جنین", "طب کار",
        "نازایی", "IVF", "پیوند کبد", "ارتودانتیکس", "پریودانتیکس",
        "پروستو ایمپلنت", "پریو ایمپلانت", "ترمیم زیبایی", "باندینگ",
        "پروتئومیکس", "فارماسیوتیکس", "فارماکوگنوزی",
        "علوم داروهای پرتوزا", "توکسینهای میکروبی", "توکسین‌های میکروبی", "پاتوبیولوژی",
        "بیولوژی دهان", "گفتار درمانی", "کاردرمانی", "مطالعات اعتیاد",
        "آسیب‌شناسی", "ارتوپدی", "کاردیولوژی", "بافت‌شناسی", "سالمندشناسی",
        "شنوایی شناسی", "امور دارویی", "قارچ‌شناسی پزشکی", "نوزادی",
        "باکتری‌شناسی پزشکی", "ویروس‌شناسی پزشکی", "ایمنی‌شناسی پزشکی",
        "نوزادان", "امداد و نجات", "تهیهکنندگی", "تکنسین اتاق عمل",
        "اتاق عمل", "اعضای مصنوعی", "ارتوز", "پروتز", "ارگونومی",
        "آماد", "اروانکولوژی", "بینائیسنجی", "پریو ایمپلنت",
        "سرطانهای دستگاه ادراری", "تناسلی",
    ]),
    ("دامپزشکی", [
        "دامپزشکی", "دامپروری", "طیور", "پرورش دام", "پرورش طیور",
        "پرورش آبزیان", "ماهی", "قارچ", "قارچ‌شناسی", "زنبور", "کرم ابریشم",
        "تغذیه دام", "تغذیه طیور", "پرورش گوشتی", "پرورش شیری",
        "علوم اسب", "اسبداری",
    ]),
    ("علوم پایه", [
        "ریاضی", "فیزیک", "شیمی", "زیست‌شناسی", "آمار", "زمین‌شناسی",
        "نجوم", "اخترفیزیک", "زیست‌فیزیک", "شیمی آلی", "شیمی معدنی",
        "شیمی تجزیه", "شیمی فیزیک", "شیمی محاسباتی", "بیوشیمی",
        "بیوفیزیک", "میکروبیولوژی", "باکتری‌شناسی", "ویروس‌شناسی", "مولکولی", "سلولی", "تکوینی",
        "ژنتیک", "اکولوژی", "زیست‌بوم", "گیاه‌شناسی", "جانورشناسی",
        "حشره‌شناسی", "شیلات", "آبزی‌شناسی",
        "علوم پایه", "علوم تجربی", "علوم تشریحی", "علوم زمین",
        "طب", "رژیم درمانی", "ترکیبات طبیعی", "دارویی دریایی",
    ]),
    ("حقوق و علوم سیاسی", [
        "حقوق", "جزا", "جرم‌شناسی", "حقوق بین‌الملل", "حقوق بشر",
        "حقوق خصوصی", "حقوق عمومی", "حقوق خانواده", "حقوق کار",
        "علوم سیاسی", "روابط بین‌الملل", "سیاست", "حکمرانی",
        "حقوق اساسی", "مدنی", "کیفری", "ثبت", "اسناد",
    ]),
    ("کشاورزی و منابع طبیعی", [
        "کشاورزی", "زراعت", "باغبانی", "گیاهان زراعی", "گیاهان دارویی",
        "آبیاری", "خاک", "حاصلخیزی", "سم‌شناسی خاک", "تغذیه گیاه",
        "گیاه‌پزشکی", "آفات", "بیماری گیاه", "قارچ‌کشی", "حشره‌کش",
        "کنترل بیولوژیک", "منابع طبیعی", "جنگل", "مرتع", "آبخیز",
        "آب زیرزمینی", "آب سطحی", "رودخانه", "دریاچه",
    ]),
    ("ورزش و تربیت بدنی", [
        "تربیت بدنی", "ورزش", "فوتبال", "والیبال", "بسکتبال",
        "هندبال", "والیبال", "توپ", "دو", "میدانی", "شنا",
        "جودو", "کاراته", "تکواندو", "بوکس", "وزنه‌برداری",
        "بدنسازی", "آمادگی بدنی", "فیزیولوژی ورزش", "تغذیه ورزشی",
        "آسیب‌شناسی ورزش", "پزشکی ورزش", "پزشکی ورزشی", "مربیگری", "مدیریت ورزشی",
        "تربیت بدنی", "دانش‌آموز", "دانشجو",
    ]),
    ("رسانه و ارتباطات", [
        "رسانه", "خبر", "ژورنالیسم", "روزنامه‌نگاری", "خبرنگاری",
        "رسانه جمعی", "رسانه دیجیتال", "شبکه‌های اجتماعی",
        "تولید محتوا", "تدوین", "پخش", "رادیو", "تلویزیون",
        "سینما", "فیلم‌سازی", "مستند", "کلیپ", "انیمیشن",
    ]),
]


def _compile_rules() -> list[tuple[str, list[tuple[str, list[str], int]]]]:
    """Pre-fold every keyword once so classification itself stays allocation-light.

    Returns ``[(category, [(original, folded_tokens, folded_len), ...]), ...]``
    in tie-break order.
    """
    compiled = []
    for category, keywords in _RULES:
        entries = []
        for kw in keywords:
            tokens = _fold_tokens(kw)
            if not tokens:
                continue
            entries.append((kw, tokens, len("".join(tokens))))
        compiled.append((category, entries))
    return compiled


_COMPILED = _compile_rules()

_FOLDED_NEGATIVES = {
    category: ["".join(_fold_tokens(kw)) for kw in kws]
    for category, kws in _NEGATIVES.items()
}


def _vetoed(category: str, folded_name: str) -> bool:
    """Whether a negative keyword excludes this category for this name."""
    return any(neg and neg in folded_name for neg in _FOLDED_NEGATIVES.get(category, []))


def classify_with_reason(normalized_name: str) -> tuple[str, str | None, int | None]:
    """Classify a normalized field name, explaining the decision.

    Returns ``(category, matched_keyword, rule_index)`` where
    ``matched_keyword`` is the original keyword spelling and ``rule_index``
    is the winning category's position in the rule list (the documented
    tie-break). ``("other", None, None)`` when nothing matches.
    """
    tokens = _fold_tokens(normalized_name)
    if not tokens:
        return "other", None, None
    folded_name = "".join(tokens)

    best: tuple[int, int, int, str] | None = None
    for rule_index, (category, entries) in enumerate(_COMPILED):
        if _vetoed(category, folded_name):
            continue
        for original, kw_tokens, kw_len in entries:
            whole = False
            if len(kw_tokens) > 1:
                if not _contains_sequence(tokens, kw_tokens):
                    continue
                whole = True
            elif len(kw_tokens[0]) <= 4:
                if kw_tokens[0] not in tokens:
                    continue
                whole = True
            else:
                if kw_tokens[0] in tokens:
                    whole = True
                elif not any(kw_tokens[0] in token for token in tokens):
                    continue
            candidate = (kw_len, 1 if whole else 0, -rule_index, original)
            if best is None or candidate[:3] > best[:3]:
                best = (kw_len, 1 if whole else 0, -rule_index, original)

    if best is None:
        return "other", None, None
    return _category_at(best)


def _category_at(best: tuple[int, int, int, str]) -> tuple[str, str | None, int | None]:
    """Unpack a winning candidate into the public return shape."""
    _, _, neg_rule, original = best
    rule_index = -neg_rule
    return _RULES[rule_index][0], original, rule_index


def classify_field(normalized_name: str) -> str:
    """Classify a normalized field name into a category name.

    Plain-category facade over :func:`classify_with_reason`, kept so the
    Worker, the seed script, and existing callers are untouched. Returns
    ``other`` when nothing matches.
    """
    category, _, _ = classify_with_reason(normalized_name)
    return category
