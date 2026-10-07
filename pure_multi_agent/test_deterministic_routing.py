"""Conservative fast paths skip inference only when they cover the full turn."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from langchain_core.messages import AIMessage, HumanMessage

from pure_multi_agent.reference_answers import direct_reply, reference_reply, standalone_cost_calculation
from pure_multi_agent.student_graph import _reason_impl, _tools
from pure_multi_agent.turn_policy import classify, standalone_start_question


class DirectReplyTests(SimpleTestCase):
    def runtime(self, text):
        ctx = {'canonical_student_id': 'student', 'current_message': text,
               'profile_action_checked': True}
        return ctx, SimpleNamespace(context={'ctx': ctx, 'prompt': ''})

    def test_standalone_github_help_never_calls_classifier(self):
        ctx, runtime = self.runtime('How do I connect my GitHub account to Kormic?')
        with patch('pure_multi_agent.turn_policy.classify') as classifier:
            result = _reason_impl({'messages': [HumanMessage(content=ctx['current_message'])]}, runtime)
        classifier.assert_not_called()
        self.assertIn('Connect GitHub', result['messages'][0].content)

    def test_missing_resume_is_checked_before_classifier(self):
        ctx, runtime = self.runtime('Could you review my uploaded résumé?')
        with patch('django_api.models.ResumeUpload.objects.filter') as uploads, \
             patch('pure_multi_agent.document_evidence.manifest', return_value=[]), \
             patch('pure_multi_agent.turn_policy.classify') as classifier:
            uploads.return_value.exists.return_value = False
            result = _reason_impl({'messages': [HumanMessage(content=ctx['current_message'])]}, runtime)
        classifier.assert_not_called()
        self.assertIn('don’t have an uploaded resume', result['messages'][0].content)

    def test_compound_requests_never_use_partial_canned_answer(self):
        for text in ('How do I connect GitHub, and which projects should I improve?',
                     'Review my résumé and compare my projects with Stanford University.',
                     'Where should I start, and which universities fit me?'):
            with self.subTest(text=text):
                self.assertIsNone(direct_reply({'canonical_student_id': 'student', 'current_message': text}))
                self.assertFalse(standalone_start_question(text))
        self.assertIsNone(reference_reply(
            {'current_message': 'How do I connect GitHub, and which projects should I improve?'},
            {'route': 'general', 'reference_topic': 'github_connection'}))
        self.assertFalse(standalone_cost_calculation('Calculate my costs and suggest scholarships.'))
        self.assertTrue(standalone_cost_calculation('Calculate my total cost.'))
        self.assertTrue(standalone_start_question('What information do you need from me to suggest suitable universities?'))

    def test_compound_request_reaches_model_classification(self):
        ctx = {'canonical_student_id': 'student',
               'current_message': 'How do I connect GitHub, and which projects should I improve?'}
        with patch('pure_multi_agent.model_router.invoke',
                   return_value=AIMessage(content='{"route":"profile"}')) as model:
            intent = classify(ctx, [HumanMessage(content=ctx['current_message'])])
        model.assert_called_once()
        self.assertEqual(intent['route'], 'profile')

    def test_validated_cost_without_comparable_budget_has_complete_direct_answer(self):
        result = {'currency': 'INR', 'months': 12, 'tuition_total': '100000',
                  'living_monthly': '10000', 'living_total': '120000',
                  'other_total': '5000', 'total': '225000', 'remaining': None}
        calculator = SimpleNamespace(name='calculate_study_budget', invoke=lambda args: result)
        call = AIMessage(content='', tool_calls=[{'id': 'cost',
            'name': 'calculate_study_budget', 'args': {}}])
        ctx = {'current_message': 'Calculate these costs', 'profile_action_checked': True,
               'turn_intent': {'route': 'general', 'institutions': [], 'followup': False}}
        runtime = SimpleNamespace(context={'ctx': ctx, 'prompt': ''})
        with patch('pure_multi_agent.student_graph.build_all_tools', return_value=[calculator]), \
             patch('pure_multi_agent.job_recovery.boundary'), \
             patch('pure_multi_agent.activity.tool_activity'), \
             patch('pure_multi_agent.model_router.invoke') as model:
            _tools({'messages': [call]}, runtime)
            answer = _reason_impl({'messages': [HumanMessage(content=ctx['current_message']), call]}, runtime)
        model.assert_not_called()
        self.assertIn('INR 225,000.00', answer['messages'][0].content)
        self.assertIn('affordability is unknown', answer['messages'][0].content)

    def test_compound_cost_request_keeps_agent_path(self):
        result = {'currency': 'INR', 'months': 12, 'tuition_total': '100000',
                  'living_monthly': '10000', 'living_total': '120000',
                  'other_total': '0', 'total': '220000', 'remaining': None}
        calculator = SimpleNamespace(name='calculate_study_budget', invoke=lambda args: result)
        call = AIMessage(content='', tool_calls=[{'id': 'cost',
            'name': 'calculate_study_budget', 'args': {}}])
        ctx = {'current_message': 'Calculate my costs and suggest scholarships.'}
        with patch('pure_multi_agent.student_graph.build_all_tools', return_value=[calculator]), \
             patch('pure_multi_agent.job_recovery.boundary'), \
             patch('pure_multi_agent.activity.tool_activity'):
            _tools({'messages': [call]}, SimpleNamespace(context={'ctx': ctx, 'prompt': ''}))
        self.assertNotIn('completed_evidence_answer', ctx)


class NumberedSelectionTests(TestCase):
    def test_owned_preceding_choice_bypasses_classifier(self):
        from django_api.models import StudentProfile
        from university_research.models import UniversitySearch

        student = StudentProfile.objects.create(name='Fast selection')
        UniversitySearch.objects.create(student=student, query='Example University',
            candidates={'universities': [
                {'name': 'Example University', 'website': 'https://one.example.edu/'},
                {'name': 'Another University', 'website': 'https://two.example.edu/'}]})
        ctx = {'canonical_student_id': str(student.uuid), 'current_message': 'option 2',
               'turn_id': 'selection-turn', 'profile_action_checked': True}
        messages = [HumanMessage(content='Find Example University'),
            AIMessage(content='Which university do you mean?\n1. https://one.example.edu/\n2. https://two.example.edu/'),
            HumanMessage(content='option 2')]
        with patch('pure_multi_agent.turn_policy.classify') as classifier:
            result = _reason_impl({'messages': messages}, SimpleNamespace(context={'ctx': ctx, 'prompt': ''}))
        classifier.assert_not_called()
        self.assertEqual(result['messages'][0].tool_calls[0]['name'], 'select_university_candidate')
        self.assertEqual(result['messages'][0].tool_calls[0]['args']['candidate_index'], 2)
        self.assertEqual(ctx['turn_intent']['route'], 'university')
