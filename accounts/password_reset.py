from __future__ import annotations

import hashlib
import secrets
import uuid

from django.core.cache import cache

# Numeric email OTP, not TOTP -- no shared secret/time-window semantics
# needed, unlike accounts.totp. Cache-backed and TTL'd, mirroring the
# mfa_token pattern in accounts.mfa so this flow reads the same way to
# anyone already familiar with the login MFA code.
OTP_LENGTH = 6
OTP_TTL = 600  # seconds -- how long a requested code stays valid
OTP_RESEND_COOLDOWN = 60  # seconds between /forgot-password/ calls for the same email
OTP_MAX_ATTEMPTS = 5
OTP_LOCKOUT_TTL = 900  # seconds a user is locked out of verify-otp after too many wrong codes

# Opaque one-time token exchanged for a verified OTP, required to actually
# set the new password. Keeps "prove you own the inbox" and "choose a new
# password" as two separate steps, like mfa_token -> access/refresh token.
RESET_TOKEN_TTL = 300  # seconds


def _otp_key(user_id) -> str:
    return f"pwreset:otp:{user_id}"


def _attempt_key(user_id) -> str:
    return f"pwreset:attempts:{user_id}"


def _cooldown_key(email: str) -> str:
    return f"pwreset:cooldown:{email.strip().lower()}"


def _reset_token_key(token: str) -> str:
    return f"pwreset:token:{token}"


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def generate_otp() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(OTP_LENGTH))


def is_resend_throttled(email: str) -> bool:
    """
    Keyed by the submitted email itself (not a resolved user id) so the
    cooldown is set identically whether or not an account exists --
    otherwise a caller could fingerprint real accounts by whether a
    second rapid request is throttled.
    """
    return cache.get(_cooldown_key(email)) is not None


def start_cooldown(email: str) -> None:
    cache.set(_cooldown_key(email), True, timeout=OTP_RESEND_COOLDOWN)


def issue_otp(user_id) -> str:
    """Generates a fresh OTP, replacing any still-pending one, and resets the attempt counter."""
    otp = generate_otp()
    cache.set(_otp_key(user_id), hash_otp(otp), timeout=OTP_TTL)
    cache.delete(_attempt_key(user_id))
    return otp


def is_otp_locked(user_id) -> bool:
    return (cache.get(_attempt_key(user_id)) or 0) >= OTP_MAX_ATTEMPTS


def record_otp_failure(user_id) -> int:
    key = _attempt_key(user_id)
    count = (cache.get(key) or 0) + 1
    cache.set(key, count, timeout=OTP_LOCKOUT_TTL)
    return count


def verify_otp(user_id, otp: str) -> bool:
    stored_hash = cache.get(_otp_key(user_id))
    if not stored_hash or not secrets.compare_digest(stored_hash, hash_otp(otp)):
        return False

    # One-time use: a matched code is immediately burned so it can't be
    # replayed to mint a second reset_token.
    cache.delete(_otp_key(user_id))
    cache.delete(_attempt_key(user_id))
    return True


def create_reset_session(user_id) -> str:
    token = uuid.uuid4().hex
    cache.set(_reset_token_key(token), user_id, timeout=RESET_TOKEN_TTL)
    return token


def get_user_id_from_reset_token(token: str):
    return cache.get(_reset_token_key(token))


def invalidate_reset_session(token: str) -> None:
    cache.delete(_reset_token_key(token))
