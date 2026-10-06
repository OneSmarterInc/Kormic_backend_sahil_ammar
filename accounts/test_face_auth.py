import base64
from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.face_auth import pending_response, seal, unseal
from accounts.face_models import MODEL_HASHES
from accounts.models import Account, StudentFaceChallenge, StudentFaceCredential, TOTPDevice


@override_settings(STUDENT_FACE_AUTH_REQUIRED=True,
                   STUDENT_FACE_ENCRYPTION_KEY=Fernet.generate_key().decode())
class FaceAuthenticationTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.user = User.objects.create_user('face-student', password='test-only-pass')
        Account.objects.create(user=self.user, role='student')
        TOTPDevice.objects.create(user=self.user, secret='JBSWY3DPEHPK3PXP', confirmed_at=timezone.now())
        self.client = APIClient()
        self.pending = pending_response(self.user)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + self.pending['access'])
        self.vector = [1.0] + [0.0] * 511

    def start(self):
        result = self.client.post('/api/auth/face/start/', {'consent': True}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        return StudentFaceChallenge.objects.get(pk=result.data['id'])

    def step(self, scan, index, vector=None, capture=None):
        StudentFaceChallenge.objects.filter(pk=scan.pk).update(step_started_at=timezone.now() - timedelta(seconds=2))
        action = scan.sequence[index]
        yaw = {'center': 0, 'left': .3, 'right': -.3}[action]
        image = base64.b64encode((capture or f'{scan.pk}-{index}').encode()).decode()
        with patch('accounts.face_auth.measure', return_value=(yaw, vector or self.vector)):
            return self.client.post(f'/api/auth/face/{scan.pk}/step/', {'step': index, 'image': image}, format='json')

    def test_pending_token_cannot_use_profile_or_refresh(self):
        self.assertNotIn('refresh', self.pending)
        response = self.client.get('/api/auth/github/status/')
        self.assertEqual(response.status_code, 401)
        response = self.client.get('/api/auth/me/')
        self.assertTrue(response.data['face_verification_required'])
        old_refresh = str(RefreshToken.for_user(self.user))
        self.assertEqual(self.client.post('/api/auth/refresh/', {'refresh': old_refresh}).status_code, 401)

    def test_pre_totp_access_cannot_start_scan(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(self.user)))
        self.assertEqual(self.client.post('/api/auth/face/start/', {'consent': True}, format='json').status_code, 401)

    def test_login_without_totp_still_requires_totp_first(self):
        TOTPDevice.objects.filter(user=self.user).delete()
        self.user.email = 'face@example.test'
        self.user.username = self.user.email
        self.user.save()
        self.client.credentials()
        result = self.client.post('/api/auth/login/', {'email': self.user.email, 'password': 'test-only-pass', 'portal': 'student'}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data['must_enroll_totp'])
        self.assertNotIn('face_pending', AccessToken(result.data['access']))

    def test_totp_login_returns_only_a_restricted_face_session(self):
        import pyotp
        self.user.email = 'face@example.test'
        self.user.username = self.user.email
        self.user.save()
        self.client.credentials()
        login = self.client.post('/api/auth/login/', {'email': self.user.email, 'password': 'test-only-pass', 'portal': 'student'}, format='json')
        result = self.client.post('/api/auth/verify-totp/', {'mfa_token': login.data['mfa_token'], 'code': pyotp.TOTP('JBSWY3DPEHPK3PXP').now(), 'portal': 'student'}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data['user']['face_verification_required'])
        self.assertTrue(AccessToken(result.data['access'])['face_pending'])
        self.assertNotIn('refresh', result.data)

    def test_browser_face_completion_sets_http_only_cookie(self):
        from accounts.web_auth import cookie_name
        pending = pending_response(self.user, 'student')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + pending['access'])
        scan = self.start()
        for i in range(4):
            result = self.step(scan, i)
        self.assertNotIn('refresh', result.data)
        self.assertTrue(result.cookies[cookie_name('student')]['httponly'])
        self.assertTrue(RefreshToken(result.cookies[cookie_name('student')].value)['face_verified'])

    def test_success_enrolls_encrypted_template_and_issues_verified_tokens(self):
        scan = self.start()
        for i in range(4):
            response = self.step(scan, i)
            self.assertEqual(response.status_code, 200, response.data)
        credential = StudentFaceCredential.objects.get(user=self.user)
        self.assertEqual(unseal(self.user, credential.encrypted_template), self.vector)
        self.assertNotIn(b'"vector"', bytes(credential.encrypted_template))
        self.assertTrue(RefreshToken(response.data['refresh'])['face_verified'])
        self.assertFalse(response.data['user']['face_verification_required'])
        self.assertEqual(self.step(scan, 3).status_code, 403)
        self.assertEqual(self.client.post('/api/auth/face/start/', {'consent': True}, format='json').status_code, 403)

    def test_wrong_face_does_not_replace_existing_enrollment(self):
        stored = seal(self.user, self.vector)
        StudentFaceCredential.objects.create(user=self.user, encrypted_template=stored,
            consent_at=timezone.now(), model_sha256=MODEL_HASHES['w600k_r50.onnx'])
        scan = self.start()
        other = [0.0, 1.0] + [0.0] * 510
        result = self.step(scan, 0, vector=other)
        self.assertEqual(result.status_code, 400)
        self.assertNotIn('access', result.data)
        self.assertEqual(bytes(StudentFaceCredential.objects.get(user=self.user).encrypted_template), stored)

    def test_replay_across_challenges_is_rejected(self):
        scan = self.start()
        self.assertEqual(self.step(scan, 0, capture='identical-capture').status_code, 200)
        scan = self.start()
        self.assertEqual(self.step(scan, 0, capture='identical-capture').status_code, 400)

    def test_expired_scan_is_rejected_and_temporary_template_cleared(self):
        scan = self.start()
        self.step(scan, 0)
        StudentFaceChallenge.objects.filter(pk=scan.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.step(scan, 1).status_code, 403)
        scan.refresh_from_db()
        self.assertFalse(scan.encrypted_reference)
        self.assertFalse(StudentFaceCredential.objects.filter(user=self.user).exists())

    def test_pending_token_cannot_use_another_logins_challenge(self):
        scan = self.start()
        fresh = pending_response(self.user)
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + fresh['access'])
        self.assertEqual(self.step(scan, 0).status_code, 403)

    def test_disabled_feature_preserves_non_face_auth(self):
        with override_settings(STUDENT_FACE_AUTH_REQUIRED=False):
            self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(self.user)))
            self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)

    def test_university_accounts_are_not_face_gated(self):
        Account.objects.filter(user=self.user).update(role='university')
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(self.user)))
        self.assertEqual(self.client.get('/api/auth/me/').status_code, 200)

    def test_cleanup_clears_abandoned_encrypted_reference(self):
        from django.core.management import call_command
        scan = self.start()
        self.step(scan, 0)
        StudentFaceChallenge.objects.filter(pk=scan.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        call_command('cleanup_face_challenges', stdout=__import__('io').StringIO())
        scan.refresh_from_db()
        self.assertFalse(scan.encrypted_reference)
        self.assertIsNotNone(scan.finished_at)
