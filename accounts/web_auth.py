"""Browser-only cookie sessions. Native clients retain the JSON token API."""
from django.conf import settings
from django.contrib.auth.models import User
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

from accounts.models import Account, TOTPDevice
from accounts.views import LoginView, RegisterView, TOTPLoginVerifyView, _serialize_user_with_verification


def cookie_name(portal):
    # __Host- forbids Domain and requires Secure + Path=/ in production.
    prefix = '' if settings.DEBUG else '__Host-'
    return f'{prefix}kormic-refresh-{portal}'


def clear_cookie(response, portal):
    response.set_cookie(cookie_name(portal), '', max_age=0, path='/',
                        secure=not settings.DEBUG, httponly=True, samesite='Lax')


def portal_for(request):
    portal = request.data.get('portal')
    if portal not in Account.Role.values:
        raise ValidationError({'portal': 'A valid application portal is required.'})
    return portal


class WebCSRF:
    def initial(self, request, *args, **kwargs):
        # APIView is csrf_exempt; enforce Django's token AND Origin/Referer
        # checks explicitly, including unauthenticated login and logout.
        SessionAuthentication().enforce_csrf(request)
        self.portal = portal_for(request)
        super().initial(request, *args, **kwargs)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'no-store'
        return response


class WebCSRFView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        response = Response({'csrfToken': get_token(request)})
        response['Cache-Control'] = 'no-store'
        return response


class WebLoginView(WebCSRF, LoginView):
    pass


class WebRegisterView(WebCSRF, RegisterView):
    def post(self, request):
        if self.portal != Account.Role.STUDENT:
            raise ValidationError('Public registration is only available to students.')
        return super().post(request)


class WebTOTPLoginVerifyView(WebCSRF, TOTPLoginVerifyView):
    def post(self, request):
        response = super().post(request)
        if response.status_code == 200 and 'refresh' in response.data:
            token = RefreshToken(response.data.pop('refresh'))
            token['web_portal'] = self.portal
            # Refresh credentials never appear in browser-readable response data.
            response.data['access'] = str(token.access_token)
            response.set_cookie(
                cookie_name(self.portal), str(token),
                max_age=max(0, token['exp'] - int(timezone.now().timestamp())),
                secure=not settings.DEBUG, httponly=True, samesite='Lax', path='/',
            )
        return response


class WebRefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_scope = 'auth'

    def initial(self, request, *args, **kwargs):
        # Refresh is a read-only credential exchange: it only validates the
        # existing HttpOnly refresh cookie and returns a short-lived access
        # token. Do not require a CSRF token here, so a full-page redirect
        # after TOTP can restore the browser session without a second CSRF
        # bootstrap request. All state-changing browser auth endpoints remain
        # protected by WebCSRF.
        self.portal = portal_for(request)
        super().initial(request, *args, **kwargs)

    def post(self, request):
        try:
            token = RefreshToken(request.COOKIES.get(cookie_name(self.portal), ''))
            if token.get('web_portal') != self.portal:
                raise TokenError('Wrong portal')
            user = User.objects.filter(pk=token['user_id'], is_active=True,
                                       account__role=self.portal).first()
            if user is None or not TOTPDevice.objects.filter(user=user, confirmed_at__isnull=False).exists():
                raise TokenError('Session is no longer valid')
        except (TokenError, KeyError):
            response = Response({'detail': 'Session expired. Please sign in again.'}, status=401)
            clear_cookie(response, self.portal)
            return response
        # Preserve the original seven-day expiry; refresh does not extend it.
        # Return the same server-validated user representation used by /auth/me.
        # This lets browser clients restore the session without a second auth hop.
        return Response({
            'access': str(token.access_token),
            'user': _serialize_user_with_verification(user),
        })


class WebLogoutView(WebCSRF, APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            token = RefreshToken(request.COOKIES.get(cookie_name(self.portal), ''))
            if token.get('web_portal') == self.portal:
                token.blacklist()
        except TokenError:
            pass  # Idempotent, including expired/already-blacklisted cookies.
        response = Response(status=204)
        clear_cookie(response, self.portal)
        return response


class NativeTokenRefreshView(TokenRefreshView):
    def post(self, request, *args, **kwargs):
        raw = request.data.get('refresh')
        if raw:
            try:
                if RefreshToken(raw).get('web_portal'):
                    raise AuthenticationFailed('Browser sessions require the cookie refresh endpoint.')
            except TokenError as exc:
                raise InvalidToken() from exc
        return super().post(request, *args, **kwargs)
