from unittest.mock import patch
from django.test import TestCase
from langchain_core.messages import AIMessage, HumanMessage
from pure_multi_agent.activity import track_activity, tool_activity, read_activity
from pure_multi_agent.completion import review_completion


class ActivityTests(TestCase):
    def test_activity_is_scoped_and_finishes_without_login_session(self):
        @track_activity('student')
        def turn(student_id):
            tool_activity('ask_university')
            self.assertEqual(read_activity('student:' + student_id)['label'], 'Asking the university agent…')
            self.assertEqual(read_activity('student:another')['status'], 'idle')
        turn(student_id='example')
        self.assertEqual(read_activity('student:example')['status'], 'completed')

    def test_failed_activity_is_not_left_running(self):
        @track_activity('university')
        def turn(university_id):
            raise ValueError('private error')
        with self.assertRaises(ValueError):
            turn('example')
        result = read_activity('university:example')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('private', str(result))

    @patch('pure_multi_agent.model_router.invoke')
    def test_promise_review_requires_real_consultation(self, invoke):
        invoke.return_value = AIMessage(content='', tool_calls=[{'name': 'completion_decision',
            'args': {'complete': False, 'next_action': 'Call ask_university for admission requirements.'}, 'id': 'audit'}])
        result = review_completion([HumanMessage(content='What are their admission requirements?')],
            AIMessage(content="I'll ask their agent and let you know."))
        self.assertFalse(result['complete'])
        self.assertTrue(invoke.call_args.kwargs['require_tools'])

    @patch('pure_multi_agent.registered_adviser.invoke')
    @patch('pure_multi_agent.tools.university_tools._university_evidence')
    def test_university_consultation_runs_without_officer_logged_in(self, evidence, invoke):
        from django_api.models import StudentProfile
        from universities.models import University
        from pure_multi_agent.registered_adviser import consult
        from agent_queries.models import AgentConversationMessage, AgentQuery
        student = StudentProfile.objects.create(name='Example Student')
        university = University.objects.create(name='Example University')
        evidence.return_value = {'facts': [{'text': 'Intake is September.'}]}
        invoke.side_effect = [AIMessage(content='', tool_calls=[{'name':'retrieve_official_information',
            'args':{'query':'intake'}, 'id':'retrieve'}]), AIMessage(content='Intake is September.')]
        result = consult({'canonical_student_id':str(student.uuid),'student_profile':{}, 'turn_id':'test'}, university, 'When is intake?')
        self.assertEqual(result['answer'], 'Intake is September.')
        self.assertFalse(AgentQuery.objects.exists())
        self.assertTrue(AgentConversationMessage.objects.filter(kind='reply').exists())
