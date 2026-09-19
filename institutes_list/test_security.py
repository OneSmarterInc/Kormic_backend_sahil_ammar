import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken
from accounts.models import Account, TOTPDevice
from accounts.permissions import IsTOTPEnrolled
from institutes.services import register_institute
from .models import UniversityStudentList, ListedStudent
from .otp import hash_otp, check_otp
from .source_files import source_path
from .tasks import cache_claim_otp_code, claim_otp_cache_key

CSV = b'full_name,email,field_of_study,degree_level,expected_graduation\nAda,ada@example.test,Physics,Bachelor,2028\n'


class InstituteSecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.override = override_settings(MEDIA_ROOT=self.media.name)
        self.override.enable(); self.addCleanup(self.override.disable)
        self.institute = register_institute('Security Institute', contact_email='owner@example.test')
        self.user = get_user_model().objects.create_user(username='institute', password='test-password')
        Account.objects.create(user=self.user, role=Account.Role.INSTITUTE, institute=self.institute)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION='Bearer ' + str(AccessToken.for_user(self.user)))
        self.source = UniversityStudentList.objects.create(institute=self.institute, contact_name='Owner', contact_email='owner@example.test')
        self.row = ListedStudent.objects.create(source_list=self.source, institute_id=str(self.institute.uuid), full_name='Ada', email='ada@example.test', field_of_study='Physics', degree_level='Bachelor', expected_graduation='2028')

    def enroll(self):
        TOTPDevice.objects.create(user=self.user, secret='JBSWY3DPEHPK3PXP', confirmed_at=timezone.now())

    def upload(self, data=CSV, name='roster.csv', content_type='text/csv'):
        return self.client.post('/api/v1/institute-lists/upload/', {'file': SimpleUploadedFile(name, data, content_type=content_type), 'institute_id': str(self.institute.uuid), 'contact_name': 'Owner', 'contact_email': 'owner@example.test'}, format='multipart')

    def test_pre_enrollment_tokens_cannot_use_any_intake_endpoint(self):
        base = f'/api/v1/institute-lists/lists/{self.source.pk}'
        with patch('institutes_list.views.send_invite_email_task.delay') as deliver:
            for method, path in [('post', '/api/v1/institute-lists/upload/'), ('get', '/api/v1/institute-lists/lists/'), ('get', base + '/students/'), ('get', base + '/file/'), ('post', base + '/send-invites/'), ('post', base + f'/students/{self.row.pk}/send-invite/')]:
                with self.subTest(path=path):
                    self.assertEqual(getattr(self.client, method)(path).status_code, 403)
            deliver.assert_not_called()
        self.enroll()
        self.assertEqual(self.client.get('/api/v1/institute-lists/lists/').status_code, 200)

    def test_bounded_csv_upload_and_streamed_private_download(self):
        self.enroll()
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        source = UniversityStudentList.objects.get(pk=response.data['list_id'])
        self.assertFalse(Path(source.source_file_path).is_absolute())
        self.assertEqual(source_path(source.source_file_path).read_bytes(), CSV)
        response = self.client.get(f'/api/v1/institute-lists/lists/{source.pk}/file/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), CSV)
        response.close()
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        with self.assertRaises(ValueError): source_path('../outside.csv')
        with self.assertRaises(ValueError): source_path('/etc/passwd')

    def test_oversized_malformed_and_non_csv_uploads_have_no_side_effects(self):
        self.enroll()
        with override_settings(INSTITUTE_ROSTER_MAX_BYTES=20):
            self.assertEqual(self.upload().status_code, 413)
        with override_settings(INSTITUTE_ROSTER_MAX_ROWS=1):
            self.assertEqual(self.upload(CSV + b'Bob,bob@example.test,Math,Bachelor,2028\n').status_code, 413)
        for body, name, kind in [(CSV, 'roster.html', 'text/html'), (b'\xff\x00', 'roster.csv', 'text/csv'), (b'wrong,headers\n1,2', 'roster.csv', 'text/csv'), (CSV + b'"unterminated', 'roster.csv', 'text/csv')]:
            self.assertEqual(self.upload(body, name, kind).status_code, 400)
        self.assertEqual(UniversityStudentList.objects.count(), 1)
        self.assertEqual(ListedStudent.objects.count(), 1)
        self.assertFalse(list(Path(self.media.name).rglob('*.csv')))

    def test_otp_hash_is_unique_keyed_and_legacy_hash_is_not_accepted(self):
        first, second = hash_otp('123456'), hash_otp('123456')
        self.assertNotEqual(first, second)
        self.assertTrue(check_otp('123456', first))
        self.assertFalse(check_otp('654321', first))
        self.assertFalse(check_otp('123456', hashlib.sha256(b'123456').hexdigest()))
        with override_settings(SECRET_KEY='different-secret'):
            self.assertFalse(check_otp('123456', first))

    def test_successful_verification_consumes_otp_and_deletes_delivery_payload(self):
        digest = hash_otp('123456')
        self.row.otp_hash = digest
        self.row.otp_expires_at = timezone.now() + timezone.timedelta(minutes=10)
        self.row.save()
        cache_claim_otp_code(self.row.pk, digest, '123456', timeout=600)
        anonymous = APIClient()
        body = {'token': self.row.claim_token, 'code': '123456'}
        self.assertEqual(anonymous.post('/api/v1/claim/verify/', body).status_code, 200)
        self.assertIsNone(cache.get(claim_otp_cache_key(self.row.pk, digest)))
        self.assertEqual(anonymous.post('/api/v1/claim/verify/', body).status_code, 400)

    def test_raw_file_expires_before_structured_roster(self):
        self.enroll()
        response = self.upload()
        source = UniversityStudentList.objects.get(pk=response.data['list_id'])
        path = source_path(source.source_file_path)
        UniversityStudentList.objects.filter(pk=source.pk).update(created_at=timezone.now()-timezone.timedelta(days=31))
        from accounts.privacy_tasks import apply_retention
        apply_retention()
        source.refresh_from_db()
        self.assertFalse(path.exists())
        self.assertEqual(source.source_file_path, '')
        self.assertEqual(source.students.count(), 1)

    def test_authenticated_views_cannot_silently_replace_the_totp_gate(self):
        from accounts import views
        exemptions = {views.TOTPEnrollView, views.TOTPVerifyEnrollmentView, views.CurrentUserView, views.LogoutView}
        def inspect(patterns):
            for pattern in patterns:
                if isinstance(pattern, URLResolver):
                    inspect(pattern.url_patterns)
                elif isinstance(pattern, URLPattern):
                    cls = getattr(pattern.callback, 'cls', None)
                    if cls is None: continue
                    permissions = cls.permission_classes
                    if IsAuthenticated in permissions and cls not in exemptions:
                        self.assertIn(IsTOTPEnrolled, permissions, f'{cls.__module__}.{cls.__name__}')
        inspect(get_resolver().url_patterns)
