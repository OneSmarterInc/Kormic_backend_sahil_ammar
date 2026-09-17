from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings


class TokenEncryptionNotConfigured(RuntimeError):
    pass


class TOTPEncryptionError(RuntimeError):
    """No secret or key material is included in the error message."""


def totp_keyring() -> MultiFernet:
    keys = settings.TOTP_SECRET_KEYS
    if not keys:
        raise TOTPEncryptionError('TOTP_SECRET_KEYS must contain at least one Fernet key.')
    try:
        return MultiFernet([Fernet(key.encode('ascii')) for key in keys])
    except (ValueError, TypeError, UnicodeError) as exc:
        raise TOTPEncryptionError('TOTP_SECRET_KEYS contains an invalid Fernet key.') from exc


def encrypt_totp_secret(plaintext: str) -> str:
    return totp_keyring().encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt_totp_secret(ciphertext: str) -> str:
    try:
        return totp_keyring().decrypt(ciphertext.encode('ascii')).decode('utf-8')
    except (InvalidToken, UnicodeError, AttributeError) as exc:
        raise TOTPEncryptionError('TOTP secret cannot be decrypted with the configured keys.') from exc


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
