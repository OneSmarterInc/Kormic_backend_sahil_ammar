from unittest.mock import patch
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
