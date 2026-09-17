import logging
from rest_framework.response import Response
from rest_framework.views import exception_handler
from accounts.crypto import TOTPEncryptionError

logger = logging.getLogger(__name__)


def auth_exception_handler(exc, context):
    if isinstance(exc, TOTPEncryptionError):
        logger.error('TOTP encryption unavailable; check encryption keys and stored data.')
        return Response({'detail': 'Authentication is temporarily unavailable. Please try again later.'}, status=503)
    return exception_handler(exc, context)
