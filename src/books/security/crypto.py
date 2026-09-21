"""Encryption for provider access tokens.

Tokens are encrypted before they are written to Postgres and decrypted only
inside ``books.core.sync`` when a provider call needs them. They are never
logged (see ``books.logging.REDACTED_KEYS``) and never leave the core service.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from books.config import get_settings
from books.core.errors import ConfigurationError


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise ConfigurationError(
            "BOOKS_ENCRYPTION_KEY is unset. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:  # malformed key
        raise ConfigurationError(f"BOOKS_ENCRYPTION_KEY is not a valid Fernet key: {exc}") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ConfigurationError(
            "Stored access token could not be decrypted. The BOOKS_ENCRYPTION_KEY "
            "likely differs from the one used when the item was linked."
        ) from exc


def generate_key() -> str:
    return Fernet.generate_key().decode()
