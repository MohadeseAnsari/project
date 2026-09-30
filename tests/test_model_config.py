"""Model identifier consistency tests.

The canonical runtime model identifier is ``gpt-5.6-luna`` (confirmed by the
project owner). These tests prevent model-name drift in configuration, seed
data, and source code. The application must read the model from configuration
and must not silently use another identifier.
"""

import inspect
from pathlib import Path

from app.config import Settings, settings

CANONICAL_MODEL = "gpt-5.6-luna"


def test_configured_model_is_canonical():
    assert settings.llm_model == CANONICAL_MODEL


def test_model_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-other-model")
    fresh = Settings()
    assert fresh.llm_model == "some-other-model"


def test_generation_reads_model_from_settings():
    import app.news.generation as generation

    source = inspect.getsource(generation)
    assert "settings.llm_model" in source


def test_seed_migration_uses_canonical_model():
    migration = Path("migrations/004_seed_model.sql").read_text(encoding="utf-8")
    assert CANONICAL_MODEL in migration
    assert "luna-5" not in migration


def test_no_stale_model_identifier_in_source():
    for py in Path("app").rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "luna-5" not in text, f"stale model identifier in {py}"
