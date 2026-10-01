from unittest.mock import patch
from django.test import TestCase
from django.contrib.auth.models import User
from django_api.models import StudentProfile, ResumeUpload
from accounts.models import Account
from accounts.views import _serialize_user_with_verification


class AuthDoesNotRunInferenceTests(TestCase):
    @patch('verification.services.AIVerificationAgent.analyze', side_effect=AssertionError('Model busy'))
    def test_student_restore_remains_available_when_evidence_changes_and_model_is_busy(self, analyze):
        profile = StudentProfile.objects.create(name='Student', email='student@example.test')
        user = User.objects.create(username='student@example.test', email='student@example.test')
        Account.objects.create(user=user, role='student', student_profile=profile)
        ResumeUpload.objects.create(student=profile, extracted_data={'name':'Student'})
        payload = _serialize_user_with_verification(user)
        self.assertEqual(payload['role'], 'student')
        self.assertTrue(payload['verification']['analysis_pending'])
        self.assertTrue(payload['verification_required'])
        analyze.assert_not_called()
