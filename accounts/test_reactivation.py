from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient
from accounts.mfa import record_totp_failure, is_totp_throttled
from accounts.models import Account
from accounts.tokens import AccessToken
from project_superuser.tests import make_superuser_client


class ReactivationTests(TestCase):
    def test_reactivation_allows_fresh_login_and_keeps_old_sessions_revoked(self):
        cache.clear()
        admin = make_superuser_client()
        user = User.objects.create_user(username='Legacy.Student@Example.com', email='Legacy.Student@Example.com', password='S3curePassw0rd!')
        Account.objects.create(user=user, role='student')
        old_access = str(AccessToken.for_user(user))
        client = APIClient()
        payload = {'email': 'legacy.student@example.com', 'password': 'S3curePassw0rd!', 'portal': 'student'}
        path = f'/api/superuser/users/{user.pk}/'
        self.assertEqual(admin.patch(path, {'is_active': False}, format='json').status_code, 200)
        self.assertEqual(client.post('/api/auth/login/', payload, format='json').status_code, 403)
        for _ in range(5):
            record_totp_failure(user.pk)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(admin.patch(path, {'is_active': True}, format='json').status_code, 200)
        self.assertFalse(is_totp_throttled(user.pk))
        fresh = client.post('/api/auth/login/', payload, format='json')
        self.assertEqual(fresh.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {old_access}')
        self.assertEqual(client.get('/api/auth/me/').status_code, 401)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {fresh.data['access']}")
        self.assertEqual(client.get('/api/auth/me/').status_code, 200)
