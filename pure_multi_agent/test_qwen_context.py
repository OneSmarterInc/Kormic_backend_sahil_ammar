from contextlib import nullcontext
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from pure_multi_agent import legacy_qwen, model_router
from pure_multi_agent.qwen_context import (ContextBudget, ContextBudgetExceeded,
    select_context, needs_expansion)
from pure_multi_agent.turn_policy import select_tools


@tool
def read_fact() -> dict:
    """Read a fact from saved evidence."""
    return {'fact': 'saved'}


class ContextSizingTests(SimpleTestCase):
    def test_profiles_reserve_input_and_output_and_expand_for_long_evidence(self):
        short = [{'role': 'user', 'content': 'Classify this message.'}]
        self.assertEqual(select_context(short, profile='routing').num_ctx, 4096)
        self.assertEqual(select_context(short, profile='routing').num_predict, 512)
        self.assertEqual(select_context(short, profile='general').num_ctx, 8192)
        self.assertEqual(select_context(short, profile='evidence').num_ctx, 16384)
        long = [{'role': 'user', 'content': 'Evidence ' + 'x' * 12000}]
        self.assertEqual(select_context(long, profile='routing').num_ctx, 8192)
        self.assertEqual(select_context(long, profile='general').num_ctx, 16384)

    def test_schema_and_output_reserve_can_expand_a_request(self):
        messages = [{'role': 'user', 'content': 'Use the schema.'}]
        schema = {'description': 'x' * 6500}
        self.assertEqual(select_context(messages, profile='routing',
            format_schema=schema).num_ctx, 8192)
        self.assertEqual(select_context(messages, profile='routing',
            output_tokens=3900).num_ctx, 8192)

    def test_oversized_input_is_rejected_without_mutating_sources(self):
        messages = [{'role': 'user', 'content': 'source:' + 'x' * 32000}]
        with self.assertRaises(ContextBudgetExceeded):
            select_context(messages, profile='general', max_context=16384)
        self.assertEqual(len(messages[0]['content']), 32007)

    def test_router_sends_routing_profile_to_qwen(self):
        response = AIMessage(content='{"route":"general"}')
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen') as qwen, \
             patch.object(model_router, 'measured_invoke', return_value=response):
            model_router.invoke([HumanMessage(content='Hello')], profile='routing', local_only=True)
        qwen.assert_called_with(4096, 512)

    def test_actual_prompt_pressure_retries_before_using_a_reply(self):
        first = AIMessage(content='Incomplete', response_metadata={'prompt_eval_count': 7500})
        second = AIMessage(content='Complete', response_metadata={'prompt_eval_count': 3000})
        budgets = [ContextBudget('general', 8192, 2400, 8500, 4500),
                   ContextBudget('general', 16384, 2400, 8500, 4500)]
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'select_context', side_effect=budgets) as sizing, \
             patch.object(model_router, 'qwen') as qwen, \
             patch.object(model_router, 'measured_invoke', side_effect=[first, second]):
            reply = model_router.invoke([HumanMessage(content='Question')], local_only=True)
        self.assertEqual(reply.content, 'Complete')
        self.assertEqual(sizing.call_count, 2)
        qwen.assert_any_call(8192, 2400)
        qwen.assert_any_call(16384, 2400)

    def test_output_limit_expands_without_returning_an_incomplete_answer(self):
        first = AIMessage(content='Partial', response_metadata={
            'prompt_eval_count': 300, 'done_reason': 'length'})
        second = AIMessage(content='Complete', response_metadata={
            'prompt_eval_count': 300, 'done_reason': 'stop'})
        budgets = [ContextBudget('routing', 4096, 512, 1000, 700),
                   ContextBudget('routing', 4096, 1024, 1000, 700)]
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'select_context', side_effect=budgets), \
             patch.object(model_router, 'qwen') as qwen, \
             patch.object(model_router, 'measured_invoke', side_effect=[first, second]):
            reply = model_router.invoke([HumanMessage(content='Classify')],
                profile='routing', local_only=True)
        self.assertEqual(reply.content, 'Complete')
        qwen.assert_any_call(4096, 512)
        qwen.assert_any_call(4096, 1024)

    def test_unverified_prompt_cannot_be_used_when_byte_bound_exceeds_window(self):
        budget = ContextBudget('general', 8192, 2400, 9000, 4700)
        with self.assertRaises(ContextBudgetExceeded):
            needs_expansion(budget, None)

    def test_github_and_document_routes_keep_relevant_compound_tools(self):
        names = ('get_github_processing_status', 'read_student_document',
            'list_universities', 'ask_university', 'update_student_profile')
        tools = [Mock(name=name) for name in names]
        for tool, name in zip(tools, names):
            tool.name = name
        ctx = {'current_message': 'Review my GitHub and compare it with this university programme'}
        selected = select_tools(tools, {'route': 'github'}, ctx)
        self.assertEqual({tool.name for tool in selected},
            {'get_github_processing_status', 'list_universities', 'ask_university'})
        ctx['chat_attachments'] = [{'name': 'cv.pdf'}]
        selected = select_tools(tools, {'route': 'document'}, ctx)
        self.assertEqual({tool.name for tool in selected},
            {'get_github_processing_status', 'read_student_document', 'list_universities', 'ask_university'})

    def test_repair_request_is_rebudgeted_before_dispatch(self):
        invalid = AIMessage(content='', tool_calls=[{'id': 'bad', 'name': 'unknown', 'args': {}}])
        valid = AIMessage(content='', tool_calls=[{'id': 'good', 'name': 'read_fact', 'args': {}}])
        budgets = [ContextBudget('general', 8192, 2400, 3000, 1500),
                   ContextBudget('general', 16384, 2400, 9000, 4500)]
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'select_context', side_effect=budgets) as sizing, \
             patch.object(model_router, 'qwen') as qwen, \
             patch.object(model_router, 'measured_invoke', side_effect=[invalid, valid]):
            result = model_router.invoke([HumanMessage(content='Read the fact')],
                [read_fact], local_only=True)
        self.assertEqual(sizing.call_count, 2)
        self.assertEqual(result.tool_calls[0]['name'], 'read_fact')
        qwen.assert_any_call(8192, 2400)
        qwen.assert_any_call(16384, 2400)

    def test_legacy_adapter_expands_and_preserves_full_message(self):
        first, second = Mock(), Mock()
        first.json.return_value = {'message': {'content': 'Incomplete'}, 'prompt_eval_count': 7500}
        second.json.return_value = {'message': {'content': 'Done'}, 'prompt_eval_count': 3000}
        text = 'x' * 8000
        with patch.object(legacy_qwen, 'model_slot', return_value=nullcontext()), \
             patch.object(legacy_qwen.httpx, 'post', side_effect=[first, second]) as post:
            legacy_qwen.create(messages=[{'role': 'user', 'content': text}])
        body = post.call_args.kwargs['json']
        self.assertEqual(post.call_count, 2)
        self.assertEqual(body['options']['num_ctx'], 16384)
        self.assertEqual(body['messages'][0]['content'], text)

    def test_legacy_adapter_never_sends_oversized_input(self):
        with patch.object(legacy_qwen.httpx, 'post') as post:
            with self.assertRaises(ContextBudgetExceeded):
                legacy_qwen.create(messages=[{'role': 'user', 'content': 'x' * 32000}])
        post.assert_not_called()
