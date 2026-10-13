from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from types import SimpleNamespace

import pyotp
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.tests import register, login, enroll_and_confirm
from accounts.totp import verify_totp_code
from accounts.totp import hash_backup_code
from accounts.models import Account, TOTPBackupCode, TOTPDevice
from accounts.mfa import create_mfa_session
from django.contrib.auth.models import User
from django.utils import timezone


class EnrollmentScopeTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_another_sessions_enrollment_does_not_upgrade_password_token(self):
        client = APIClient()
        registered = register(client).data
        old_access = registered['access']
        url = '/api/profile/' + registered['user']['student_id'] + '/'
        other_access = login(client).data['access']
        _, confirmed, _ = enroll_and_confirm(client, other_access)
        self.assertEqual(confirmed.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + old_access)
        self.assertEqual(client.get(url).status_code, 401)
        client.credentials(HTTP_AUTHORIZATION='Bearer ' + confirmed.data['access'])
        self.assertEqual(client.get(url).status_code, 200)
        self.assertIn('refresh', confirmed.data)

    def test_concurrent_totp_claim_has_one_winner(self):
        secret = pyotp.random_base32()
        code = pyotp.TOTP(secret).now()
        barrier = Barrier(2)
        original = pyotp.TOTP.verify

        def verify(totp, value, **kwargs):
            valid = original(totp, value, **kwargs)
            barrier.wait(timeout=5)
            return valid

        with patch.object(pyotp.TOTP, 'verify', verify), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: verify_totp_code(secret, code, user_id=991), range(2)))
        self.assertEqual(sorted(results), [False, True])

    def test_stale_backup_code_read_cannot_authorize_a_second_login(self):
        user = User.objects.create_user(username='backup-race@example.test')
        Account.objects.create(user=user, role='superuser')
        TOTPDevice.objects.create(user=user, secret=pyotp.random_base32(), confirmed_at=timezone.now())
        code = 'ABCDEFGHJK'
        stale = TOTPBackupCode.objects.create(user=user, code_hash=hash_backup_code(code))
        first, second = create_mfa_session(user.pk), create_mfa_session(user.pk)
        real_filter = TOTPBackupCode.objects.filter

        def selected_before_consumption(*args, **kwargs):
            if 'code_hash' in kwargs:
                return SimpleNamespace(first=lambda: stale)
            return real_filter(*args, **kwargs)

        client = APIClient()
        with patch.object(TOTPBackupCode.objects, 'filter', side_effect=selected_before_consumption):
            self.assertEqual(client.post('/api/auth/verify-totp/', {'mfa_token':first,'code':code}).status_code, 200)
            self.assertEqual(client.post('/api/auth/verify-totp/', {'mfa_token':second,'code':code}).status_code, 400)
