from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from django.db import OperationalError


class ReviewRecoveryTests(SimpleTestCase):
    def test_refresh_keeps_previous_completed_analysis_available(self):
        from pure_multi_agent.tools.github_tools import github_evidence
        saved = {'username':'example', 'summary':'Previous completed findings', 'generated_at':'2026-09-28'}
        row = SimpleNamespace(student=SimpleNamespace(github_assessment=saved), synced_at=None)
        row.runs = SimpleNamespace(first=lambda:SimpleNamespace(status='running',progress='Repository 2/8',pk='job'))
        connection=SimpleNamespace(github_username='example',github_user_id=123)
        with patch('accounts.github_oauth.get_connection_for_student_id',return_value=connection), patch('django_api.models.GitHubProfileSnapshot.objects') as manager:
            manager.filter.return_value.first.return_value=row
            with patch('pure_multi_agent.telemetry.emit'):
                result=github_evidence('student')
        self.assertEqual(result['saved_analysis'],saved)
        self.assertEqual(result['status'],'processing')
        self.assertTrue(result['saved_analysis_available'])

    def test_context_save_retries_lock_without_replaying_turn(self):
        from pure_multi_agent.runtime import _persist_context
        with patch('pure_multi_agent.runtime._persist_context_once',side_effect=[OperationalError('database is locked'),None]) as save, patch('time.sleep'):
            _persist_context('student',{})
        self.assertEqual(save.call_count,2)

    def test_context_save_does_not_retry_other_database_errors(self):
        from pure_multi_agent.runtime import _persist_context
        with patch('pure_multi_agent.runtime._persist_context_once',side_effect=OperationalError('connection lost')) as save:
            with self.assertRaises(OperationalError):
                _persist_context('student',{})
        self.assertEqual(save.call_count,1)
