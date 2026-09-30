from unittest.mock import patch
from django.test import TestCase
from langchain_core.messages import AIMessage
from django_api.models import StudentProfile
from universities.models import University
from agent_queries.models import AgentConversation, AgentQuery
from pure_multi_agent.registered_adviser import consult
from pure_multi_agent.tools.university_tools import build_tools


class ConsultationTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name='Synthetic Student')
        self.uni = University.objects.create(name='Synthetic University')
        self.ctx = {'canonical_student_id': str(self.student.uuid), 'student_profile': {}}

    def test_agent_exchange_is_durable_and_followup_reads_answer(self):
        replies = [AIMessage(content='', tool_calls=[{'id':'read', 'name':'retrieve_official_information',
                    'args':{'query':'GPA'}}]), AIMessage(content='Minimum GPA is 3.5 on a 4.0 scale.')]
        with patch('pure_multi_agent.registered_adviser.invoke', side_effect=replies), \
             patch('pure_multi_agent.tools.university_tools._university_evidence', return_value={'requirements':'3.5/4.0'}):
            result = consult(self.ctx, self.uni, 'What GPA is required?')
        conv = AgentConversation.objects.get(pk=result['conversation_id'])
        self.assertEqual(list(conv.messages.values_list('kind', flat=True)), ['request','tool','reply'])
        self.assertTrue(all(m.created_at for m in conv.messages.all()))
        status = next(t for t in build_tools(self.ctx) if t.name == 'university_reply_status').invoke({})
        self.assertIn('3.5', str(status))
        other = StudentProfile.objects.create(name='Other student')
        status = next(t for t in build_tools({'canonical_student_id':str(other.uuid)}) if t.name == 'university_reply_status').invoke({})
        self.assertEqual(status['results'], [])

    def test_failure_is_visible_in_conversation_not_a_fake_answer(self):
        with patch('pure_multi_agent.registered_adviser.invoke', side_effect=ValueError('private error')), \
             self.assertRaises(ValueError):
            consult(self.ctx, self.uni, 'What GPA is required?')
        conv = AgentConversation.objects.get(student=self.student, university=self.uni)
        error = conv.messages.get(kind='error')
        self.assertEqual(error.metadata['error_type'], 'ValueError')
        self.assertNotIn('private error', error.content)
        self.assertFalse(conv.messages.filter(kind='reply').exists())
