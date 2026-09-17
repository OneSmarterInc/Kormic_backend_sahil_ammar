from __future__ import annotations

import uuid

from django.core.cache import cache

MFA_TOKEN_TTL = 300  # seconds
TOTP_MAX_ATTEMPTS = 5
TOTP_LOCKOUT_TTL = 300  # seconds


def _token_key(token: str) -> str:
    return f"mfa:token:{token}"


def _attempt_key(user_id) -> str:
    return f"mfa:totp_attempts:{user_id}"


def create_mfa_session(user_id) -> str:
    token = uuid.uuid4().hex
    cache.set(_token_key(token), user_id, timeout=MFA_TOKEN_TTL)
    return token


def get_user_id_from_mfa_token(token: str):
    return cache.get(_token_key(token))


def invalidate_mfa_session(token: str) -> None:
    cache.delete(_token_key(token))


def record_totp_failure(user_id) -> int:
    key = _attempt_key(user_id)
    count = (cache.get(key) or 0) + 1
    cache.set(key, count, timeout=TOTP_LOCKOUT_TTL)
    return count


def is_totp_throttled(user_id) -> bool:
    return (cache.get(_attempt_key(user_id)) or 0) >= TOTP_MAX_ATTEMPTS


def clear_totp_failures(user_id) -> None:
    cache.delete(_attempt_key(user_id))
