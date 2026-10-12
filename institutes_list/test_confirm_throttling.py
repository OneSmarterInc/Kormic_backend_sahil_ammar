from copy import deepcopy
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient


class ClaimConfirmThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        config = deepcopy(settings.REST_FRAMEWORK)
        config['DEFAULT_THROTTLE_RATES'].update(claim_confirm_ip='2/min', claim_confirm_session='1/min')
        self.override = override_settings(REST_FRAMEWORK=config)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(cache.clear)

    def test_repeated_session_is_limited_independently_of_ip(self):
        body = {'claim_session': 'invalid-signed-session', 'fields': {}}
        first = self.client.post('/api/claim/confirm/', body, format='json', REMOTE_ADDR='192.0.2.1')
        self.assertEqual(first.status_code, 400)
        second = self.client.post('/api/claim/confirm/', body, format='json', REMOTE_ADDR='192.0.2.2')
        self.assertEqual(second.status_code, 429)
        self.assertIn('Retry-After', second)

    def test_missing_sessions_are_still_limited_by_ip(self):
        for _ in range(2):
            self.assertEqual(self.client.post('/api/claim/confirm/', {}, format='json').status_code, 400)
        self.assertEqual(self.client.post('/api/claim/confirm/', {}, format='json').status_code, 429)
