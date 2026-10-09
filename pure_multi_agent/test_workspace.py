from unittest.mock import patch
from contextlib import nullcontext
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from accounts.models import Account
from universities.models import University
from django_api.models import VerifiedAnswer
from pure_multi_agent.tools.officer_tools import build_tools
from pure_multi_agent.officer_graph import run_turn


@override_settings(AGENT_DISTRIBUTED_LIMITS=False, UNIVERSITY_VECTOR_SEARCH=False)
class WorkspaceToolsTests(TestCase):
    def test_admission_edit_with_repair_and_later_confirmation(self):
        from pure_multi_agent import model_router
        from django_api.models import AgentChangeProposal
        requirement = {'criterion': 'Minimum CGPA', 'detail': 'Minimum CGPA 3.7 on a 4.0 scale.',
            'category': 'gpa', 'minimum': 3.7, 'maximum': 4.0, 'scale_maximum': 4.0,
            'applies_to': 'All applicants'}
        # A real router validation failure is corrected before any tool executes.
        steps = [('unknown_tool', {}), ('read_university_record', {'section': 'requirements'}),
                 ('enable_officer_tool', {'name': 'propose_admission_requirement'}),
                 ('propose_admission_requirement', {'operation': 'update', 'index': 0,
                                                    'requirement': requirement}),
                 (None, {}), ('university_change_status', {}),
                 ('resolve_university_change', {}), (None, {})]
        dispatches = []
        def respond(model, messages, *args):
            index = len(dispatches)
            dispatches.append(messages)
            name, arguments = steps[index]
            if name == 'resolve_university_change':
                proposal = AgentChangeProposal.objects.get(status='pending')
                arguments = {'proposal_id': str(proposal.pk), 'decision': 'approve',
                             'confirmation_message': 'yes'}
            return AIMessage(content='Please confirm.' if index == 4 else 'Saved.' if name is None else '',
                tool_calls=[{'name': name, 'args': arguments, 'id': str(index)}] if name else [],
                response_metadata={'prompt_eval_count': 3000})
        saver = InMemorySaver()
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen'), \
             patch.object(model_router, 'measured_invoke', side_effect=respond):
            first = run_turn(str(self.uni.uuid), self.user.pk,
                'Change the minimum CGPA to 3.7, maximum and scale 4.0, for all applicants.',
                checkpointer=saver, history=[{'role': 'user', 'content': 'Earlier question'},
                    {'role': 'assistant', 'content': 'Old discussion ' * 1000}])
            self.uni.refresh_from_db()
            self.assertEqual(self.uni.eligibility_criteria[0]['detail'], '3.5 to 4.0 scale')
            self.assertTrue(AgentChangeProposal.objects.filter(status='pending').exists())
            self.assertIn('confirm', first['reply'])
            second = run_turn(str(self.uni.uuid), self.user.pk, 'yes', checkpointer=saver)
        self.uni.refresh_from_db()
        self.assertEqual(self.uni.eligibility_criteria[0]['minimum'], 3.7)
        self.assertEqual(second['reply'], 'Saved.')
        self.assertEqual(len(dispatches), len(steps))

    def setUp(self):
        self.user = User.objects.create_user(username='workspace-officer')
        self.uni = University.objects.create(name='Synthetic University', eligibility_criteria=[
            {'criterion': 'Min GPA', 'detail': '3.5 to 4.0 scale'}])
        Account.objects.create(user=self.user, role='university', university=self.uni)
        self.ctx = {'university_id': str(self.uni.uuid), 'actor_id': self.user.pk,
                    'turn_id': 'test', 'current_message': 'What are our requirements?'}
        self.tools = {tool.name: tool for tool in build_tools(self.ctx)}

    def test_every_sidebar_tab_reads_database(self):
        for tab in ('dashboard', 'university_profile', 'knowledge_sources', 'knowledge_base',
                    'knowledge_groups', 'assistant_chat', 'student_profiles', 'queries',
                    'escalated_queries', 'verified_knowledge'):
            with self.subTest(tab=tab):
                self.assertIsInstance(self.tools['read_portal_tab'].invoke({'tab': tab}), dict)

    def test_verified_records_are_scoped_and_paginated(self):
        other = University.objects.create(name='Other University')
        VerifiedAnswer.objects.create(university_id=str(other.uuid), question='Private', answer='Secret')
        for index in range(11):
            VerifiedAnswer.objects.create(university_id=str(self.uni.uuid), question=f'Intake {index}', answer='September')
        result = self.tools['read_portal_tab'].invoke({'tab': 'verified_knowledge', 'query': 'Intake'})
        self.assertEqual(result['total'], 11)
        self.assertEqual(len(result['records']), 10)
        self.assertTrue(result['has_next'])
        self.assertEqual(len(self.tools['read_portal_tab'].invoke({'tab': 'verified_knowledge', 'page': 2})['records']), 1)

    def test_graph_supplies_saved_requirements_and_existing_action_tools_to_ai(self):
        calls = []
        def model(messages, tools, **kwargs):
            from pure_multi_agent.qwen_context import select_context
            select_context(messages, tools, profile='evidence', max_context=16384)
            calls.append(kwargs.get('require_tools'))
            self.assertNotIn('3.5 to 4.0 scale', messages[0].content)
            names = {tool.name for tool in tools}
            self.assertTrue({'read_portal_tab', 'enable_officer_tool',
                'resolve_university_change', 'ask_student_agent'}.issubset(names))
            if len(calls) == 1:
                self.assertTrue(kwargs['require_tools'])
                return AIMessage(content='', tool_calls=[{'id': 'requirements',
                    'name': 'read_university_record', 'args': {'section': 'requirements'}}])
            self.assertIn('3.5 to 4.0 scale', messages[-1].content)
            return AIMessage(content='Your saved Min GPA requirement is 3.5 to 4.0 scale.')
        with patch('pure_multi_agent.model_router.invoke', side_effect=model):
            result = run_turn(str(self.uni.uuid), self.user.pk, self.ctx['current_message'],
                              checkpointer=InMemorySaver(), history=[
                                  {'role': 'user', 'content': 'What are our requirements?'},
                                  {'role': 'assistant', 'content': 'No requirements are recorded.'}])
        self.assertIn('3.5', str(result))
        self.assertEqual(calls, [True, False])
