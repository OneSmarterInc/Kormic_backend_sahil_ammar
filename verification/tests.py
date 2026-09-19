from unittest.mock import patch
from django.core.cache import cache
from django.test import TestCase
from django_api.tests import make_student_client, make_university_client
from django_api.models import StudentProfile
from verification.models import VerificationCheck, VerificationItem


class VerificationStatusGates(TestCase):
    def setUp(self):
        cache.clear()
        self.student, self.sid = make_student_client(email='verify-matrix@example.com')
        self.other, self.other_id = make_student_client(email='verify-other@example.com')

    @patch('verification.services.AIVerificationAgent.analyze', side_effect=RuntimeError('Provider unavailable'))
    def test_skipping_sources_cannot_turn_completion_into_verification(self, analyze):
        other_before = list(VerificationCheck.objects.filter(student__uuid=self.other_id).values())
        profile = StudentProfile.objects.get(uuid=self.sid)
        profile.verified = True  # stale cached flag must be corrected by authoritative status
        profile.save()
        self.student.patch('/api/v1/auth/onboarding/preferences/',
                {'github_onboarding_state': 'skipped', 'linkedin_onboarding_state': 'skipped'}, format='json')
        response = self.student.get('/api/v1/verification/status/?student_id=' + self.other_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['student_id'], self.sid)
        self.assertFalse(response.data['verified'])
        self.assertEqual(response.data['status'], 'incomplete')
        self.assertIn('resume', response.data['missing_sources'])
        profile.refresh_from_db()
        self.assertFalse(profile.verified)
        self.assertEqual(list(VerificationCheck.objects.filter(student__uuid=self.other_id).values()), other_before)

    def test_items_are_owner_scoped_and_university_cannot_read_them(self):
        for sid, message in [(self.sid, 'My mismatch'), (self.other_id, 'Other private mismatch')]:
            check, _ = VerificationCheck.objects.get_or_create(student=StudentProfile.objects.get(uuid=sid))
            VerificationItem.objects.create(verification_check=check, key='name:resume', dimension='name', message=message)
        response = self.student.get('/api/v1/verification/items/?status=all&student_id=' + self.other_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['items'][0]['message'], 'My mismatch')
        officer, _ = make_university_client(email='verify-officer@example.com', university_id='verify-university')
        self.assertEqual(officer.get('/api/v1/verification/items/').status_code, 403)
        self.assertEqual(officer.get('/api/v1/verification/status/').status_code, 403)
