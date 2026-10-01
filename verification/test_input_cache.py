from unittest.mock import patch
from django.test import TestCase
from django_api.models import StudentProfile, ResumeUpload
from verification.services import run_verification
from verification.models import VerificationCheck


class VerificationInputCacheTests(TestCase):
    def setUp(self):
        self.profile = StudentProfile.objects.create(name='Cache test', email='cache@example.test')

    @patch('verification.services.AIVerificationAgent.analyze', side_effect=AssertionError('Auth must not use AI'))
    def test_auth_snapshot_never_waits_for_ai(self, analyze):
        result = run_verification(str(self.profile.uuid), allow_analysis=False)
        self.assertTrue(result['analysis_pending'])
        self.assertFalse(result['verified'])
        analyze.assert_not_called()

    @patch('verification.services.AIVerificationAgent.analyze', return_value={'missing_sources': [], 'candidates': []})
    def test_changed_sources_are_pending_without_reanalysis_on_auth(self, analyze):
        run_verification(str(self.profile.uuid))
        ResumeUpload.objects.create(student=self.profile, extracted_data={'name': 'New evidence'})
        result = run_verification(str(self.profile.uuid), allow_analysis=False)
        self.assertFalse(result['verified'])
        self.assertTrue(result['analysis_pending'])
        self.assertEqual(analyze.call_count, 1)

    @patch('verification.services.AIVerificationAgent.analyze', return_value={'missing_sources': ['resume'], 'candidates': []})
    def test_unchanged_inputs_reuse_ai_and_profile_edit_invalidates(self, analyze):
        first = run_verification(str(self.profile.uuid))
        second = run_verification(str(self.profile.uuid))
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(first, second)
        self.profile.major = 'Physics'
        self.profile.save()
        run_verification(str(self.profile.uuid))
        self.assertEqual(analyze.call_count, 2)

    @patch('verification.services.AIVerificationAgent.analyze', return_value={'missing_sources': [], 'candidates': []})
    def test_changed_github_email_and_force_rerun(self, analyze):
        with patch('verification.services._resolve_github_verified_email', return_value='old@example.test'):
            run_verification(str(self.profile.uuid))
        with patch('verification.services._resolve_github_verified_email', return_value='new@example.test'):
            run_verification(str(self.profile.uuid))
            run_verification(str(self.profile.uuid), force=True)
        self.assertEqual(analyze.call_count, 3)

    @patch('verification.services.AIVerificationAgent.analyze', return_value={'missing_sources': [], 'candidates': []})
    def test_new_source_invalidates_cached_analysis(self, analyze):
        run_verification(str(self.profile.uuid))
        ResumeUpload.objects.create(student=self.profile, extracted_data={'name': 'Different'})
        run_verification(str(self.profile.uuid))
        self.assertEqual(analyze.call_count, 2)

    @patch('verification.services.AIVerificationAgent.analyze', return_value={'missing_sources': [], 'candidates': []})
    def test_degraded_analysis_is_not_reused(self, analyze):
        run_verification(str(self.profile.uuid))
        VerificationCheck.objects.filter(student=self.profile).update(engine='rule_fallback')
        run_verification(str(self.profile.uuid))
        self.assertEqual(analyze.call_count, 2)
