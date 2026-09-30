from unittest.mock import patch
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage
from pure_multi_agent.completion import review_completion


class CompletionAuditTests(SimpleTestCase):
    @patch('pure_multi_agent.model_router.invoke')
    def test_pending_action_cannot_be_accepted_as_complete(self, invoke):
        invoke.return_value = AIMessage(content='{"complete":true,"next_action":"Call ask_university"}')
        result = review_completion([HumanMessage(content='Find university requirements')], AIMessage(content='I will ask.'))
        self.assertFalse(result['complete'])
        self.assertIn('json_schema', invoke.call_args.kwargs)
        self.assertNotIn('require_tools', invoke.call_args.kwargs)

    @patch('pure_multi_agent.model_router.invoke')
    def test_completed_answer(self, invoke):
        invoke.return_value = AIMessage(content='{"complete":true,"next_action":""}')
        self.assertTrue(review_completion([HumanMessage(content='Hi')], AIMessage(content='Hello'))['complete'])
