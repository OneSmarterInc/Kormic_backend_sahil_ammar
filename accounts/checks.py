from django.core.checks import Error, register
from accounts.crypto import TOTPEncryptionError, totp_keyring


@register()
def check_totp_encryption(app_configs, **kwargs):
    try:
        totp_keyring()
    except TOTPEncryptionError as exc:
        return [Error(str(exc), hint='Configure TOTP_SECRET_KEYS outside the database; see TOTP_SECURITY.md.', id='accounts.E001')]
    return []
