from app.fields.normalization import normalize_field


def test_nfc_normalization():
    assert normalize_field("آسیب‌شناسی") == "آسیب‌شناسی"


def test_trim_whitespace():
    assert normalize_field("  پزشکی  ") == "پزشکی"


def test_collapse_internal_whitespace():
    assert normalize_field("بیماریهای  چشم") == "بیماریهای چشم"


def test_collapse_multiple_spaces():
    assert normalize_field("خون‌شناسی  آزمایشگاهی و بانک خون") == "خون‌شناسی آزمایشگاهی و بانک خون"


def test_zwnj_preserved():
    result = normalize_field("آسیب‌شناسی")
    assert "\u200c" in result


def test_zero_width_removed():
    result = normalize_field("پزشکی\u200b")
    assert result == "پزشکی"


def test_empty_string():
    assert normalize_field("") == ""


def test_whitespace_only():
    assert normalize_field("   ") == ""
