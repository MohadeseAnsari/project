"""Credential encryption for database-stored secrets (PART D).

Provider keys and bot tokens live in the database so they can be managed from
the panel, but never in plaintext:

- ``encrypt``/``decrypt`` are Fernet (AES-128-CBC + HMAC) from
  ``cryptography``: authenticated, so a tampered ciphertext fails loudly
  instead of returning garbage.
- The master key is NEVER stored in the database. It comes from the
  ``CREDENTIALS_KEY`` environment variable; when that is unset, it is
  generated once on first boot into a gitignored file with mode ``0600`` and
  the operator is told, in the log and in the docs, that it must be backed up
  (losing it makes every stored credential undecryptable).
- ``decrypt`` never raises with the ciphertext in the message, so a failure
  cannot leak key material into logs.
"""

from __future__ import annotations

import base64
import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

#: File used when CREDENTIALS_KEY is not set. Gitignored, 0600.
DEFAULT_KEY_PATH = Path("data/.credentials_key")

#: Prefix stored with the ciphertext, so a stored value is self-describing.
_CIPHER_PREFIX = "enc:v1:"


class CredentialError(RuntimeError):
    """A credential could not be encrypted/decrypted. Never carries a value."""


def _key_path() -> Path:
    return Path(os.environ.get("CREDENTIALS_KEY_PATH") or DEFAULT_KEY_PATH)


def load_master_key() -> bytes:
    """Return the master key, generating it on first boot when needed.

    Order: ``CREDENTIALS_KEY`` env → the on-disk key file → generate.
    The generated file is 0600 and gitignored; generating is logged so the
    operator knows to back it up.
    """
    from_env = os.environ.get("CREDENTIALS_KEY", "").strip()
    if from_env:
        return _validate(from_env)

    path = _key_path()
    if path.exists():
        return _validate(path.read_text(encoding="utf-8").strip())

    generated = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start, never widen it afterwards.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(generated.decode("ascii"))
    os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)
    logger.warning(
        "CREDENTIALS_KEY is not set; generated a new master key at %s (mode 0600). "
        "Back it up: without it every stored credential is undecryptable. "
        "Set CREDENTIALS_KEY in the environment to manage this explicitly.",
        path,
    )
    return generated


def _validate(value: str) -> bytes:
    """Accept a Fernet key and return the raw 32 bytes.

    Validated through ``Fernet`` itself (which checks length and encoding) so
    any real key works, and a wrong one fails with a message that never echoes
    the value.
    """
    try:
        key = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CredentialError("CREDENTIALS_KEY is not a valid Fernet key") from exc
    try:
        Fernet(key)  # validates length + urlsafe-base64
    except Exception as exc:  # noqa: BLE001 - never echo the value
        raise CredentialError("CREDENTIALS_KEY is not a valid Fernet key") from exc
    return key


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(load_master_key())
    return _fernet


def reset_master_key_cache() -> None:
    """Forget the cached Fernet (used by tests and after a key rotation)."""
    global _fernet
    _fernet = None


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a secret for storage; ``None``/empty stays ``None``.

    Storing ``None`` for "no key" (rather than an empty ciphertext) is what
    lets "empty submit keeps the stored value" and the explicit clear action
    be distinguished.
    """
    if plaintext is None or plaintext == "":
        return None
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_CIPHER_PREFIX}{token}"


def decrypt(stored: str | None) -> str | None:
    """Decrypt a stored credential; ``None`` when there is nothing stored.

    Raises:
        CredentialError: the value is not decryptable (wrong master key or
            tampered data). The message never contains the stored value.
    """
    if stored is None or stored == "":
        return None
    if not stored.startswith(_CIPHER_PREFIX):
        raise CredentialError("stored credential is not in the expected format")
    token = stored[len(_CIPHER_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialError("stored credential cannot be decrypted with this master key") from exc


def mask(value: str | None) -> str | None:
    """A display-safe hint for a stored secret (never the secret itself).

    Shows only the last 4 characters, so an operator can tell *which* key is
    stored without the value ever reaching a page or a log.
    """
    if not value:
        return None
    tail = value[-4:]
    return f"••••{tail}"


def is_encrypted(value: str | None) -> bool:
    """Whether a stored column value is already ciphertext."""
    return bool(value) and value.startswith(_CIPHER_PREFIX)