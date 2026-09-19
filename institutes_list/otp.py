"""Versioned per-issuance HMACs: database access alone cannot enumerate OTPs."""
import secrets
from django.utils.crypto import constant_time_compare, salted_hmac


def hash_otp(code):
    nonce = secrets.token_hex(16)
    digest = salted_hmac("institute-claim-otp-v1", nonce + ":" + code, algorithm="sha256").hexdigest()
    return "h1$" + nonce + "$" + digest


def check_otp(code, stored):
    try:
        version, nonce, digest = stored.split("$")
    except (AttributeError, ValueError):
        return False
    if version != "h1" or len(nonce) != 32 or len(digest) != 64:
        return False
    expected = salted_hmac("institute-claim-otp-v1", nonce + ":" + code, algorithm="sha256").hexdigest()
    return constant_time_compare(expected, digest)
