import pyotp
from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken

from accounts.models import Account, TOTPDevice
from accounts.web_auth import cookie_name


@override_settings(
    DEBUG=False,
    CSRF_TRUSTED_ORIGINS=['https://university.kormic.ai'],
    CORS_ALLOWED_ORIGINS=['https://university.kormic.ai'],
    CSRF_COOKIE_SECURE=True,
    TOTP_SECRET_KEYS=(Fernet.generate_key().decode(),),
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class WebAuthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)
        self.user = User.objects.create_user('web@example.com', email='web@example.com', password='Password!123')
        Account.objects.create(user=self.user, role=Account.Role.UNIVERSITY)
        self.seed = pyotp.random_base32()
        TOTPDevice.objects.create(user=self.user, secret=self.seed, confirmed_at=timezone.now())
        response = self.client.get('/api/auth/web/csrf/', secure=True)
        self.csrf = response.data['csrfToken']

    def post(self, path, data=None, **headers):
        return self.client.post('/api/auth/web/' + path + '/',
                                {'portal': 'university', **(data or {})}, format='json', secure=True,
                                HTTP_X_CSRFTOKEN=self.csrf,
                                HTTP_ORIGIN=headers.pop('HTTP_ORIGIN', 'https://university.kormic.ai'), **headers)

    def login(self):
        password = self.post('login', {'email': self.user.email, 'password': 'Password!123'})
        self.assertEqual(password.status_code, 200)
        self.assertNotIn(cookie_name('university'), password.cookies)
        return self.post('verify-totp', {'mfa_token': password.data['mfa_token'], 'code': pyotp.TOTP(self.seed).now()})

    def test_cookie_flags_no_refresh_in_json_and_short_access(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('refresh', response.data)
        cookie = response.cookies[cookie_name('university')]
        self.assertTrue(cookie['secure'])
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        self.assertEqual(cookie['path'], '/')
        self.assertEqual(cookie['domain'], '')
        self.assertLessEqual(int(cookie['max-age']), 7 * 86400)
        self.assertEqual(response['Cache-Control'], 'no-store')
        token = AccessToken(response.data['access'])
        self.assertEqual(token['exp'] - token['iat'], 300)

    def test_reload_refresh_and_logout_revoke_cookie_without_access_header(self):
        self.login()
        raw = self.client.cookies[cookie_name('university')].value
        refreshed = self.post('refresh')
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(set(refreshed.data), {'access', 'user'})
        self.assertEqual(refreshed.data['user']['role'], 'university')
        self.assertEqual(self.post('logout').status_code, 204)
        self.assertEqual(self.client.cookies[cookie_name('university')]['max-age'], 0)
        self.client.cookies[cookie_name('university')] = raw
        self.assertEqual(self.post('refresh').status_code, 401)
        self.assertEqual(self.post('logout').status_code, 204)

    def test_missing_csrf_and_untrusted_origin_cannot_login_refresh_or_logout(self):
        for path in ['login', 'verify-totp', 'register', 'refresh', 'logout']:
            response = self.client.post(f'/api/auth/web/{path}/', {'portal': 'university'}, format='json', secure=True)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.post(path, HTTP_ORIGIN='https://evil.example').status_code, 403)

    def test_wrong_portal_rejected_before_mfa_and_cookie_isolation(self):
        response = self.post('login', {'portal': 'institute', 'email': self.user.email, 'password': 'Password!123'})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('mfa_token', response.data)
        self.login()
        self.assertEqual(self.post('refresh', {'portal': 'institute'}).status_code, 401)
        self.assertEqual(self.post('logout', {'portal': 'institute'}).status_code, 204)
        self.assertEqual(self.post('refresh').status_code, 200)

    def test_refresh_cannot_be_supplied_in_json_or_exchanged_on_native_endpoint(self):
        self.login()
        raw = self.client.cookies[cookie_name('university')].value
        del self.client.cookies[cookie_name('university')]
        self.assertEqual(self.post('refresh', {'refresh': raw}).status_code, 401)
        response = self.client.post('/api/auth/refresh/', {'refresh': raw}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_native_refresh_remains_supported_without_csrf_or_cookies(self):
        raw = str(RefreshToken.for_user(self.user))
        response = self.client.post('/api/auth/refresh/', {'refresh': raw}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)
        self.assertFalse(response.cookies)

    def test_refresh_rechecks_active_role_and_totp(self):
        self.login()
        self.user.account.role = Account.Role.INSTITUTE
        self.user.account.save()
        self.assertEqual(self.post('refresh').status_code, 401)

    def test_invalid_and_expired_cookies_fail_closed(self):
        token = RefreshToken.for_user(self.user)
        token['web_portal'] = 'university'
        token['exp'] = 1
        for raw in ['corrupted', str(token)]:
            self.client.cookies[cookie_name('university')] = raw
            self.assertEqual(self.post('refresh').status_code, 401)

    def test_cors_only_trusted_origins_get_credentials(self):
        allowed = self.client.get('/api/auth/web/csrf/', HTTP_ORIGIN='https://university.kormic.ai', secure=True)
        self.assertEqual(allowed['Access-Control-Allow-Origin'], 'https://university.kormic.ai')
        self.assertEqual(allowed['Access-Control-Allow-Credentials'], 'true')
        denied = self.client.get('/api/auth/web/csrf/', HTTP_ORIGIN='https://evil.example', secure=True)
        self.assertNotIn('Access-Control-Allow-Origin', denied)
