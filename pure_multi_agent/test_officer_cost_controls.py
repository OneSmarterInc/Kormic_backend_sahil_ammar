import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage

from pure_multi_agent.officer_cost_controls import initial_read
from pure_multi_agent.officer_graph import _reason, _act, _validate_consent_call, RESUME_KEYS
from pure_multi_agent.officer_context import prepare
from pure_multi_agent.tools.officer_tools import build_tools


class OfficerSavingsTests(SimpleTestCase):
    def setUp(self):
        self.university = SimpleNamespace(uuid='university', name='Example', agent_name='Assistant',
            website_url='https://example.edu', tone_descriptors=[], communication_style_notes='', never_do_notes='')

    def test_clear_read_uses_one_answer_call_and_executes_scoped_tool_first(self):
        text = 'What are our admission requirements?'
        ctx = {'current_message': text, 'turn_id': 'turn', 'university_id': 'university'}
        runtime = SimpleNamespace(context=ctx)
        evidence = {'requirements': [{'minimum': 3.0, 'scale': 4.0}], 'source_url': 'https://example.edu/admissions'}
        tools = [SimpleNamespace(name='read_university_record', invoke=lambda args: evidence)]
        state = {'messages': [HumanMessage(content=text)]}
        with patch('pure_multi_agent.officer_graph.changes.officer_university', return_value=self.university), \
             patch('pure_multi_agent.officer_graph.changes.conversation_state', return_value={}), \
             patch('pure_multi_agent.job_recovery.boundary'), \
             patch('pure_multi_agent.activity.tool_activity'), \
             patch('pure_multi_agent.officer_graph.build_tools', return_value=tools), \
             patch('pure_multi_agent.officer_context.fit_history', side_effect=lambda prompt, messages, tools, **kw: messages), \
             patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(content='The saved minimum is 3.0 on a 4.0 scale.')) as model:
            first = _reason(state, runtime)
            model.assert_not_called()
            self.assertEqual(first['messages'][0].tool_calls[0]['name'], 'read_university_record')
            state['messages'] += first['messages']
            state['messages'] += _act(state, runtime)['messages']
            self.assertEqual(json.loads(state['messages'][-1].content), evidence)
            _reason(state, runtime)
        model.assert_called_once()
        self.assertFalse(model.call_args.kwargs['require_tools'])

    def test_clear_edit_loads_validated_proposal_schema_without_creating_edit(self):
        ctx = {'current_message': 'Update the CGPA admission requirement to 3.5 on a 4.0 scale.'}
        with patch('pure_multi_agent.officer_graph.changes.officer_university', return_value=self.university), \
             patch('pure_multi_agent.job_recovery.boundary'), \
             patch('pure_multi_agent.change_proposals.propose_university') as propose, \
             patch('pure_multi_agent.change_proposals.resolve') as resolve, \
             patch('pure_multi_agent.model_router.invoke') as model:
            result = _reason({'messages': [HumanMessage(content=ctx['current_message'])]}, SimpleNamespace(context=ctx))
            _, tools = prepare(ctx, result['messages'], build_tools(ctx))
        propose.assert_not_called(); resolve.assert_not_called(); model.assert_not_called()
        self.assertEqual(ctx['officer_proposal_tool'], 'propose_admission_requirement')
        self.assertIn('propose_admission_requirement', {tool.name for tool in tools})
        self.assertNotIn('propose_knowledge_change', {tool.name for tool in tools})
        self.assertIn('initial_read_requested', RESUME_KEYS)

    def test_consent_fast_path_only_reads_live_state_and_preserves_exact_quote_validation(self):
        for text in ('yes', 'no', 'approve', 'reject', 'cancel'):
            self.assertEqual(initial_read(text), {'name': 'university_change_status', 'args': {}})
        self.assertIsNone(initial_read('yes, but change the amount'))
        with self.assertRaises(ValueError):
            _validate_consent_call({'name': 'resolve_university_change', 'args': {'confirmation_message': 'yes'}},
                                   'yes, but change the amount')

    def test_ambiguous_mixed_or_student_requests_keep_normal_reasoning(self):
        for text in ('Review this', 'Update the same policy', 'Compare students by CGPA',
                     'Show student scholarship eligibility', 'Show fees and admission requirements',
                     'Update the scholarship and send it to students', 'What did we decide earlier?'):
            with self.subTest(text=text):
                self.assertIsNone(initial_read(text))

    def test_university_authorization_runs_before_deterministic_read(self):
        with patch('pure_multi_agent.job_recovery.boundary'), \
             patch('pure_multi_agent.officer_graph.changes.officer_university', side_effect=ValueError('Unauthorized')), \
             patch('pure_multi_agent.model_router.invoke') as model:
            with self.assertRaisesMessage(ValueError, 'Unauthorized'):
                _reason({'messages': []}, SimpleNamespace(context={'current_message': 'Show our admission requirements'}))
        model.assert_not_called()
