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
        replies = [AIMessage(content='Minimum GPA is 3.5 on a 4.0 scale.')]
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


    def test_registered_agent_reads_manual_knowledge_and_full_profile(self):
        from django.contrib.auth.models import User
        from accounts.models import Account
        from django_api.models import UniversityKnowledgeEntry
        from pure_multi_agent.tools.university_tools import _university_evidence
        Account.objects.create(user=User.objects.create_user('registered-owner'), role='university', university=self.uni)
        self.uni.location, self.uni.best_fit_notes = 'Campus location', 'Research applicants'
        self.uni.save()
        entry = UniversityKnowledgeEntry.objects.create(university_id=str(self.uni.uuid), topic='Fees', content='Annual tuition 12000 USD', source_type='manual')
        from types import SimpleNamespace
        with patch('knowledge.university_kb.UniversityKnowledgeBase') as kb, patch('agents.commons.record_university_interest'):
            kb.return_value.search.return_value = [SimpleNamespace(source_type='manual', source_url=None, to_dict=lambda: {'topic':entry.topic, 'content':entry.content})]
            result = _university_evidence(self.ctx, str(self.uni.uuid), 'fees')
        self.assertEqual(result['facts'][0]['content'], 'Annual tuition 12000 USD')
        self.assertEqual(result['profile']['location'], 'Campus location')
        self.assertEqual(result['profile']['best_fit_notes'], 'Research applicants')
