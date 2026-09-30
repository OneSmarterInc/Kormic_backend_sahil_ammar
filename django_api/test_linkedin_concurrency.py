from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from django.test import TestCase
from django_api.models import StudentProfile, LinkedInAnalysis
from django_api.services import analyze_linkedin
from github_profiles.scheduling import CapacityBusy


class LinkedInPersistenceTests(TestCase):
    def test_github_result_committed_during_extraction_survives_linkedin_save(self):
        student = StudentProfile.objects.create(name='Student')
        def extraction(paths):
            StudentProfile.objects.filter(pk=student.pk).update(
                github_assessment={'summary':'Finished GitHub'}, skills=['Python'], evidence={'github':{'result':'saved'}})
            return {'name':'Student'}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'upload.txt'
            path.write_text('Student')
            with patch('django_api.services.validate_upload'), \
                 patch('django_api.services.save_uploaded_file', return_value=path), \
                 patch('django_api.services.relative_upload_path', return_value='linkedin/upload.txt'), \
                 patch('agents.linkedin_agent.LinkedInAgent.extract', side_effect=extraction):
                analyze_linkedin(str(student.uuid), [SimpleNamespace(name='upload.txt')])
        student.refresh_from_db()
        self.assertEqual(student.github_assessment, {'summary':'Finished GitHub'})
        self.assertEqual(student.skills, ['Python'])
        self.assertIn('github', student.evidence)
        self.assertIn('linkedin', student.evidence)
        self.assertEqual(LinkedInAnalysis.objects.count(), 1)

    def test_capacity_retry_removes_copy_without_saving_partial_profile(self):
        student = StudentProfile.objects.create(name='Student')
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'upload.txt'
            path.write_text('Student')
            with patch('django_api.services.validate_upload'), \
                 patch('django_api.services.save_uploaded_file', return_value=path), \
                 patch('django_api.services.relative_upload_path', return_value='linkedin/upload.txt'), \
                 patch('agents.linkedin_agent.LinkedInAgent.extract', side_effect=CapacityBusy()):
                with self.assertRaises(CapacityBusy):
                    analyze_linkedin(str(student.uuid), [SimpleNamespace(name='upload.txt')])
            self.assertFalse(path.exists())
        self.assertEqual(LinkedInAnalysis.objects.count(), 0)
