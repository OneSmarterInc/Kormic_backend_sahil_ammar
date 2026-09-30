from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient
from accounts.models import Account, TOTPDevice
from django_api.models import StudentProfile, AgentAuditLog
from universities.models import University
from agent_queries.models import AgentConversation, AgentConversationMessage
from agent_queries.services import message
from pure_multi_agent.telemetry import scope, operation, emit

class ConversationFlowTests(TestCase):
    def setUp(self):
        admin=User.objects.create_user('flow-admin')
        Account.objects.create(user=admin,role='superuser')
        TOTPDevice.objects.create(user=admin,secret_encrypted='fixture',confirmed_at=timezone.now())
        self.client=APIClient();self.client.force_authenticate(admin)
        self.student=StudentProfile.objects.create(name='Alex',agent_name='Cove')
        self.conv=AgentConversation.objects.create(student=self.student,university=University.objects.create(name='Example',agent_name='Nova'))
        self.url=f'/api/agent-queries/conversations/{self.conv.pk}/'

    def test_latest_history_and_incremental_cursor(self):
        rows=[AgentConversationMessage.objects.create(conversation=self.conv,actor='student_agent',actor_name='Cove',kind='request',content=str(i)) for i in range(35)]
        latest=self.client.get(self.url,{'latest':1})
        self.assertEqual(latest.status_code,200)
        self.assertEqual([row['id'] for row in latest.data['results']],[r.pk for r in rows[5:]])
        self.assertTrue(latest.data['has_older'])
        older=self.client.get(self.url,{'before_id':rows[5].pk})
        self.assertEqual([row['id'] for row in older.data['results']],[r.pk for r in rows[:5]])
        after=self.client.get(self.url,{'after_id':rows[32].pk})
        self.assertEqual([row['id'] for row in after.data['results']],[r.pk for r in rows[33:]])

    def test_flow_joins_exact_exchange_and_preserves_tool_inputs_outputs(self):
        with scope('Cove (Student Agent)',str(self.student.uuid),'turn'):
            with operation('University Agent','consult'):
                request=message(self.conv,'student_agent','Search requirements',kind='request')
                emit('TOOL_CALL_START','search',inputs={'call_id':'call','arguments':{'query':'requirements'}})
                message(self.conv,'university_agent','Used search',kind='tool',metadata={'tool':'search','inputs':{'query':'requirements'},'outputs':{'answer':'Verified'}})
                emit('TOOL_RESULT','search',inputs={'call_id':'call'},outputs={'result':{'answer':'Verified'}})
                message(self.conv,'university_agent','Verified requirements',kind='reply')
            with operation('University Agent','consult'):
                other=message(self.conv,'student_agent','Unrelated question',kind='request')
        response=self.client.get(self.url,{'message_id':request.pk})
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.data['steps']),3)
        self.assertNotIn(other.pk,[row['id'] for row in response.data['steps']])
        tools=[row for row in response.data['logs'] if row['action_type']=='TOOL_RESULT']
        self.assertEqual(tools[0]['outputs']['result']['answer'],'Verified')
        self.assertTrue(response.data['trace_available'])
        self.assertEqual(response.data['steps'][1]['metadata']['inputs'],{'query':'requirements'})

    def test_legacy_flow_and_cross_conversation_boundary(self):
        request=message(self.conv,'student_agent','Old question',kind='request')
        tool=message(self.conv,'university_agent','Used search',kind='tool',metadata={'tool':'search'})
        message(self.conv,'university_agent','Old answer',kind='reply')
        message(self.conv,'student_agent','Next question',kind='request')
        result=self.client.get(self.url,{'message_id':tool.pk})
        self.assertEqual(len(result.data['steps']),3)
        self.assertFalse(result.data['trace_available'])
        self.assertEqual(result.data['logs'],[])
        self.assertEqual(self.client.get(self.url,{'message_id':999999}).status_code,404)
        self.assertEqual(self.client.get(self.url,{'before_id':'bad'}).status_code,400)
        other=AgentConversation.objects.create(student=self.student,university=University.objects.create(name='Other'))
        row=message(other,'student_agent','Private',kind='request')
        self.assertEqual(self.client.get(self.url,{'message_id':row.pk}).status_code,404)
