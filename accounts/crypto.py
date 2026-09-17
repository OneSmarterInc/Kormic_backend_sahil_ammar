from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptionNotConfigured(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = os.getenv("GITHUB_OAUTH_TOKEN_KEY")
    if not key:
        raise TokenEncryptionNotConfigured(
            "GITHUB_OAUTH_TOKEN_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
            "and add it to .env before connecting any GitHub account."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise TokenEncryptionNotConfigured(
            "GITHUB_OAUTH_TOKEN_KEY is not a valid Fernet key."
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a token/secret for storage. Returns a text-safe ciphertext."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a value previously produced by encrypt_secret."""
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise TokenEncryptionNotConfigured(
            "Stored secret could not be decrypted -- GITHUB_OAUTH_TOKEN_KEY may have changed."
        ) from exc
