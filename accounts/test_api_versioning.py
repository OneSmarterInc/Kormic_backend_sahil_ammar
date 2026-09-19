from django.test import TestCase, SimpleTestCase
from django.urls import resolve, reverse, Resolver404
from django_api.test_chat_jobs import fixture

class VersionRoutesTests(SimpleTestCase):
    def test_all_api_route_families_share_handlers(self):
        for suffix in ['health/', 'schema/', 'auth/login/', 'auth/me/', 'auth/privacy/export/', 'auth/privacy/delete/', 'auth/privacy/retention/', 'verification/status/', 'university-admin/knowledge-sources/', 'university-admin/staff/', 'notifications/register-token/', 'superuser/metrics/models/', 'profile/', 'chat/agent/', 'claim/start/']:
            with self.subTest(suffix=suffix):
                old=resolve('/api/'+suffix); new=resolve('/api/v1/'+suffix)
                self.assertEqual(old.func, new.func)
                self.assertEqual(new.namespace,'v1')
    def test_attachment_reverse_respects_requested_version(self):
        self.assertEqual(reverse('v1:chat-attachment-detail',args=[7]), '/api/v1/chat/agent/attachments/7/')
        self.assertEqual(reverse('chat-attachment-detail',args=[7]), '/api/chat/agent/attachments/7/')
    def test_unknown_version_is_not_silently_downgraded(self):
        with self.assertRaises(Resolver404): resolve('/api/v2/auth/me/')

class VersionContractTests(TestCase):
    def test_same_profile_and_error_contract_through_both_paths(self):
        _,profile,client=fixture()
        for prefix in ['/api','/api/v1']:
            response=client.get(prefix+'/auth/me/')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response['X-API-Version'],'v1' if prefix.endswith('v1') else 'legacy')
            missing=client.get(prefix+'/profile/not-a-student/')
            self.assertIn(missing.status_code,[403,404])
            self.assertIn('request_id',missing.json()['error'])
