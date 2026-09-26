"""Behavioral agent, durability, and shared-capacity tests. No live credentials."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch, Mock

from django.contrib.auth.models import User
from django.db import connections, OperationalError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from accounts.models import GitHubOAuthConnection
from django_api.models import (StudentProfile, GitHubProfileSnapshot, GitHubSyncRun,
    GitHubRepository, GitHubRepositoryReport, GitHubModelSlot, GitHubModelPool)
from .agent import RepositoryAgent
from .checkpoints import DjangoSaver
from .inference import Inference
from .runner import execute_slice
from .scheduling import claim, release, heartbeat, fenced, model_slot, CapacityBusy, LeaseLost
from .tests import FixtureGitHub, finding


def make_run(index=0):
    user = User.objects.create(username=f'agent-test-{index}')
    student = StudentProfile.objects.create(name=f'Student {index}')
    connection = GitHubOAuthConnection.objects.create(user=user, github_user_id=index+10,
        github_username=f'user-{index}', access_token_encrypted='not-a-live-token')
    profile = GitHubProfileSnapshot.objects.create(student=student, connection=connection,
        github_user_id=connection.github_user_id, identity={'login': 'ada'})
    return GitHubSyncRun.objects.create(profile=profile)


class SchedulingTests(TransactionTestCase):
    def test_multiple_users_complete_with_concurrent_workers(self):
        sessions = [make_run(i) for i in range(8)]
        class MultiUserGitHub(FixtureGitHub):
            def get(self, path, params=None, optional=False):
                value = super().get(path, params, optional)
                if path == '/user':
                    value['id'] = int(self.token)
                return value
        def process(_):
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    run = None
                    try:
                        if not GitHubSyncRun.objects.filter(status__in=['queued', 'running']).exists():
                            return
                        run = claim()
                        if run:
                            execute_slice(run)
                    except OperationalError:
                        pass
                    time.sleep(0.01)
            finally:
                connections.close_all()
        with patch('github_profiles.sync.get_valid_access_token', side_effect=lambda c: str(c.github_user_id)), patch('github_profiles.sync.GitHub', MultiUserGitHub), patch('github_profiles.agent.Inference.chat', side_effect=finding):
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(process, range(4)))
        self.assertEqual(GitHubSyncRun.objects.filter(status='completed').count(), len(sessions),
            list(GitHubSyncRun.objects.values('status', 'stage', 'progress', 'error', 'lease_token', 'work')))
        self.assertEqual(GitHubRepositoryReport.objects.count(), len(sessions)*2)
        for run in GitHubSyncRun.objects.select_related('profile'):
            self.assertEqual(run.result['student_id'], str(run.profile.student.uuid))

    def test_100_sessions_rotate_and_multiple_users_can_be_claimed(self):
        runs = [make_run(i) for i in range(100)]
        active = [claim() for _ in range(8)]
        self.assertEqual(len({r.pk for r in active}), 8)
        self.assertEqual(GitHubSyncRun.objects.filter(lease_token__isnull=False).count(), 8)
        first = active[0]
        release(first)
        next_run = claim()
        self.assertNotEqual(next_run.pk, first.pk)
        self.assertEqual(next_run.pk, runs[8].pk)
        self.assertEqual(GitHubSyncRun.objects.count(), 100)

    def test_simultaneous_workers_never_claim_the_same_session(self):
        for i in range(8):
            make_run(i)
        barrier = Barrier(4)
        def contender(_):
            barrier.wait()
            try:
                for _ in range(20):
                    try:
                        run = claim()
                        if run:
                            return run.pk
                    except OperationalError:
                        time.sleep(0.01)
                self.fail('Could not acquire any queue job')
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(contender, range(4)))
        self.assertEqual(len(set(ids)), 4)

    def test_expired_lease_can_resume_but_previous_worker_cannot_write(self):
        run = make_run()
        old = claim(run.pk)
        GitHubSyncRun.objects.filter(pk=run.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        new = claim(run.pk)
        self.assertNotEqual(old.lease_token, new.lease_token)
        with self.assertRaises(LeaseLost):
            release(old, status='completed')
        heartbeat(new)
        run.refresh_from_db()
        self.assertEqual(run.lease_token, new.lease_token)

    @override_settings(GITHUB_QWEN_CONCURRENCY=1)
    def test_model_capacity_is_shared_across_sessions_and_recovers_expiry(self):
        a, b = claim(make_run().pk), claim(make_run(1).pk)
        with model_slot('qwen', a, 100):
            with self.assertRaises(CapacityBusy):
                with model_slot('qwen', b, 100):
                    self.fail('Concurrent capacity exceeded')
        with model_slot('qwen', b, 100):
            pass
        GitHubModelSlot.objects.update(token=a.lease_token, expires_at=timezone.now()-timedelta(seconds=1))
        with model_slot('qwen', a, 100):
            pass
        self.assertFalse(GitHubModelSlot.objects.filter(token__isnull=False).exists())

    @override_settings(GITHUB_QWEN_RPM=1)
    def test_rate_budget_does_not_spend_another_model_call(self):
        run = claim(make_run().pk)
        with model_slot('qwen', run, 100):
            pass
        with self.assertRaises(CapacityBusy):
            with model_slot('qwen', run, 100):
                pass
        run.refresh_from_db()
        self.assertEqual(run.model_calls, 1)
        self.assertEqual(GitHubModelPool.objects.get(provider='qwen').requests, 1)

    @override_settings(GITHUB_RUN_MAX_MODEL_CALLS=1)
    def test_per_run_ai_budget_is_enforced(self):
        from .errors import ServiceError
        run = claim(make_run().pk)
        with model_slot('qwen', run, 100):
            pass
        with self.assertRaisesMessage(ServiceError, 'AI budget'):
            with model_slot('claude', run, 100):
                pass

    @override_settings(GITHUB_DAILY_SYNC_LIMIT=1)
    def test_daily_limit_keeps_active_job_deduplication(self):
        from accounts.models import Account
        from rest_framework.exceptions import Throttled
        from .sync import queue_sync
        run = make_run()
        Account.objects.create(user=run.profile.connection.user, role='student', student_profile=run.profile.student)
        student_id = str(run.profile.student.uuid)
        self.assertEqual(queue_sync(student_id).pk, run.pk)
        GitHubSyncRun.objects.filter(pk=run.pk).update(status='completed')
        with self.assertRaises(Throttled):
            queue_sync(student_id)

    def test_busy_qwen_does_not_trigger_claude_fallback(self):
        run = claim(make_run().pk)
        with patch('github_profiles.inference.model_slot', side_effect=CapacityBusy()), patch('agents.github_agent._get_anthropic_client') as claude:
            with self.assertRaises(CapacityBusy):
                Inference(run).chat([{'role': 'user', 'content': 'test'}], {'type': 'object'})
            claude.assert_not_called()


class AgentBehaviorTests(TransactionTestCase):
    def setUp(self):
        self.run = claim(make_run().pk)
        self.repo = GitHubRepository.objects.create(profile=self.run.profile, github_id=1,
            name='repo-1', full_name='ada/repo-1', owner_login='ada')
        self.gh = FixtureGitHub('test', progress=lambda: heartbeat(self.run))
        self.sha = 'a'*40

    def agent(self):
        return RepositoryAgent(self.run, self.repo, self.gh, self.sha)

    def test_graph_resume_does_not_repeat_completed_model_decision(self):
        with patch('github_profiles.agent.Inference.chat', side_effect=finding) as model:
            self.assertIsNone(self.agent().advance())  # model chooses list_files
            self.assertEqual(model.call_count, 1)
            release(self.run)
            self.run = claim(self.run.pk)  # a new worker instance resumes the graph
            self.assertIsNone(self.agent().advance())  # execute persisted list_files
            self.assertEqual(model.call_count, 1)
            for _ in range(10):
                report = self.agent().advance()
                if report:
                    break
        self.assertEqual(report.data['agent']['tool_calls'], ['list_files', 'read_files', 'submit_finding'])
        self.assertEqual(self.run.checkpoints.count(), 2)
        self.assertEqual(GitHubRepositoryReport.objects.count(), 1)

    def test_agent_changes_tool_choice_based_on_observation(self):
        def adaptive(messages, schema):
            data = json.loads(messages[-1]['content'])
            history = data['observations']
            if data['sources'] and not any(h['tool'] == 'inspect_contributions' for h in history):
                return {'content': json.dumps({'name': 'inspect_contributions', 'arguments': {}}), 'provider': 'qwen', 'model': 'test'}
            return finding(messages, schema)
        with patch('github_profiles.agent.Inference.chat', side_effect=adaptive):
            for _ in range(15):
                report = self.agent().advance()
                if report:
                    break
        self.assertIn('inspect_contributions', report.data['agent']['tool_calls'])
        self.assertEqual(report.data['contribution']['status'], 'sampled')

    def test_bad_evidence_is_rejected_then_agent_can_correct_it(self):
        rejected = False
        def incorrect_once(messages, schema):
            nonlocal rejected
            answer = finding(messages, schema)
            call = json.loads(answer['content'])
            if call['name'] == 'submit_finding' and not rejected:
                call['arguments']['finding']['skills'][0]['evidence_ids'] = [99999999]
                answer['content'] = json.dumps(call)
                rejected = True
            return answer
        with patch('github_profiles.agent.Inference.chat', side_effect=incorrect_once):
            for _ in range(15):
                report = self.agent().advance()
                if report:
                    break
        self.assertEqual(report.data['agent']['tool_calls'].count('submit_finding'), 2)
        self.assertNotIn(99999999, report.data['skills'][0]['evidence_ids'])

    def test_tool_cannot_read_another_repository_or_secret_path(self):
        agent = self.agent()
        agent.state = {'files': ['src/main.py']}
        for path in ('../other/main.py', '.env', 'ada/another-repo/src/main.py'):
            with self.assertRaises(ValueError):
                agent.tools['read_files'].invoke({'paths': [path]})
        other = make_run(1)
        with self.assertRaises(Exception):
            RepositoryAgent(other, self.repo, self.gh, self.sha)

    def test_checkpoint_cannot_read_another_thread(self):
        saver = DjangoSaver(self.run, self.agent().thread)
        with self.assertRaises(ValueError):
            saver.get_tuple({'configurable': {'thread_id': 'another-user'}})

    @override_settings(GITHUB_AGENT_MAX_STEPS=2)
    def test_agent_loop_has_a_hard_step_budget(self):
        from .errors import ServiceError
        with patch('github_profiles.agent.Inference.chat', return_value={'content': '{"name":"read_readme","arguments":{}}', 'provider': 'qwen', 'model': 'test'}):
            with self.assertRaisesMessage(ServiceError, 'step limit'):
                for _ in range(10):
                    self.agent().advance()

    def test_capacity_pause_keeps_checkpoint_and_does_not_consume_retry(self):
        self.run.stage = 'agent'
        self.run.work = {'repositories': [self.repo.pk], 'cursor': 0, 'sha': self.sha, 'reports': {}}
        with fenced(self.run):
            GitHubSyncRun.objects.filter(pk=self.run.pk).update(stage=self.run.stage, work=self.run.work)
        with patch('github_profiles.sync.get_valid_access_token', return_value='test'), patch('github_profiles.sync.GitHub', FixtureGitHub), patch('github_profiles.agent.Inference.chat', side_effect=CapacityBusy()):
            execute_slice(self.run)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.lease_token)
        self.assertEqual(self.run.failures, 0)
        self.assertEqual(self.run.status, 'running')
        self.assertTrue(self.run.checkpoints.exists())
        GitHubSyncRun.objects.filter(pk=self.run.pk).update(available_at=timezone.now())
        self.run = claim(self.run.pk)
        with patch('github_profiles.agent.Inference.chat', side_effect=finding):
            self.agent().advance()

    def test_large_overview_yields_and_reuses_completed_model_batches(self):
        report_ids = {}
        for n in range(12):
            repo = GitHubRepository.objects.create(profile=self.run.profile, github_id=n+100,
                name=f'large-{n}', full_name=f'ada/large-{n}', owner_login='ada', metadata={'description': 'x'*1500})
            report = GitHubRepositoryReport.objects.create(repository=repo, sha=self.sha, analysis_version=3,
                provider='qwen', model='test', data={'summary': 'Project description. '*70, 'skills': [], 'domains': []})
            report_ids[str(repo.pk)] = report.pk
        self.run.stage, self.run.work = 'finalize', {'reports': report_ids}
        GitHubSyncRun.objects.filter(pk=self.run.pk).update(stage='finalize', work=self.run.work)
        seen = []
        def outline(messages, schema):
            payload = messages[-1]['content']
            self.assertNotIn(payload, seen, 'A completed overview model batch was repeated')
            seen.append(payload)
            return finding(messages, schema)
        with patch('github_profiles.sync.Inference.chat', side_effect=outline):
            for _ in range(10):
                execute_slice(self.run)
                self.run.refresh_from_db()
                if self.run.status == 'completed':
                    break
                self.run = claim(self.run.pk)
        self.assertEqual(self.run.status, 'completed')
        self.assertGreater(len(seen), 1)
