import os
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage
from pure_multi_agent.advice_policy import budget_context, apply_budget_clarifications, budget_clarification
from pure_multi_agent.model_router import advice_options
from pure_multi_agent.telemetry import safe_data

class BudgetRegressionTests(SimpleTestCase):
    def test_currency_period_and_unknown_living(self):
        original = {'budget': 60000, 'budget_text': ''}
        profile = apply_budget_clarifications(original, ['My budget is usd, annual budget'])
        budget = budget_context(profile)
        self.assertEqual((budget['amount'], budget['currency'], budget['period']), (60000, 'USD', 'annual'))
        self.assertIsNone(budget['includes_living'])
        self.assertNotIn('_conversation_budget', original)
        self.assertEqual(budget_clarification('What is an annual budget in USD?'), {})
        self.assertNotIn('amount', budget_clarification('My budget is USD annual for 2 years'))

    def test_latest_explicit_clarification_wins(self):
        profile = apply_budget_clarifications({'budget': 60000}, ['My budget is INR total', 'My budget is USD annual'])
        self.assertEqual(budget_context(profile)['period'], 'annual')
        self.assertEqual(budget_context(profile)['currency'], 'USD')

    def test_auto_small_model_is_local_only_for_advice(self):
        with patch.dict(os.environ, {'STUDENT_REASONING_PROVIDER': 'auto', 'STUDENT_OLLAMA_MODEL': 'qwen3:1.7b'}):
            self.assertEqual(advice_options(), {'local_only': True})

    def test_clarification_uses_one_local_answer_without_reviews_or_tools(self):
        from pure_multi_agent.student_graph import _reason
        reply = AIMessage(content='Understood: USD 60,000 per year. Does that include living expenses?')
        ctx = {'canonical_student_id': 'student', 'current_message': 'My budget is usd, annual budget',
               'budget_clarification_turn': True, 'clarification_checked': True,
               'student_profile': apply_budget_clarifications({'budget': 60000}, ['My budget is usd, annual budget'])}
        with patch('pure_multi_agent.change_proposals.conversation_state', return_value={'pending_changes': [], 'conversation_assumptions': {}}), patch('pure_multi_agent.job_recovery.boundary'), patch('pure_multi_agent.student_graph.build_all_tools', return_value=[]), \
             patch('pure_multi_agent.model_router.invoke', return_value=reply) as model, \
             patch('pure_multi_agent.response_review.review_answer') as review, \
             patch.dict(os.environ, {'STUDENT_REASONING_PROVIDER': 'auto'}):
            result = _reason({'messages': [HumanMessage(content=ctx['current_message'])]}, SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        model.assert_called_once()
        self.assertEqual(model.call_args.args[1], [])
        self.assertFalse(model.call_args.kwargs.get('force_claude', False))
        self.assertTrue(model.call_args.kwargs.get('local_only', False))
        self.assertIs(result['messages'][0], reply)
        review.assert_not_called()

    def test_usage_counts_preserved_secrets_redacted(self):
        result = safe_data({'input_tokens': 123, 'output_tokens': 42, 'input_token_details': {'cache_read': 100, 'access_token': 'secret'}, 'access_token': 'secret'})
        self.assertEqual(result['input_tokens'], 123)
        self.assertEqual(result['input_token_details']['cache_read'], 100)
        self.assertEqual(result['input_token_details']['access_token'], '[redacted]')
        self.assertEqual(result['access_token'], '[redacted]')
