import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django_api.models import ResumeUpload, StudentProfile
from django_api.resume_identity import validate_resume_name
from django_api.tests import make_student_client


class NameComparisonTests(SimpleTestCase):
    def test_accepts_shared_whole_words_case_accents_and_order(self):
        for registered, cv in [('Rahul Sharma', 'RAHUL Kumar'), ('José Silva', 'Silva Jose'), ('Rahul Kumar Shah', 'Shah R.'), ('Anne-Marie Doe', 'Anne Marie Smith')]:
            with self.subTest(cv=cv):
                validate_resume_name(registered, cv)

    def test_rejects_missing_unrelated_initial_only_or_substring_names(self):
        for cv in [None, '', 'Student', 'Amit Verma', 'Rahulson Verma', 'R. S.', 'Mr. Amit']:
            with self.subTest(cv=cv), self.assertRaises(ValueError):
                validate_resume_name('Mr. Rahul Sharma', cv)


class ResumeOwnershipTests(TestCase):
    def setUp(self):
        self.client, self.sid = make_student_client(email='cv-owner@example.com')
        StudentProfile.objects.filter(uuid=self.sid).update(name='Rahul Sharma', skills=['Python'])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = override_settings(MEDIA_ROOT=self.directory.name)
        override.enable()
        self.addCleanup(override.disable)

    def upload(self, name):
        with patch('agents.resume_parser.ResumeParserAgent') as parser:
            parser.return_value.parse.return_value = {'name': name, 'skills': ['Java'], 'email': 'other@example.com'}
            return self.client.post('/api/profile/resume/', {'file': SimpleUploadedFile('Rahul.pdf', b'cv', content_type='application/pdf')}, format='multipart')

    def test_mismatch_rejects_without_modifying_profile_or_retaining_cv(self):
        response = self.upload('Amit Verma')
        self.assertEqual(response.status_code, 400)
        self.assertIn('does not match', response.data['message'])
        profile = StudentProfile.objects.get(uuid=self.sid)
        self.assertEqual(profile.name, 'Rahul Sharma')
        self.assertEqual(profile.skills, ['Python'])
        self.assertFalse(ResumeUpload.objects.exists())
        self.assertFalse(list(Path(self.directory.name).rglob('*.pdf')))

    def test_matching_cv_keeps_registered_name_and_saves_extraction(self):
        response = self.upload('Rahul Kumar')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StudentProfile.objects.get(uuid=self.sid).name, 'Rahul Sharma')
        self.assertEqual(ResumeUpload.objects.get().extracted_data['name'], 'Rahul Kumar')

    def test_unreadable_name_requires_a_clearer_cv(self):
        self.assertEqual(self.upload(None).status_code, 400)
