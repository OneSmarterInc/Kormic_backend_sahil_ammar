from datetime import timedelta
from django.test import TestCase
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone
from accounts.github_oauth import create_oauth_state, consume_oauth_state
from accounts.models import GitHubOAuthState


class GitHubStateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='oauth-test')

    def test_state_survives_cache_loss_and_is_single_use(self):
        state = create_oauth_state(self.user.pk)
        cache.clear()
        self.assertNotEqual(GitHubOAuthState.objects.get().digest, state)
        self.assertEqual(consume_oauth_state(state), self.user.pk)
        self.assertIsNone(consume_oauth_state(state))

    def test_expired_and_unknown_state_rejected(self):
        state = create_oauth_state(self.user.pk)
        GitHubOAuthState.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertIsNone(consume_oauth_state(state))
        self.assertIsNone(consume_oauth_state('fabricated'))

    def test_connection_telemetry_uses_separate_id_and_no_oauth_secrets(self):
        from accounts.models import Account
        from django_api.models import StudentProfile, AgentAuditLog
        from accounts.github_oauth import connection_activity
        student=StudentProfile.objects.create(name='Connection owner')
        Account.objects.create(user=self.user,role='student',student_profile=student)
        state=create_oauth_state(self.user.pk)
        connection_activity(state,self.user,'RUN_START','Waiting for GitHub consent.')
        connection_activity(state,self.user,'RUN_COMPLETE','GitHub connection saved.')
        rows=list(AgentAuditLog.objects.all())
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0].run_id,rows[1].run_id)
        self.assertEqual(rows[0].student_id,str(student.uuid))
        serialized=str([(r.run_id,r.inputs,r.outputs) for r in rows])
        self.assertNotIn(state,serialized)
        self.assertNotIn(GitHubOAuthState.objects.get().digest,serialized)
