from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError

from accounts.models import Account, TOTPDevice
from django_api.models import StudentProfile


class AdminUserMenuTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('menu-admin', email='admin@example.test')
        Account.objects.create(user=self.admin, role='superuser')
        TOTPDevice.objects.create(user=self.admin, secret_encrypted='fixture', confirmed_at=timezone.now())
        self.profile = StudentProfile.objects.create(name='Original', email='student@example.test')
        self.user = User.objects.create_user('student@example.test', email='student@example.test', password='Original-password-42!')
        Account.objects.create(user=self.user, role='student', student_profile=self.profile)
        self.refresh = str(RefreshToken.for_user(self.user))
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.url = f'/api/superuser/users/{self.user.pk}/'

    def test_update_name_email_keeps_login_and_profile_consistent(self):
        response = self.client.patch(self.url, {'name': 'Updated', 'email': 'NEW@example.test'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db(); self.profile.refresh_from_db()
        self.assertEqual(self.user.username, 'new@example.test')
        self.assertEqual(self.profile.email, self.user.email)
        self.assertEqual(self.profile.name, self.user.first_name)
        with self.assertRaises(TokenError): RefreshToken(self.refresh)

    def test_duplicate_email_does_not_change_account(self):
        User.objects.create_user('other@example.test', email='other@example.test')
        response = self.client.patch(self.url, {'email': 'OTHER@example.test'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'student@example.test')

    def test_deactivate_and_reactivate(self):
        for active in (False, True):
            response = self.client.patch(self.url, {'is_active': active}, format='json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data['is_active'], active)

    def test_invalid_active_value_rejected(self):
        self.assertEqual(self.client.patch(self.url, {'is_active': 'invalid'}, format='json').status_code, 400)

    def test_totp_removal_revokes_old_refresh_tokens(self):
        TOTPDevice.objects.create(user=self.user, secret_encrypted='fixture', confirmed_at=timezone.now())
        response = self.client.post(self.url + 'remove-totp/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['totp_enrolled'])
        with self.assertRaises(TokenError): RefreshToken(self.refresh)

    def test_reset_password_and_revoke_sessions(self):
        response = self.client.post(self.url + 'reset-password/', {'password': 'Updated-password-93!'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Updated-password-93!'))
        with self.assertRaises(TokenError): RefreshToken(self.refresh)

    def test_delete_login_preserves_profile(self):
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(StudentProfile.objects.filter(pk=self.profile.pk).exists())

    def test_delete_profile_removes_profile_and_login(self):
        self.assertEqual(self.client.delete(f'/api/superuser/students/{self.profile.uuid}/').status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertFalse(StudentProfile.objects.filter(pk=self.profile.pk).exists())

    def test_student_cannot_use_admin_actions(self):
        TOTPDevice.objects.create(user=self.user, secret_encrypted='fixture', confirmed_at=timezone.now())
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.patch(self.url, {'name': 'Unauthorized'}, format='json').status_code, 403)

    def test_admin_cannot_disable_self(self):
        response = self.client.patch(f'/api/superuser/users/{self.admin.pk}/', {'is_active': False}, format='json')
        self.assertEqual(response.status_code, 400)
