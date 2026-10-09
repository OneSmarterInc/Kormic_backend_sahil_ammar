"""The picker is a server-enforced boundary, including non-UI callers."""
from unittest.mock import patch
from django.test import TransactionTestCase
from rest_framework.exceptions import ValidationError
from django_api.models import GitHubRepository, GitHubRepositoryReport, GitHubSourceEvidence, GitHubSyncRun
from .sync import queue_inventory, queue_sync
from .tests import ExtractionTests, FixtureGitHub, finding, drain


class ManyRepositories(FixtureGitHub):
    def get(self, path, params=None, optional=False):
        if path == '/user/repos':
            base = super().get(path, params, optional)[0]
            return [{**base, 'id': n, 'name': f'repo-{n}', 'full_name': f'ada/repo-{n}'} for n in range(1, 14)]
        return super().get(path, params, optional)


class SelectionTests(TransactionTestCase):
    def setUp(self):
        ExtractionTests.setUp(self)

    def discover(self):
        run = queue_inventory(str(self.student.uuid))
        with patch('github_profiles.sync.get_valid_access_token', return_value='test'), \
             patch('github_profiles.sync.GitHub', ManyRepositories), \
             patch('github_profiles.inference.Inference.chat') as model:
            drain(run)
        self.assertEqual(run.status, 'completed', run.error)
        model.assert_not_called()
        return run

    def test_connection_discovery_lists_all_names_without_reading_or_analysing_source(self):
        run = self.discover()
        self.assertEqual(run.profile.repositories.count(), 13)
        self.assertFalse(GitHubSourceEvidence.objects.exists())
        self.assertFalse(GitHubRepositoryReport.objects.exists())
        self.assertIsNone(run.profile.synced_at)
        self.assertFalse(run.profile.repositories.filter(details_complete=True).exists())
        response = self.client.get('/api/profile/github/repos/?search=REPO-13')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['name'], 'ada/repo-13')
        self.assertEqual(response.data['max_selection'], 5)
        self.assertEqual(self.client.get('/api/profile/github/repos/?page=2').data['count'], 13)
        self.assertIsNone(self.client.get('/api/profile/github/overview/').data['sync'])

    def test_api_rejects_empty_six_duplicate_unknown_or_invalid_ids_before_queueing(self):
        self.discover()
        ids = list(GitHubRepository.objects.values_list('pk', flat=True))
        for body in ({}, {'repository_ids': []}, {'repository_ids': ids[:6]},
                     {'repository_ids': [ids[0], ids[0]]}, {'repository_ids': [999999]},
                     {'repository_ids': [True]}, {'repository_ids': ['1']}, {'repository_ids': '1'}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/api/profile/github/', body, format='json').status_code, 400)
        self.assertEqual(GitHubSyncRun.objects.count(), 1)

    def test_only_five_selected_repositories_reach_details_claude_and_student_profile(self):
        self.discover()
        chosen = list(GitHubRepository.objects.order_by('-id').values_list('pk', flat=True)[:5])
        response = self.client.post('/api/profile/github/', {'repository_ids': chosen}, format='json')
        self.assertEqual(response.status_code, 202)
        run = GitHubSyncRun.objects.get(pk=response.data['job_id'])
        with patch('github_profiles.sync.get_valid_access_token', return_value='test'), \
             patch('github_profiles.sync.GitHub', ManyRepositories), \
             patch('github_profiles.inference.Inference.chat', side_effect=finding):
            drain(run)
        self.assertEqual(run.status, 'completed', run.error)
        self.assertEqual(set(run.work['repositories']), set(chosen))
        self.assertEqual(set(GitHubRepositoryReport.objects.values_list('repository_id', flat=True)), set(chosen))
        self.assertFalse(GitHubSourceEvidence.objects.exclude(repository_id__in=chosen).exists())
        self.assertFalse(GitHubRepository.objects.exclude(pk__in=chosen).filter(details_complete=True).exists())
        self.student.refresh_from_db()
        self.assertEqual(len(self.student.github_assessment['projects']), 5)
        self.assertEqual(run.profile.statistics['repositories'], 5)
        self.assertEqual(self.client.get('/api/profile/github/repos/').data['count'], 13)
        self.assertEqual(self.client.get('/api/profile/github/repos/?scope=selected').data['count'], 5)

    def test_chat_tool_cannot_start_all_repositories_without_a_selection(self):
        self.discover()
        with self.assertRaises(ValidationError):
            queue_sync(str(self.student.uuid))

    def test_worker_refuses_legacy_or_tampered_unselected_jobs(self):
        run = self.discover()
        for work in ({}, {'selected_repository_ids': [1], 'repositories': [1, 2]}):
            job = GitHubSyncRun.objects.create(profile=run.profile, stage='agent', work=work)
            with patch('github_profiles.inference.Inference.chat') as model:
                drain(job)
            self.assertEqual(job.status, 'failed')
            model.assert_not_called()

    def test_inventory_refresh_preserves_completed_analysis_and_is_not_a_paid_sync(self):
        self.discover()
        run = queue_sync(str(self.student.uuid), self.repository_ids)
        with patch('github_profiles.sync.get_valid_access_token', return_value='test'), \
             patch('github_profiles.sync.GitHub', ManyRepositories), \
             patch('github_profiles.inference.Inference.chat', side_effect=finding):
            drain(run)
        summary = run.profile.summary
        self.discover()
        run.profile.refresh_from_db()
        self.assertEqual(run.profile.summary, summary)
        self.assertEqual(self.client.get('/api/profile/github/overview/').data['sync']['job_id'], str(run.pk))
