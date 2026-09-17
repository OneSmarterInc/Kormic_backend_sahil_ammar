import importlib
from io import StringIO
from types import SimpleNamespace
from unittest import mock

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from django.apps import apps
from django.contrib.auth.models import User
from django.core import serializers
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.crypto import TOTPEncryptionError, decrypt_totp_secret
from accounts.models import Account, TOTPDevice

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()


@override_settings(TOTP_SECRET_KEYS=(OLD_KEY,))
class TOTPEncryptionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('seed@example.com', password='TestPassword!123')
        self.seed = pyotp.random_base32()

    def test_database_and_serialization_contain_ciphertext_only(self):
        device = TOTPDevice.objects.create(user=self.user, secret=self.seed)
        with connection.cursor() as cursor:
            cursor.execute('SELECT secret_encrypted FROM accounts_totpdevice WHERE id = %s', [device.pk])
            stored = cursor.fetchone()[0]
        self.assertNotIn(self.seed, stored)
        self.assertEqual(Fernet(OLD_KEY.encode()).decrypt(stored.encode()).decode(), self.seed)
        device.refresh_from_db()
        self.assertEqual(device.secret, self.seed)
        self.assertNotIn(self.seed, serializers.serialize('json', [device]))
        self.assertFalse(TOTPDevice._meta.get_field('secret_encrypted').editable)
        self.assertNotIn('secret', [field.name for field in TOTPDevice._meta.fields])

    def test_missing_wrong_and_tampered_keys_fail_closed(self):
        device = TOTPDevice.objects.create(user=self.user, secret=self.seed)
        for keys in ((), ('invalid',), (NEW_KEY,)):
            with self.subTest(keys=len(keys)), override_settings(TOTP_SECRET_KEYS=keys):
                with self.assertRaises(TOTPEncryptionError):
                    _ = device.secret
        device.secret_encrypted = 'plaintext-is-not-accepted'
        with self.assertRaises(TOTPEncryptionError):
            _ = device.secret

    def test_backfill_preserves_existing_authenticator_and_confirmation(self):
        confirmed = timezone.now()
        device = TOTPDevice.objects.create(user=self.user, secret_encrypted=self.seed, confirmed_at=confirmed)
        migration = importlib.import_module('accounts.migrations.0002_encrypt_totp_secrets')
        with transaction.atomic():
            migration.encrypt_existing_secrets(apps, SimpleNamespace(connection=connection))
        device.refresh_from_db()
        self.assertNotEqual(device.secret_encrypted, self.seed)
        self.assertEqual(device.secret, self.seed)
        self.assertEqual(device.confirmed_at, confirmed)
        self.assertTrue(pyotp.TOTP(device.secret).verify(pyotp.TOTP(self.seed).now()))

    def test_backfill_refuses_to_run_without_key(self):
        device = TOTPDevice.objects.create(user=self.user, secret_encrypted=self.seed)
        migration = importlib.import_module('accounts.migrations.0002_encrypt_totp_secrets')
        with override_settings(TOTP_SECRET_KEYS=()), self.assertRaises(TOTPEncryptionError):
            migration.encrypt_existing_secrets(apps, SimpleNamespace(connection=connection))
        device.refresh_from_db()
        self.assertEqual(device.secret_encrypted, self.seed)

    def test_rotation_and_old_key_removal_keep_authenticator_working(self):
        device = TOTPDevice.objects.create(user=self.user, secret=self.seed)
        with override_settings(TOTP_SECRET_KEYS=(NEW_KEY, OLD_KEY)):
            self.assertEqual(device.secret, self.seed)
            with self.assertRaises(CommandError):
                call_command('rotate_totp_secrets', check=True, stdout=StringIO())
            call_command('rotate_totp_secrets', stdout=StringIO())
            call_command('rotate_totp_secrets', check=True, stdout=StringIO())
        device.refresh_from_db()
        with override_settings(TOTP_SECRET_KEYS=(NEW_KEY,)):
            self.assertEqual(device.secret, self.seed)
        with self.assertRaises(InvalidToken):
            Fernet(OLD_KEY.encode()).decrypt(device.secret_encrypted.encode())

    def test_corrupt_row_rolls_back_entire_rotation(self):
        device = TOTPDevice.objects.create(user=self.user, secret=self.seed)
        original = device.secret_encrypted
        other = User.objects.create_user('bad@example.com')
        TOTPDevice.objects.create(user=other, secret_encrypted='corrupted')
        with override_settings(TOTP_SECRET_KEYS=(NEW_KEY, OLD_KEY)), self.assertRaises(CommandError):
            call_command('rotate_totp_secrets', stdout=StringIO())
        device.refresh_from_db()
        self.assertEqual(device.secret_encrypted, original)

    def test_key_error_response_does_not_leak_secret_or_exception(self):
        Account.objects.create(user=self.user, role=Account.Role.INSTITUTE)
        TOTPDevice.objects.create(user=self.user, secret=self.seed)
        client = APIClient()
        client.force_authenticate(self.user)
        with override_settings(TOTP_SECRET_KEYS=(NEW_KEY,)):
            response = client.post('/api/auth/totp/enroll/')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(self.seed, str(response.data))
        self.assertNotIn(NEW_KEY, str(response.data))


@override_settings(
    TOTP_SECRET_KEYS=(OLD_KEY,),
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class PortalLoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.password = 'PortalPassword!123'
        self.users = {}
        for role in Account.Role.values:
            user = User.objects.create_user(f'{role}@example.com', email=f'{role}@example.com', password=self.password)
            Account.objects.create(user=user, role=role)
            self.users[role] = user

    def login(self, role, portal, password=None):
        return self.client.post('/api/auth/login/', {
            'email': self.users[role].email, 'password': password or self.password, 'portal': portal,
        }, format='json')

    def test_all_wrong_portals_reject_before_enrollment_or_mfa(self):
        for enrolled in (False, True):
            for role, user in self.users.items():
                if enrolled:
                    TOTPDevice.objects.create(user=user, secret=pyotp.random_base32(), confirmed_at=timezone.now())
                for portal in Account.Role.values:
                    if role == portal:
                        continue
                    with self.subTest(role=role, portal=portal, enrolled=enrolled):
                        cache.clear()
                        with mock.patch('accounts.views.create_mfa_session') as create, mock.patch('accounts.views.RefreshToken.for_user') as issue:
                            response = self.login(role, portal)
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(response.data, {'detail': 'Invalid credentials.'})
                        create.assert_not_called()
                        issue.assert_not_called()

    def test_wrong_password_and_wrong_portal_have_same_response(self):
        wrong_role = self.login('institute', 'university')
        wrong_password = self.login('institute', 'institute', password='wrong')
        self.assertEqual(wrong_role.data, wrong_password.data)
        self.assertEqual(wrong_role.status_code, wrong_password.status_code)

    def test_matching_portal_can_enroll(self):
        for role in Account.Role.values:
            cache.clear()
            response = self.login(role, role)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data['must_enroll_totp'])
            self.assertIn('access', response.data)

    def test_bound_challenge_rejects_other_portal_and_omission(self):
        seed = pyotp.random_base32()
        TOTPDevice.objects.create(user=self.users['institute'], secret=seed, confirmed_at=timezone.now())
        token = self.login('institute', 'institute').data['mfa_token']
        for extra in ({'portal': 'university'}, {}):
            response = self.client.post('/api/auth/verify-totp/', {'mfa_token': token, 'code': pyotp.TOTP(seed).now(), **extra}, format='json')
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.data, {'detail': 'Invalid credentials.'})
        response = self.client.post('/api/auth/verify-totp/', {'mfa_token': token, 'code': pyotp.TOTP(seed).now(), 'portal': 'institute'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertIn('access', response.data)

    def test_role_change_or_deactivation_invalidates_challenge(self):
        for deactivate in (False, True):
            cache.clear()
            user = self.users['university']
            user.is_active = True
            user.save(update_fields=['is_active'])
            Account.objects.filter(user=user).update(role='university')
            TOTPDevice.objects.update_or_create(user=user, defaults={'secret': pyotp.random_base32(), 'confirmed_at': timezone.now()})
            token = self.login('university', 'university').data['mfa_token']
            if deactivate:
                user.is_active = False
                user.save(update_fields=['is_active'])
            else:
                Account.objects.filter(user=user).update(role='institute')
            response = self.client.post('/api/auth/verify-totp/', {'mfa_token': token, 'code': '123456', 'portal': 'university'}, format='json')
            self.assertEqual(response.status_code, 401)
            self.assertNotIn('access', response.data)
