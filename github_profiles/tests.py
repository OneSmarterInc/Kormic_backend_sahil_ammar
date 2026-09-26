import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from django.contrib.auth.models import User
from django.test import TransactionTestCase, SimpleTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice, GitHubOAuthConnection
from django_api.models import (StudentProfile, GitHubProfileSnapshot, GitHubSyncRun,
    GitHubRepository, GitHubRepositoryReport, GitHubSourceEvidence, GitHubAnalysis)
from .errors import ServiceError
from .github import GitHub
from .inference import Inference
from .source_rules import eligible
from .redaction import redact
from .sync import queue_sync, execute_run, expire_interrupted_runs


SCHEMA = {'type': 'object', 'properties': {'summary': {'type': 'string'}}, 'required': ['summary']}
MESSAGES = [{'role': 'system', 'content': 'Analyze data.'}, {'role': 'user', 'content': '{}'}]


class InferenceTests(SimpleTestCase):
    @patch('agents.github_agent._get_anthropic_client')
    @patch('github_profiles.inference.httpx.post')
    def test_qwen_first_no_claude_when_valid(self, post, claude):
        post.return_value = Mock(json=lambda: {'message': {'content': '{"summary":"Source evidence"}'}})
        answer = Inference().chat(MESSAGES, SCHEMA)
        self.assertEqual(answer['provider'], 'qwen')
        claude.assert_not_called()
        self.assertEqual(post.call_args.kwargs['json']['format'], SCHEMA)
        self.assertFalse(post.call_args.kwargs['trust_env'])

    @patch('agents.github_agent._get_anthropic_client')
    @patch('github_profiles.inference.httpx.post')
    def test_offline_qwen_falls_back_and_is_not_retried_for_each_repo(self, post, claude):
        post.side_effect = httpx.ConnectError('Offline')
        claude.return_value.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"summary":"Evidence"}')])
        model = Inference()
        self.assertEqual(model.chat(MESSAGES, SCHEMA)['provider'], 'claude')
        model.chat(MESSAGES, SCHEMA)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(claude.call_count, 2)

    @patch('agents.github_agent._get_anthropic_client')
    @patch('github_profiles.inference.httpx.post')
    def test_invalid_qwen_schema_falls_back(self, post, claude):
        post.return_value = Mock(json=lambda: {'message': {'content': '{"summary":42}'}})
        claude.return_value.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text='{"summary":"Validated"}')])
        self.assertEqual(Inference().chat(MESSAGES, SCHEMA)['provider'], 'claude')

    @patch('agents.github_agent._get_anthropic_client')
    @patch('github_profiles.inference.httpx.post', side_effect=httpx.ConnectError('Offline'))
    def test_claude_uses_schema_constrained_tool_response(self, post, claude):
        claude.return_value.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(
            type='tool_use', name='structured_response', input={'summary': 'Validated tool output'})])
        answer = Inference().chat(MESSAGES, SCHEMA)
        self.assertEqual(json.loads(answer['content'])['summary'], 'Validated tool output')
        self.assertEqual(claude.return_value.messages.create.call_args.kwargs['tool_choice']['name'], 'structured_response')

    @patch('agents.github_agent._get_anthropic_client', side_effect=RuntimeError('credential detail'))
    @patch('github_profiles.inference.httpx.post', side_effect=httpx.ConnectError('Offline'))
    def test_both_unavailable_returns_safe_error_once(self, post, claude):
        model = Inference()
        for _ in range(2):
            with self.assertRaisesMessage(ServiceError, 'Collected GitHub facts remain saved'):
                model.chat(MESSAGES, SCHEMA)
        self.assertEqual(claude.call_count, 1)

    def test_all_repository_pages_are_fetched(self):
        gh = GitHub('not-a-real-token')
        gh.get = Mock(side_effect=[list(range(100)), list(range(100, 200)), [200]])
        self.assertEqual(sum(len(p) for p in gh.pages('/user/repos')), 201)
        self.assertEqual(gh.get.call_args.args[1]['page'], 3)

    def test_source_exclusions_and_redaction(self):
        for path in ('.env', '.env.local', 'node_modules/main.js', '.venv/lib.py', 'private.key', 'dist/app.js'):
            self.assertFalse(eligible(path))
        self.assertTrue(eligible('src/main.py'))
        self.assertNotIn('sk-' + 'a'*25, redact('api_key = sk-' + 'a'*25))
        self.assertNotIn('secret-value', redact('password: secret-value'))


class FixtureGitHub(GitHub):
    def get(self, path, params=None, optional=False):
        self.progress()
        if path == '/user':
            return {'id': 10, 'login': 'ada', 'name': 'Ada', 'public_repos': 2, 'followers': 4}
        if path == '/user/repos':
            return [{'id': n, 'name': f'repo-{n}', 'full_name': f'ada/repo-{n}', 'owner': {'login': 'ada'},
                     'description': 'Internal project description', 'default_branch': 'main', 'pushed_at': '2026-09-01',
                     'language': 'Python', 'private': False, 'topics': ['web']} for n in (1, 2)]
        if path == '/user/orgs' or path.endswith('/events'):
            return []
        if path.endswith('/languages'):
            return {'Python': 100}
        if path.endswith('/readme'):
            return None
        if '/git/trees/' in path:
            return {'tree': [{'type': 'blob', 'path': f'src/file{i:02}.py', 'size': 7000} for i in range(20)] + [{'type': 'blob', 'path': '.env', 'size': 8}]}
        if path.endswith('/commits'):
            return [{'html_url': 'https://github.com/ada/repo-1/commit/a'}]
        if '/commits/' in path:
            return {'sha': 'a'*40}
        raise AssertionError(path)

    def file(self, full_name, path, sha):
        return 'print("bounded source")\n' * 300


def finding(messages, schema):
    prompt = json.loads(messages[-1]['content'])
    if 'tools' in prompt:
        history = prompt['observations']
        if not history:
            call = {'name': 'list_files', 'arguments': {}}
        elif not prompt['sources']:
            paths = next(h['result']['paths'] for h in history if h['tool'] == 'list_files')
            call = {'name': 'read_files', 'arguments': {'paths': paths[:2]}}
        else:
            call = {'name': 'submit_finding', 'arguments': {'finding': {'summary': 'A Python application.', 'domains': ['Software'], 'skills': [{'name': 'Python', 'evidence_ids': [prompt['sources'][0]['id']]}], 'limitations': ['Sampled.']}}}
        return {'content': json.dumps(call), 'provider': 'qwen', 'model': 'qwen3:1.7b'}
    if 'sources' in prompt:
        data = {'summary': 'A Python application.', 'domains': ['Software'],
            'skills': [{'name': 'Python', 'evidence_ids': [prompt['sources'][0]['id']]},
                       {'name': 'Invented', 'evidence_ids': [999999]}], 'limitations': ['Sampled.']}
    else:
        data = {'highlight_project_ids': [p['id'] for p in prompt['projects']][:6], 'recommendations': ['documentation']}
    return {'content': json.dumps(data), 'provider': 'qwen', 'model': 'qwen3:1.7b'}


def drain(run):
    for _ in range(250):
        execute_run(run.pk)
        run.refresh_from_db()
        if run.status in ('completed', 'failed'):
            return
        GitHubSyncRun.objects.filter(pk=run.pk).update(available_at=timezone.now())
    raise AssertionError('Agent did not finish within the test budget')


class ExtractionTests(TransactionTestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name='Ada', skills=['Existing'], country='India')
        self.user = User.objects.create_user(username='ada')
        Account.objects.create(user=self.user, role='student', student_profile=self.student)
        TOTPDevice.objects.create(user=self.user, secret_encrypted='test-only', confirmed_at=timezone.now())
        self.connection = GitHubOAuthConnection.objects.create(user=self.user, github_user_id=10,
            github_username='ada', access_token_encrypted='test-only')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def run_extraction(self, fail=False):
        run = queue_sync(str(self.student.uuid))
        with patch('github_profiles.sync.get_valid_access_token', return_value='test-only'), patch('github_profiles.sync.GitHub', FixtureGitHub), patch('github_profiles.sync.Inference.chat', side_effect=ServiceError('AI unavailable') if fail else finding):
            drain(run)
        run.refresh_from_db()
        self.assertEqual(run.status, 'completed', run.error)
        return run

    def test_queue_deduplicates_and_post_is_accepted(self):
        first = self.client.post('/api/profile/github/', {}, format='json')
        second = self.client.post('/api/profile/github/', {}, format='json')
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.data['job_id'], second.data['job_id'])
        self.assertEqual(GitHubSyncRun.objects.count(), 1)

    def test_complete_extraction_persists_facts_reports_evidence_and_legacy_profile(self):
        run = self.run_extraction()
        self.assertEqual(GitHubRepository.objects.count(), 2)
        self.assertEqual(GitHubRepositoryReport.objects.count(), 2)
        report = GitHubRepositoryReport.objects.first()
        self.assertEqual([s['name'] for s in report.data['skills']], ['Python'])
        evidence = list(report.repository.source_evidence.all())
        self.assertLessEqual(len(evidence), 10)
        self.assertLessEqual(sum(len(e.excerpt) for e in evidence), 18000)
        self.assertTrue(all('/blob/' + 'a'*40 in e.url for e in evidence))
        self.student.refresh_from_db()
        self.assertEqual(self.student.country, 'India')
        self.assertEqual(self.student.skills, ['Existing', 'Python'])
        self.assertEqual(self.student.github_assessment['username'], 'ada')
        self.assertEqual(GitHubAnalysis.objects.count(), 1)
        self.assertTrue(run.profile.coverage['repository_list_complete'])
        self.assertEqual(run.profile.statistics['repositories'], 2)

    def test_unchanged_commits_reuse_source_reports(self):
        self.run_extraction()
        evidence_count = GitHubSourceEvidence.objects.count()
        self.run_extraction()
        self.assertEqual(GitHubRepositoryReport.objects.count(), 2)
        self.assertEqual(GitHubSourceEvidence.objects.count(), evidence_count)

    def test_model_failure_keeps_factual_overview_and_repositories(self):
        run = self.run_extraction(fail=True)
        self.assertTrue(run.profile.summary)
        self.assertTrue(run.profile.warnings)
        self.assertEqual(GitHubRepository.objects.count(), 2)
        self.assertEqual(GitHubRepositoryReport.objects.count(), 0)

    def test_incomplete_listing_keeps_prior_repositories_and_marks_coverage(self):
        run = self.run_extraction()
        GitHubRepository.objects.create(profile=run.profile, github_id=99, name='prior-repo', full_name='ada/prior-repo', owner_login='ada')

        class IncompleteGitHub(FixtureGitHub):
            def get(self, path, params=None, optional=False):
                if path == '/user/repos':
                    if (params or {}).get('page', 1) > 1:
                        raise ServiceError('GitHub rate-limited the next page.')
                    return super().get(path, params, optional)[:1] * 100
                return super().get(path, params, optional)

        next_run = queue_sync(str(self.student.uuid))
        with patch('github_profiles.sync.get_valid_access_token', return_value='test-only'), patch('github_profiles.sync.GitHub', IncompleteGitHub), patch('github_profiles.sync.Inference.chat', side_effect=finding):
            drain(next_run)
        next_run.refresh_from_db()
        self.assertEqual(next_run.status, 'completed', next_run.error)
        self.assertEqual(next_run.profile.repositories.filter(active=True).count(), 3)
        self.assertFalse(next_run.profile.coverage['repository_list_complete'])
        self.assertTrue(any(w['resource'] == 'inventory' for w in next_run.profile.warnings))

    def test_completed_listing_retires_removed_repos(self):
        run = self.run_extraction()
        prior = GitHubRepository.objects.create(profile=run.profile, github_id=99, name='prior-repo', full_name='ada/prior-repo', owner_login='ada')
        self.run_extraction()
        prior.refresh_from_db()
        self.assertFalse(prior.active)
        self.assertEqual(self.client.get('/api/profile/github/repos/').data['count'], 2)

    def test_expired_student_token_does_not_use_shared_credentials(self):
        run = queue_sync(str(self.student.uuid))
        with patch('github_profiles.sync.get_valid_access_token', side_effect=RuntimeError('Expired')), patch('github_profiles.sync.GitHub') as github:
            drain(run)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        github.assert_not_called()

    def test_names_only_ten_per_page_and_overview_hides_repo_descriptions(self):
        run = self.run_extraction()
        for n in range(3, 24):
            GitHubRepository.objects.create(profile=run.profile, github_id=n, name=f'repo-{n}', full_name=f'ada/repo-{n}', owner_login='ada', metadata={'description': 'Never serialize me'})
        first = self.client.get('/api/profile/github/repos/?page=1&page_size=999')
        second = self.client.get('/api/profile/github/repos/?page=2')
        last = self.client.get('/api/profile/github/repos/?page=3')
        self.assertEqual([len(r.data['results']) for r in (first, second, last)], [10, 10, 3])
        self.assertEqual(first.data['count'], 23)
        self.assertEqual(set(first.data['results'][0]), {'id', 'name'})
        self.assertFalse({r['id'] for r in first.data['results']} & {r['id'] for r in second.data['results']})
        overview = self.client.get('/api/profile/github/overview/').data
        self.assertNotIn('Project Experience', overview['profile']['overview'])
        self.assertNotIn('A Python application.', overview['profile']['overview'])
        self.assertNotIn('Never serialize me', json.dumps(overview, default=str))
        self.assertEqual(self.client.get('/api/profile/github/repos/?page=0').status_code, 400)
        self.assertEqual(self.client.get('/api/profile/github/repos/?page=4').status_code, 404)

    def test_other_student_cannot_read_jobs_or_repos(self):
        run = self.run_extraction()
        other = User.objects.create_user(username='other')
        Account.objects.create(user=other, role='student', student_profile=StudentProfile.objects.create())
        TOTPDevice.objects.create(user=other, secret_encrypted='test-only', confirmed_at=timezone.now())
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f'/api/profile/github/jobs/{run.pk}/').status_code, 404)
        response = self.client.get(f'/api/profile/github/repos/?student_id={self.student.uuid}')
        self.assertEqual(response.data['count'], 0)
        self.assertFalse(self.client.get('/api/profile/github/overview/').data['connected'])

    def test_disconnect_cascades_private_data_and_worker_cannot_claim_twice(self):
        run = self.run_extraction()
        with patch('github_profiles.runner.initialize') as collect:
            execute_run(run.pk)
            collect.assert_not_called()
        self.connection.delete()
        for model in (GitHubProfileSnapshot, GitHubSyncRun, GitHubRepository, GitHubSourceEvidence, GitHubRepositoryReport):
            self.assertEqual(model.objects.count(), 0)

    def test_unauthenticated_and_non_student_access_denied(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get('/api/profile/github/overview/').status_code, 401)
        self.user.account.role = 'university'
        self.user.account.save()
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get('/api/profile/github/repos/').status_code, 403)

    def test_changed_github_identity_hides_previous_snapshot(self):
        run = self.run_extraction()
        self.connection.github_user_id = 11
        self.connection.save()
        self.assertIsNone(self.client.get('/api/profile/github/overview/').data['profile'])
        self.assertEqual(self.client.get('/api/profile/github/repos/').data['count'], 0)
        self.assertEqual(self.client.get(f'/api/profile/github/jobs/{run.pk}/').status_code, 404)

    def test_interrupted_worker_is_recoverable(self):
        from datetime import timedelta
        run = queue_sync(str(self.student.uuid))
        GitHubSyncRun.objects.filter(pk=run.pk).update(status='running', updated_at=timezone.now()-timedelta(minutes=16))
        expire_interrupted_runs()
        run.refresh_from_db()
        self.assertEqual(run.status, 'queued')
        self.assertEqual(queue_sync(str(self.student.uuid)).pk, run.pk)
