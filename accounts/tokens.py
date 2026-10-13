"""JWT credentials bound to a revocable account generation."""
from rest_framework_simplejwt.tokens import AccessToken as BaseAccessToken, RefreshToken as BaseRefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from accounts.models import Account


def session_version(user_id):
    return Account.objects.filter(user_id=user_id).values_list("session_version", flat=True).first()


def session_valid(token):
    version = session_version(token.get("user_id"))
    return version is not None and token.get("session_version", 0) == version


class VersionedToken:
    @classmethod
    def for_user(cls, user):
        token = super().for_user(user)
        token["session_version"] = session_version(user.pk) or 0
        return token


class AccessToken(VersionedToken, BaseAccessToken):
    pass


def enrollment_token(user) -> AccessToken:
    """Password-only credentials never become a full session implicitly."""
    token = AccessToken.for_user(user)
    token['enrollment_only'] = True
    return token


class RefreshToken(VersionedToken, BaseRefreshToken):
    def verify(self):
        super().verify()
        if not session_valid(self):
            raise TokenError("Session revoked. Please sign in again.")
