from datetime import timedelta
from django.test import TestCase, override_settings
from django.utils import timezone
from pure_multi_agent.capacity import lease, check_rate, AgentBusy
from agent_queries.models import AgentCapacitySlot, AgentRateWindow


@override_settings(DEBUG=False, AGENT_DISTRIBUTED_LIMITS=True, AGENT_CAPACITY_BACKEND='database')
class DatabaseCapacityTests(TestCase):
    @override_settings(AGENT_QUEUE_ENABLED=False, UNIVERSITY_VECTOR_SEARCH=False)
    def test_public_officer_endpoint_can_read_requirements(self):
        from unittest.mock import patch
        from django.contrib.auth.models import User
        from rest_framework.test import APIClient
        from accounts.models import Account, TOTPDevice
        from universities.models import University
        from langchain_core.messages import AIMessage
        user = User.objects.create_user(username='public-demo')
        university = University.objects.create(name='Synthetic University')
        Account.objects.create(user=user, role='university', university=university)
        TOTPDevice.objects.create(user=user, secret_encrypted='test-only', confirmed_at=timezone.now())
        client = APIClient()
        client.force_authenticate(user)
        replies = [AIMessage(content='', tool_calls=[{'name':'read_university_record',
            'args':{'section':'requirements'}, 'id':'requirements'}]),
            AIMessage(content='No admission requirements have been saved yet.')]
        with patch('pure_multi_agent.model_router.invoke', side_effect=replies):
            response = client.post(f'/api/university/{university.uuid}/chat/',
                {'message':'What are the admission requirements?'}, format='json', secure=True)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('requirements', response.data['reply'])

    def test_public_mode_limits_work_without_redis(self):
        with lease('university:example'):
            with self.assertRaises(AgentBusy):
                with lease('university:example'):
                    pass
            with lease('university:other'):
                pass
        with lease('university:example'):
            pass

    def test_rate_budget_and_expiry(self):
        check_rate('test', 1)
        with self.assertRaises(AgentBusy):
            check_rate('test', 1)
        AgentRateWindow.objects.filter(key='test').update(started_at=timezone.now()-timedelta(seconds=61))
        check_rate('test', 1)

    def test_expired_holder_does_not_release_new_holder(self):
        with lease('test'):
            AgentCapacitySlot.objects.filter(key='test').update(expires_at=timezone.now()-timedelta(seconds=1))
            second = lease('test')
            second.__enter__()
        self.assertIsNotNone(AgentCapacitySlot.objects.get(key='test').token)
        second.__exit__(None, None, None)
        self.assertIsNone(AgentCapacitySlot.objects.get(key='test').token)
