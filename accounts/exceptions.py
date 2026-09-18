import logging
from rest_framework.response import Response
from rest_framework.views import exception_handler
from accounts.crypto import TOTPEncryptionError

logger = logging.getLogger(__name__)


def auth_exception_handler(exc, context):
    if isinstance(exc, TOTPEncryptionError):
        logger.error('TOTP encryption unavailable; check encryption keys and stored data.')
        return Response({'detail': 'Authentication is temporarily unavailable. Please try again later.'}, status=503)
    response = exception_handler(exc, context)
    if response is None:
        logger.error("Unhandled API exception request_id=%s type=%s", getattr(context.get("request"), "request_id", ""), type(exc).__name__)
        return Response({"detail": "Something went wrong. Please try again later."}, status=500)
    code = getattr(exc, "default_code", "")
    codes = {"not_authenticated": "AUTHENTICATION_REQUIRED", "authentication_failed": "AUTHENTICATION_FAILED",
             "permission_denied": "PERMISSION_DENIED", "throttled": "RATE_LIMITED", "invalid": "VALIDATION_ERROR"}
    response.error_code = codes.get(code)
    return response

