from app.auth.secret import verify_secret
from app.config import settings


def test_valid_secret():
    assert verify_secret(settings.shared_secret) is True


def test_invalid_secret():
    assert verify_secret("wrong-secret") is False


def test_empty_secret():
    assert verify_secret("") is False


def test_none_secret():
    assert verify_secret("") is False
