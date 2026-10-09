"""Grouped analysis must retain source isolation, recovery, and complete coverage."""
import json
from unittest.mock import patch

from django.test import TransactionTestCase, SimpleTestCase
from django.utils import timezone

from django_api.models import GitHubRepository, GitHubRepositoryReport, GitHubSyncRun
from .grouped import step, selected_paths
from .runner import execute_slice
from .scheduling import claim, release, CapacityBusy
from .test_agents import make_run
from .tests import FixtureGitHub, finding, drain


class SelectionTests(SimpleTestCase):
    def test_dependencies_and_source_are_selected_instead_of_readme_only(self):
        selected = selected_paths(['README.md', 'package.json', 'src/app.ts',
                                   'src/helper.ts', 'api/main.py'])
        self.assertEqual(selected, ['package.json', 'src/app.ts', 'api/main.py'])


class GroupedTests(TransactionTestCase):
    def setUp(self):
        self.run = make_run()
        repos = [GitHubRepository.objects.create(profile=self.run.profile, github_id=n,
            name=f'repo-{n}', full_name=f'ada/repo-{n}', owner_login='ada',
            metadata={'language': 'Python', 'default_branch': 'main'}) for n in range(1, 4)]
        self.run.stage = 'agent'
        self.run.work = {'repositories': [r.pk for r in repos], 'cursor': 0, 'reports': {}, 'selected_repository_ids': [r.pk for r in repos]}
        self.run.save(update_fields=['stage', 'work'])

    def complete(self, responder=finding):
        with patch('github_profiles.sync.get_valid_access_token', return_value='test'), \
             patch('github_profiles.sync.GitHub', FixtureGitHub), \
             patch('github_profiles.inference.Inference.chat', side_effect=responder) as model:
            drain(self.run)
        self.assertEqual(self.run.status, 'completed', self.run.error)
        return model

    def test_three_repositories_share_one_analysis_and_keep_separate_evidence(self):
        model = self.complete()
        self.assertEqual(model.call_count, 1)  # overview uses no inference
        self.assertEqual(GitHubRepositoryReport.objects.count(), 3)
        for report in GitHubRepositoryReport.objects.all():
            allowed = set(report.repository.source_evidence.values_list('pk', flat=True))
            self.assertTrue(set(report.data['skills'][0]['evidence_ids']) <= allowed)
            self.assertEqual(report.data['contribution']['status'], 'sampled')
            self.assertEqual(report.data['coverage']['inspected_files'], 3)
            self.assertIn('inspect_contributions', report.data['agent']['tool_calls'])

    def test_five_selected_repositories_need_only_two_analysis_calls(self):
        for n in range(4, 6):
            repo = GitHubRepository.objects.create(profile=self.run.profile, github_id=n,
                name=f'repo-{n}', full_name=f'ada/repo-{n}', owner_login='ada',
                metadata={'language': 'Python', 'default_branch': 'main'})
            self.run.work['repositories'].append(repo.pk)
            self.run.work['selected_repository_ids'].append(repo.pk)
        self.run.save(update_fields=['work'])
        self.assertEqual(self.complete().call_count, 2)
        self.assertEqual(GitHubRepositoryReport.objects.count(), 5)

    def test_oversized_group_splits_without_cutting_evidence(self):
        from pure_multi_agent.qwen_context import ContextBudgetExceeded
        def one_at_a_time(payload):
            repos = json.loads(payload['messages'][0]['content'])['repositories']
            if len(repos) > 1:
                raise ContextBudgetExceeded('Test reduced input budget')
            self.assertEqual(len(repos[0]['sources']), 3)
            by_id = {s['id']: s for s in repos[0]['sources']}
            for source in repos[0]['sources']:
                excerpt = source.get('excerpt', by_id.get(source.get('excerpt_ref'), {}).get('excerpt'))
                self.assertEqual(len(excerpt), 3500)
        with patch('github_profiles.grouped.guard', side_effect=one_at_a_time):
            self.assertEqual(self.complete().call_count, 3)
        self.assertEqual(GitHubRepositoryReport.objects.count(), 3)

    def test_missing_and_duplicate_results_get_individual_recovery(self):
        def duplicate(messages, schema, **kwargs):
            answer = finding(messages, schema, **kwargs)
            if 'repositories' in json.loads(messages[-1]['content']):
                result = json.loads(answer['content'])
                result['results'][1] = result['results'][0]
                answer['content'] = json.dumps(result)
            return answer
        self.assertEqual(self.complete(duplicate).call_count, 3)
        self.assertEqual(GitHubRepositoryReport.objects.count(), 3)

    def test_foreign_citation_falls_back_only_for_affected_repository(self):
        def crossed(messages, schema, **kwargs):
            answer = finding(messages, schema, **kwargs)
            payload = json.loads(messages[-1]['content'])
            if 'repositories' in payload:
                result = json.loads(answer['content'])
                result['results'][0]['finding']['skills'][0]['evidence_ids'] = [
                    payload['repositories'][1]['sources'][0]['id']]
                answer['content'] = json.dumps(result)
            return answer
        model = self.complete(crossed)
        self.assertEqual(model.call_count, 2)  # group and one fallback
        for report in GitHubRepositoryReport.objects.all():
            allowed = set(report.repository.source_evidence.values_list('pk', flat=True))
            self.assertTrue(set(report.data['skills'][0]['evidence_ids']) <= allowed)

    def test_requested_extra_evidence_retains_individual_tool_investigation(self):
        def more(messages, schema, **kwargs):
            answer = finding(messages, schema, **kwargs)
            payload = json.loads(messages[-1]['content'])
            if 'repositories' in payload:
                result = json.loads(answer['content'])
                result['results'][0].update(needs_more_evidence=True, finding=None)
                answer['content'] = json.dumps(result)
            elif 'tools' in payload and len(payload['sources']) == 3:
                answer['content'] = json.dumps({'name': 'read_files', 'arguments': {
                    'paths': [p for p in payload['observations'][0]['result']['paths']
                              if p not in {s['path'] for s in payload['sources']}][:1]}})
            return answer
        self.complete(more)
        self.assertEqual(sorted(r.data['coverage']['inspected_files']
                               for r in GitHubRepositoryReport.objects.all()), [3, 3, 4])

    def test_paid_group_result_survives_worker_restart(self):
        run = claim(self.run.pk)
        gh = FixtureGitHub('test')
        with patch('github_profiles.inference.Inference.chat', side_effect=finding) as model:
            for _ in range(20):
                step(run, gh)
                if model.called:
                    break
                release(run, work=run.work)
                run = claim(run.pk)
        self.assertEqual(model.call_count, 1)
        # Simulate a crash after the answer was saved but before the queue slice ended.
        release(run)
        self.run.refresh_from_db()
        self.assertTrue(all(e['status'] == 'validate' for e in self.run.work['github_group']))
        resumed = self.complete()
        self.assertEqual(resumed.call_count, 0)  # paid answer already persisted; overview is free

    def test_unchanged_second_sync_uses_no_model_calls_including_overview(self):
        self.complete()
        self.run = GitHubSyncRun.objects.create(profile=self.run.profile, stage='agent',
            work={'repositories': self.run.work['repositories'], 'selected_repository_ids': self.run.work['selected_repository_ids'], 'cursor': 0, 'reports': {}})
        model = self.complete()
        self.assertEqual(model.call_count, 0)
        # Reuse must persist into a third sync too.
        self.run = GitHubSyncRun.objects.create(profile=self.run.profile, stage='agent',
            work={'repositories': self.run.work['repositories'], 'selected_repository_ids': self.run.work['selected_repository_ids'], 'cursor': 0, 'reports': {}})
        self.assertEqual(self.complete().call_count, 0)

    def test_one_inaccessible_repository_does_not_drop_the_other_two(self):
        class PartialGitHub(FixtureGitHub):
            def get(self, path, params=None, optional=False):
                from .errors import ServiceError
                if '/repo-2/git/trees/' in path:
                    raise ServiceError('Repository is unavailable.')
                return super().get(path, params, optional)
        with patch('github_profiles.sync.get_valid_access_token', return_value='test'), \
             patch('github_profiles.sync.GitHub', PartialGitHub), \
             patch('github_profiles.inference.Inference.chat', side_effect=finding):
            drain(self.run)
        self.assertEqual(self.run.status, 'completed')
        self.assertEqual(GitHubRepositoryReport.objects.count(), 2)
        self.assertTrue(self.run.profile.warnings)

    def test_changed_findings_refresh_overview_without_inference(self):
        self.complete()
        report = GitHubRepositoryReport.objects.first()
        report.data['summary'] = 'A different verified project summary.'
        report.save(update_fields=['data'])
        self.run = GitHubSyncRun.objects.create(profile=self.run.profile, stage='agent',
            work={'repositories': self.run.work['repositories'], 'selected_repository_ids': self.run.work['selected_repository_ids'], 'cursor': 0, 'reports': {}})
        self.assertEqual(self.complete().call_count, 0)
        self.run.profile.refresh_from_db()
        self.assertIn('A different verified project summary.', self.run.profile.summary)

    def test_capacity_pause_does_not_lose_prepared_sources(self):
        run = claim(self.run.pk)
        gh = FixtureGitHub('test')
        for _ in range(20):
            if run.work.get('github_group') and all(e['status'] == 'ready'
                                                  for e in run.work['github_group']):
                break
            step(run, gh)
            release(run, work=run.work)
            run = claim(run.pk)
        with patch('github_profiles.sync.get_valid_access_token', return_value='test'), \
             patch('github_profiles.sync.GitHub', FixtureGitHub), \
             patch('github_profiles.inference.Inference.chat', side_effect=CapacityBusy()):
            execute_slice(run)
        self.run.refresh_from_db()
        self.assertTrue(all(len(e['state']['sources']) == 3 for e in self.run.work['github_group']))
        self.assertEqual(self.run.failures, 0)
        GitHubSyncRun.objects.filter(pk=self.run.pk).update(available_at=timezone.now())
        self.assertEqual(self.complete().call_count, 1)
