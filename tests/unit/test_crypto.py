from __future__ import annotations

import pytest

from books.core.errors import ConfigurationError
from books.security import crypto


def test_round_trip() -> None:
    token = "access-sandbox-abc123"
    encrypted = crypto.encrypt(token)
    assert encrypted != token
    assert crypto.decrypt(encrypted) == token


def test_decrypt_rejects_foreign_ciphertext() -> None:
    with pytest.raises(ConfigurationError, match="could not be decrypted"):
        crypto.decrypt("gAAAAABmZm90-not-a-real-token")


def test_generate_key_is_usable() -> None:
    from cryptography.fernet import Fernet

    assert Fernet(crypto.generate_key().encode())
