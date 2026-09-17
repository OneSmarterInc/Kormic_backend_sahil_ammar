from __future__ import annotations

import hashlib
import secrets

import pyotp
from django.core.cache import cache

BACKUP_CODE_COUNT = 10
BACKUP_CODE_LENGTH = 10
BACKUP_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_TOTP_USED_KEY_PREFIX = "totp:used:"
_TOTP_USED_TTL = 90  # covers valid_window=1 (three 30-second slots)


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def build_provisioning_uri(secret: str, user_email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=user_email, issuer_name="Kormic Commons")


def normalize_code(raw: str) -> str:
    """Strips ALL whitespace, not just leading/trailing."""
    return "".join(str(raw or "").split())


def verify_totp_code(secret: str, code: str, *, user_id=None) -> bool:
    code = normalize_code(code)
    if not code or not code.isdigit() or len(code) != 6:
        return False

    if user_id is not None:
        replay_key = f"{_TOTP_USED_KEY_PREFIX}{user_id}:{code}"
        if cache.get(replay_key):
            return False

    if not pyotp.TOTP(secret).verify(code, valid_window=1):
        return False

    if user_id is not None:
        cache.set(replay_key, True, timeout=_TOTP_USED_TTL)

    return True


def generate_backup_codes() -> list[str]:
    return [
        "".join(secrets.choice(BACKUP_CODE_ALPHABET) for _ in range(BACKUP_CODE_LENGTH))
        for _ in range(BACKUP_CODE_COUNT)
    ]


def hash_backup_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
