import os
import runpy
from pathlib import Path
from unittest.mock import patch
from django.http import HttpResponse
from django.test import SimpleTestCase, RequestFactory, override_settings
from django.middleware.security import SecurityMiddleware
from accounts.proxy import TrustedProxyMiddleware


class ProductionSecurityTests(SimpleTestCase):
    def test_production_defaults_and_no_default_database_password(self):
        with patch.dict(os.environ, {'DJANGO_DEBUG':'false', 'DJANGO_SECRET_KEY':'isolated-test-secret', 'EMAIL_MODE':'dev'}, clear=True), patch('dotenv.load_dotenv'):
            config = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'kormic_backend/settings.py'))
        self.assertTrue(config['SECURE_SSL_REDIRECT'])
        self.assertTrue(config['SESSION_COOKIE_SECURE'])
        self.assertGreater(config['SECURE_HSTS_SECONDS'], 0)
        self.assertIsNone(config['SECURE_PROXY_SSL_HEADER'])
        self.assertEqual(config['DATABASES']['default']['PASSWORD'], '')
        self.assertIn('drf_spectacular', config['INSTALLED_APPS'])
        self.assertFalse(any('Browsable' in v for v in config['REST_FRAMEWORK']['DEFAULT_RENDERER_CLASSES']))

    @override_settings(TRUSTED_PROXY_CIDRS=('192.0.2.10/32',), SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https'), SECURE_SSL_REDIRECT=True, SECURE_HSTS_SECONDS=3600, ALLOWED_HOSTS=['testserver'])
    def test_untrusted_forwarded_header_cannot_bypass_https_redirect(self):
        app = TrustedProxyMiddleware(SecurityMiddleware(lambda r: HttpResponse('ok')))
        factory = RequestFactory()
        untrusted = app(factory.get('/api/v1/auth/me/', REMOTE_ADDR='198.51.100.2', HTTP_X_FORWARDED_PROTO='https'))
        self.assertEqual(untrusted.status_code, 301)
        trusted = app(factory.get('/api/v1/auth/me/', REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_PROTO='https'))
        self.assertEqual(trusted.status_code, 200)
        self.assertIn('max-age=3600', trusted['Strict-Transport-Security'])


    @override_settings(TRUSTED_PROXY_CIDRS=('192.0.2.10/32',))
    def test_claim_ip_budget_ignores_spoofed_forwarding_prefix(self):
        from institutes_list.throttling import ClaimConfirmIPThrottle
        throttle = ClaimConfirmIPThrottle()
        app = TrustedProxyMiddleware(lambda r: throttle.get_cache_key(r, None))
        factory = RequestFactory()
        first = app(factory.get('/', REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_FOR='attacker-one, 198.51.100.2'))
        second = app(factory.get('/', REMOTE_ADDR='192.0.2.10', HTTP_X_FORWARDED_FOR='attacker-two, 198.51.100.2'))
        self.assertEqual(first, second)
        direct = app(factory.get('/', REMOTE_ADDR='198.51.100.2', HTTP_X_FORWARDED_FOR='forged'))
        self.assertEqual(first, direct)
