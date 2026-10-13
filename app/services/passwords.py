"""Password hashing for panel accounts (argon2, never reversible).

Pure crypto with no database or HTTP dependency, so BOTH the panel auth
layer and the users service hash/verify passwords through this one place —
the service layer never imports the panel, and the panel never stores a hash
itself.
"""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage (argon2, never reversible)."""
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Check a password against its stored hash.

    argon2 compares in a way that does not bail out early on the first wrong
    byte, so a wrong password takes the same work as a right one.
    """
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False