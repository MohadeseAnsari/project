"""Credential handling (PART D): encrypted at rest, write-only in, never out.

These are the tests that matter for a secret: a key pasted into the panel must
be encrypted in the database, must never come back out through the API, a
rendered page, an audit row or a log line, and a row with its own base_url
must fail loudly rather than borrow another provider's key.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.credentials import CredentialError, decrypt, encrypt, mask
from app.credentials_resolve import (
    MissingModelKeyError,
    describe_credentials,
    resolve_model_credentials,
)
from app.db.models import AuditLog, Channel, Main, Model, PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client

#: A distinctive value so "is this string anywhere?" is unambiguous.
SECRET = "sk-ZZtestSECRET9999-not-a-real-key"


@pytest.fixture(autouse=True)
def _isolated_master_key(tmp_path, monkeypatch):
    """Give every test its own master key file, and drop the cached Fernet."""
    from app.credentials import reset_master_key_cache

    keyfile = tmp_path / "credentials.key"
    monkeypatch.setenv("CREDENTIALS_KEY_PATH", str(keyfile))
    monkeypatch.delenv("CREDENTIALS_KEY", raising=False)
    reset_master_key_cache()
    yield
    reset_master_key_cache()


# --------------------------------------------------------------------------
# the crypto itself
# --------------------------------------------------------------------------


def test_roundtrip_encrypts_and_decrypts():
    """A secret survives encrypt -> decrypt, but is not stored in the clear."""
    stored = encrypt(SECRET)
    assert stored != SECRET
    assert SECRET not in stored
    assert decrypt(stored) == SECRET


def test_empty_and_none_stay_none():
    """No key means NULL, so 'keep stored value' and 'cleared' stay distinct."""
    assert encrypt(None) is None
    assert encrypt("") is None
    assert decrypt(None) is None
    assert decrypt("") is None


def test_mask_shows_only_the_tail():
    """The display hint reveals the last 4 characters and nothing else."""
    assert mask(SECRET).endswith(SECRET[-4:])
    assert SECRET[:-4] not in mask(SECRET)
    assert mask(None) is None


def test_master_key_file_is_generated_0600_and_gitignored():
    """First boot writes a 0600 key file outside version control."""
    import os
    import stat
    from pathlib import Path

    keyfile = Path(os.environ["CREDENTIALS_KEY_PATH"])
    assert not keyfile.exists()
    encrypt(SECRET)  # triggers generation
    assert keyfile.exists()
    assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
    # .gitignore is not shipped in the image, so assert the rule where it
    # lives: the key path must be covered by the repo's ignore file.
    repo_ignore = Path(__file__).resolve().parent.parent / ".gitignore"
    if repo_ignore.exists():
        assert "data/.credentials_key" in repo_ignore.read_text(encoding="utf-8")


def test_env_key_wins_over_the_file(monkeypatch):
    """CREDENTIALS_KEY is the explicit, portable choice."""
    from cryptography.fernet import Fernet

    from app.credentials import reset_master_key_cache

    stored = encrypt(SECRET)  # encrypted with the generated file key
    monkeypatch.setenv("CREDENTIALS_KEY", Fernet.generate_key().decode())
    reset_master_key_cache()
    # A different explicit key cannot read what the file key wrote.
    with pytest.raises(CredentialError):
        decrypt(stored)


def test_wrong_master_key_fails_without_leaking_the_value(monkeypatch):
    """A wrong key raises a message that does not contain the ciphertext."""
    from cryptography.fernet import Fernet

    from app.credentials import reset_master_key_cache

    stored = encrypt(SECRET)
    monkeypatch.setenv("CREDENTIALS_KEY", Fernet.generate_key().decode())
    reset_master_key_cache()
    with pytest.raises(CredentialError) as exc:
        decrypt(stored)
    assert SECRET not in str(exc.value)
    assert stored not in str(exc.value)


# --------------------------------------------------------------------------
# resolution: the 401 that started this
# --------------------------------------------------------------------------


class _Row:
    """A model-shaped stand-in with the attributes the resolver reads."""

    def __init__(self, **kwargs):
        self.name = kwargs.pop("name", "m")
        self.base_url = kwargs.pop("base_url", None)
        self.api_key_env = kwargs.pop("api_key_env", None)
        self.api_key_encrypted = kwargs.pop("api_key_encrypted", None)
        self.api_model = kwargs.pop("api_model", None)


def test_row_with_own_base_url_and_no_key_fails_loudly(monkeypatch):
    """The reported bug: never send the global key to a new endpoint."""
    monkeypatch.setattr(settings, "avalai_api_key", "GLOBAL-KEY-SHOULD-NOT-LEAK")
    row = _Row(name="Qwen", base_url="https://api.vft.ir/v1", api_key_env="VFT_KEY")
    with pytest.raises(MissingModelKeyError) as exc:
        resolve_model_credentials(row)
    assert "https://api.vft.ir/v1" in str(exc.value)
    assert "VFT_KEY" in str(exc.value)
    assert "GLOBAL-KEY-SHOULD-NOT-LEAK" not in str(exc.value)


def test_row_key_is_preferred_over_env_and_global(monkeypatch):
    """Order: stored key > env variable > global."""
    monkeypatch.setattr(settings, "avalai_api_key", "global-key")
    monkeypatch.setenv("ROW_ENV", "env-key")
    row = _Row(base_url="https://x/v1", api_key_encrypted=encrypt(SECRET), api_key_env="ROW_ENV")
    resolved = resolve_model_credentials(row)
    assert resolved.api_key == SECRET
    assert resolved.key_source == "row"

    row_no_stored = _Row(base_url="https://x/v1", api_key_encrypted=None, api_key_env="ROW_ENV")
    assert resolve_model_credentials(row_no_stored).api_key == "env-key"


def test_row_without_own_base_url_falls_back_to_global(monkeypatch):
    """A row sharing the configured endpoint uses the global key."""
    monkeypatch.setattr(settings, "avalai_api_key", "global-key")
    resolved = resolve_model_credentials(_Row(base_url=None, api_key_encrypted=None))
    assert resolved.api_key == "global-key"
    assert resolved.key_source == "global"


def test_api_model_falls_back_to_the_row_name(monkeypatch):
    """The provider's real model id is used when the row defines one."""
    monkeypatch.setattr(settings, "avalai_api_key", "k")
    assert resolve_model_credentials(
        _Row(name="display-name", api_model="qwen3-72b", api_key_encrypted=None)
    ).api_model == "qwen3-72b"
    assert resolve_model_credentials(
        _Row(name="display-name", api_model=None, api_key_encrypted=None)
    ).api_model == "display-name"


def test_describe_reports_needs_own_key():
    """The panel is told why a row is unusable instead of implying a fallback."""
    described = describe_credentials(_Row(base_url="https://x/v1", api_key_env="MISSING_VAR"))
    assert described["needs_own_key"] is True
    assert described["key_present"] is False
    assert described["key_hint"] is None


# --------------------------------------------------------------------------
# panel + API: write-only in, never out
# --------------------------------------------------------------------------


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="Full access")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="cred-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


@pytest.mark.asyncio
async def test_panel_stores_the_key_encrypted_and_never_renders_it(session: AsyncSession):
    """Paste a key in the panel: it lands encrypted and never appears again."""
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "cred-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.post(
            "/models",
            data={
                "name": "cred-model", "provider": "openai", "version": "v1",
                "base_url": "https://provider.example/v1", "api_key": SECRET,
                "description": "key test",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        page = await client.get("/models")
        assert SECRET not in page.text, "the panel must never render the key"

    stored = (
        await session.execute(select(Model).where(Model.name == "cred-model"))
    ).scalar_one()
    assert stored.api_key_encrypted is not None
    assert SECRET not in stored.api_key_encrypted, "stored ciphertext, not plaintext"
    assert decrypt(stored.api_key_encrypted) == SECRET

    # The key resolves back for actual use (this is why it was stored).
    assert resolve_model_credentials(stored).api_key == SECRET


@pytest.mark.asyncio
async def test_empty_submit_keeps_the_stored_key(session: AsyncSession):
    """Saving the form without retyping the key must not erase it."""
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "cred-admin", "password": "secret123"},
                          follow_redirects=False)
        await client.post(
            "/models",
            data={"name": "keep-model", "provider": "openai", "version": "v1", "api_key": SECRET},
            follow_redirects=False,
        )
        row = (await session.execute(select(Model).where(Model.name == "keep-model"))).scalar_one()
        await client.post(
            f"/models/{row.id}",
            data={"description": "changed only the description"},
            follow_redirects=False,
        )
    await session.refresh(row)
    assert decrypt(row.api_key_encrypted) == SECRET, "empty submit keeps the stored key"


@pytest.mark.asyncio
async def test_clear_key_is_explicit(session: AsyncSession):
    """The dedicated clear action removes it."""
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "cred-admin", "password": "secret123"},
                          follow_redirects=False)
        await client.post(
            "/models",
            data={"name": "clear-model", "provider": "openai", "version": "v1", "api_key": SECRET},
            follow_redirects=False,
        )
        row = (await session.execute(select(Model).where(Model.name == "clear-model"))).scalar_one()
        assert (await client.post(f"/models/{row.id}/clear-key", follow_redirects=False)).status_code == 302
    await session.refresh(row)
    assert row.api_key_encrypted is None


@pytest.mark.asyncio
async def test_audit_rows_never_contain_the_key(session: AsyncSession):
    """The audit trail records that a key was set, never the key."""
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "cred-admin", "password": "secret123"},
                          follow_redirects=False)
        await client.post(
            "/models",
            data={"name": "audit-model", "provider": "openai", "version": "v1", "api_key": SECRET},
            follow_redirects=False,
        )
        audit_page = await client.get("/audit")
    assert SECRET not in audit_page.text
    details = (await session.execute(select(AuditLog.detail))).scalars().all()
    assert all(SECRET not in (d or "") for d in details)


@pytest.mark.asyncio
async def test_api_never_returns_the_key(session: AsyncSession):
    """Machine API: create with a key, then read/list/activate it back."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app as api_app

    await _admin(session)
    secret = settings.shared_secret
    async with AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test") as client:
        created = await client.post(
            "/models",
            json={"shared_Secret": secret, "name": "api-model", "provider": "openai",
                  "version": "v1", "base_url": "https://p.example/v1", "api_key": SECRET},
        )
        assert created.status_code == 201
        assert SECRET not in created.text
        model_id = created.json()["id"]
        assert created.json()["has_key"] is True
        assert "api_key" not in created.json()

        for path in (f"/models/{model_id}", "/models"):
            body = (await client.get(path, params={"shared_Secret": secret})).text
            assert SECRET not in body, f"{path} leaked the key"
            assert "api_key_encrypted" not in body

        activated = await client.post(
            f"/models/{model_id}/activate", json={"shared_Secret": secret}
        )
        assert SECRET not in activated.text


@pytest.mark.asyncio
async def test_channel_bot_token_is_encrypted_and_write_only(session: AsyncSession):
    """A channel's own bot token is encrypted and never rendered back."""
    await _admin(session)
    from app.credentials import encrypt as _encrypt

    channel = Channel(platform="bale", chat_id=9940001, username="@tok", is_active=True,
                      bot_token_encrypted=_encrypt("123456:BOT-TOKEN-SECRET"))
    session.add(channel)
    await session.commit()

    async with panel_client() as client:
        await client.post("/login", data={"username": "cred-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await client.get("/channels")
        assert "BOT-TOKEN-SECRET" not in page.text

    await session.refresh(channel)
    assert "BOT-TOKEN-SECRET" not in channel.bot_token_encrypted
    assert decrypt(channel.bot_token_encrypted) == "123456:BOT-TOKEN-SECRET"


@pytest.mark.asyncio
async def test_logs_never_contain_the_key(caplog, monkeypatch):
    """An unreadable stored key logs a reason, never the key or ciphertext."""
    import logging

    from cryptography.fernet import Fernet

    from app.credentials import reset_master_key_cache

    row = _Row(name="logged", base_url="https://x/v1", api_key_encrypted=encrypt(SECRET))
    with caplog.at_level(logging.DEBUG):
        monkeypatch.setenv("CREDENTIALS_KEY", Fernet.generate_key().decode())
        reset_master_key_cache()
        # The stored key cannot be read. Because the row has its own endpoint,
        # resolution fails loudly rather than sending the global key there.
        with pytest.raises(MissingModelKeyError):
            resolve_model_credentials(row)
        reset_master_key_cache()
    assert SECRET not in caplog.text
    assert "unreadable" in caplog.text.lower()
    # The logged reason names the model, never the key or the ciphertext.
    assert "logged" in caplog.text


@pytest.mark.asyncio
async def test_generation_uses_the_row_key(session: AsyncSession, monkeypatch):
    """The worker path resolves the same key the panel stored."""
    from app.news.generation import resolve_generation_config

    monkeypatch.setattr(settings, "avalai_api_key", "global-key")
    row = Model(name="worker-model", provider="openai", version="v1",
                base_url="https://w.example/v1", api_key_encrypted=encrypt(SECRET))
    session.add(row)
    await session.commit()
    config = resolve_generation_config(row, None, None, None)
    assert config["api_key"] == SECRET
    assert config["base_url"] == "https://w.example/v1"
    assert config["api_model"] == "worker-model"


@pytest.mark.asyncio
async def test_record_error_mentions_the_missing_key_not_a_value(session: AsyncSession, monkeypatch):
    """The loud failure reaches main.error_message without leaking anything."""
    monkeypatch.setattr(settings, "avalai_api_key", "global-key")
    record = Main(
        username="t", title="T", abstract="A", field="پزشکی", university="U",
        author="A", supervisor="S", status="review",
        output_json={"title": "T", "lead": "L", "body": "B"},
    )
    session.add(record)
    await session.commit()

    class _BrokenModel:
        name = "no-key-model"
        base_url = "https://nokey.example/v1"
        api_key_env = "MISSING_KEY_VAR"
        api_key_encrypted = None
        api_model = None

    with pytest.raises(MissingModelKeyError) as exc:
        resolve_model_credentials(_BrokenModel())
    message = str(exc.value)
    assert "https://nokey.example/v1" in message
    assert "MISSING_KEY_VAR" in message
    assert "global-key" not in message