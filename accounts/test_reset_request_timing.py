from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import PasswordResetDelivery
from accounts.password_reset import is_resend_throttled
from accounts.reset_delivery import request_password_reset


class PasswordResetRequestTimingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.active = User.objects.create_user(
            username='active', email='active@example.com',
        )
        User.objects.create_user(
            username='inactive', email='inactive@example.com', is_active=False,
        )

    def request(self, email):
        return self.client.post('/api/auth/forgot-password/', {'email': email}, format='json')

    def test_every_address_queues_without_user_lookup_or_delivery_writes(self):
        responses = []
        for email in ('active@example.com', 'inactive@example.com', 'unknown@example.com'):
            with self.subTest(email=email), \
                 patch('accounts.reset_delivery.request_password_reset.delay') as enqueue, \
                 self.assertNumQueries(0):
                response = self.request(email)
                responses.append((response.status_code, response.data))
                enqueue.assert_called_once_with(email)
                self.assertTrue(is_resend_throttled(email))
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses, [responses[0]] * 3)
        self.assertFalse(PasswordResetDelivery.objects.exists())

    def test_cooldown_suppresses_queueing_equally_for_known_and_unknown(self):
        for email in ('active@example.com', 'unknown@example.com'):
            with self.subTest(email=email), \
                 patch('accounts.reset_delivery.request_password_reset.delay') as enqueue:
                first = self.request(email)
                second = self.request(email)
                enqueue.assert_called_once_with(email)
                self.assertEqual(first.data, second.data)
                self.assertEqual(second.status_code, 200)

    def test_broker_failure_is_generic_and_does_not_start_cooldown(self):
        responses = []
        for email in ('active@example.com', 'unknown@example.com'):
            with self.subTest(email=email), \
                 patch('accounts.reset_delivery.request_password_reset.delay', side_effect=OSError('private broker detail')), \
                 self.assertNumQueries(0):
                response = self.request(email)
                responses.append(response.data)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('private broker detail', str(response.data))
                self.assertFalse(is_resend_throttled(email))
        self.assertEqual(responses[0], responses[1])

    def test_worker_only_delivers_for_active_users(self):
        with patch('accounts.reset_delivery.request_reset_delivery') as deliver, \
             patch('project_superuser.services.log_activity') as audit:
            request_password_reset.run('unknown@example.com')
            request_password_reset.run('inactive@example.com')
            deliver.assert_not_called()
            audit.assert_not_called()
            request_password_reset.run('ACTIVE@example.com')
            deliver.assert_called_once_with(self.active)
            audit.assert_called_once()

    def test_worker_task_is_imported_by_celery_discovery(self):
        from accounts import tasks

        self.assertIs(tasks.request_password_reset, request_password_reset)
