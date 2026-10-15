from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, PasswordResetDelivery
from accounts.reset_delivery import delivery_code, retry_reset_deliveries


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class ResetDeliveryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(username='Student@Example.com', email='Student@Example.com', password='S3curePassw0rd!')
        Account.objects.create(user=self.user, role='student')

    def request_code(self):
        return self.client.post('/api/auth/forgot-password/', {'email': self.user.email}, format='json')

    def test_smtp_failure_retries_same_code_and_completes_app_reset(self):
        with patch('accounts.email.send_mail', side_effect=OSError('offline')):
            self.assertEqual(self.request_code().status_code, 200)
        job = PasswordResetDelivery.objects.get()
        self.assertIsNone(job.sent_at)
        self.assertEqual(job.last_error, 'OSError')
        PasswordResetDelivery.objects.filter(pk=job.pk).update(available_at=timezone.now()-timedelta(seconds=1))
        retry_reset_deliveries.run()
        job.refresh_from_db()
        self.assertIsNotNone(job.sent_at)
        self.assertEqual(job.attempts, 2)
        code = delivery_code(job.user_id, job.nonce)
        self.assertIn(code, mail.outbox[-1].body)
        response = self.client.post('/api/auth/reset-password/verify-otp/', {'email': self.user.email, 'otp': code}, format='json')
        self.assertEqual(response.status_code, 200)
        token = response.data['reset_token']
        response = self.client.post('/api/auth/reset-password/confirm/', {'reset_token': token, 'new_password': 'NewS3curePassw0rd!'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewS3curePassw0rd!'))
        retry_reset_deliveries.run()
        self.assertEqual(len(mail.outbox), 2)

    def test_superseded_or_expired_codes_are_not_delivered(self):
        with patch('accounts.email.send_mail', return_value=0):
            self.request_code()
            old = PasswordResetDelivery.objects.get()
            cache.delete('pwreset:cooldown:student@example.com')
            self.request_code()
        PasswordResetDelivery.objects.update(available_at=timezone.now()-timedelta(seconds=1))
        with patch('accounts.email.send_mail', return_value=1) as send:
            retry_reset_deliveries.run()
        self.assertFalse(PasswordResetDelivery.objects.filter(pk=old.pk).exists())
        send.assert_called_once()

    def test_zero_acceptance_is_a_retryable_failure(self):
        with patch('accounts.email.send_mail', return_value=0):
            self.request_code()
        self.assertIsNone(PasswordResetDelivery.objects.get().sent_at)
