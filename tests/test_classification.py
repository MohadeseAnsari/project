from app.fields.classification import classify_field


def test_architecture():
    assert classify_field("معماری") == "هنر و معماری"


def test_architecture_interior():
    assert classify_field("معماری داخلی") == "هنر و معماری"


def test_architecture_energy():
    assert classify_field("معماری و انرژی") == "مهندسی و فناوری"


def test_engineering_architecture():
    assert classify_field("مهندسی معماری") == "مهندسی و فناوری"


def test_technology_architecture():
    assert classify_field("فناوری معماری") == "مهندسی و فناوری"


def test_medicine():
    assert classify_field("پزشکی") == "پزشکی و علوم سلامت"


def test_veterinary():
    assert classify_field("دامپزشکی") == "دامپزشکی"


def test_medicine_disease_eye():
    assert classify_field("بیماریهای چشم") == "پزشکی و علوم سلامت"


def test_medicine_hematology():
    assert classify_field("خونشناسی آزمایشگاهی و بانک خون") == "پزشکی و علوم سلامت"


def test_unknown_field():
    assert classify_field("unknown field xyz") == "other"


def test_specific_before_broad():
    result = classify_field("معماری داخلی")
    assert result == "هنر و معماری"


def test_engineering_before_architecture():
    result = classify_field("مهندسی معماری")
    assert result == "مهندسی و فناوری"
