from contextlib import nullcontext
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from pure_multi_agent.capacity import LimitedMessages
from pure_multi_agent import model_router, legacy_qwen


@tool
def read_requirements() -> dict:
    """Read saved requirements."""
    return {'gpa': 3.5}


class QwenRoutingTests(SimpleTestCase):
    def test_backup_repairs_unavailable_tool_without_executing_it(self):
        model = Mock()
        expected = AIMessage(content='', tool_calls=[{'id':'valid','name':'read_requirements','args':{}}])
        model.invoke.side_effect = [AIMessage(content='', tool_calls=[{'id':'bad','name':'select_university_candidate','args':{}}]), expected]
        with patch.object(model_router, 'provider_blocked', return_value=False), patch.object(model_router, 'model_slot', return_value=nullcontext()), patch('pure_multi_agent.capacity.model_slot', return_value=nullcontext()), patch.object(model_router, 'claude') as claude:
            claude.return_value.model = 'test'
            claude.return_value.bind_tools.return_value = model
            reply = model_router.invoke([HumanMessage(content='Find a college')], [read_requirements], force_claude=True, require_tools=True)
        self.assertEqual(reply.tool_calls[0]['name'], 'read_requirements')
        self.assertEqual(model.invoke.call_count, 2)
        self.assertIn('Allowed tools: read_requirements', model.invoke.call_args.args[0][-1].content)

    def test_busy_local_slot_is_retried_before_model_execution(self):
        from github_profiles.scheduling import CapacityBusy
        with patch.object(model_router, 'model_slot', side_effect=[CapacityBusy(), nullcontext()]) as slots, \
             patch.object(model_router.time, 'sleep'), patch('pure_multi_agent.activity.publish'):
            with model_router.qwen_slot(1500):
                pass
        self.assertEqual(slots.call_count, 2)

    def test_invalid_backup_credentials_preserve_capacity_retry(self):
        from github_profiles.scheduling import CapacityBusy
        error = RuntimeError('invalid credentials')
        error.status_code = 401
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', side_effect=CapacityBusy()), \
             patch.object(model_router, 'model_slot', return_value=nullcontext()), \
             patch('pure_multi_agent.capacity.model_slot', return_value=nullcontext()), \
             patch.object(model_router, 'claude') as claude, \
             patch.object(model_router, 'block_provider') as block, patch('pure_multi_agent.activity.publish'):
            claude.return_value.invoke.side_effect = error
            with self.assertRaises(CapacityBusy):
                model_router.invoke([HumanMessage(content='Hello')])
        claude.assert_not_called()
        block.assert_not_called()

    def test_current_gaps_do_not_claim_saved_projects_or_skills_are_missing(self):
        from django_api.services import current_profile_gaps
        profile = {'projects': [{'name': 'Example'}], 'skills': ['Python'], 'gpa': 0,
            'gaps': ['projects', 'technical_skills', 'gpa', 'budget', 'Needs stronger research evidence']}
        self.assertEqual(current_profile_gaps(profile), ['budget', 'Needs stronger research evidence'])

    def test_followup_system_instruction_is_merged_before_conversation(self):
        history = [SystemMessage(content='Student assistant'), HumanMessage(content='Did they reply?'),
                   AIMessage(content='I will check.'), SystemMessage(content='Perform the university consultation now')]
        for provider in ('Qwen', 'Claude'):
            normalized = model_router.provider_messages(history, provider, 'test-model')
            self.assertEqual([m.type for m in normalized], ['system', 'human', 'ai'])
            self.assertIn('Perform the university consultation now', normalized[0].content)
            self.assertEqual(normalized[1:], history[1:3])
        self.assertEqual(history[-1].type, 'system')
        from langchain_anthropic.chat_models import _format_messages
        system, conversation = _format_messages(model_router.provider_messages(history, 'Claude', 'test-model'))
        self.assertIn('Perform the university consultation now', str(system))
        self.assertEqual(len(conversation), 2)

    def test_runtime_identity_is_provider_specific_and_does_not_mutate_history(self):
        history = [SystemMessage(content='University assistant'), HumanMessage(content='Who are you?')]
        qwen = model_router.provider_messages(history, 'Qwen', 'qwen3:1.7b')
        claude = model_router.provider_messages(history, 'Claude', 'fallback-model')
        self.assertIn('You are not Claude', qwen[0].content)
        self.assertIn('Claude is developed by Anthropic', claude[0].content)
        self.assertEqual(history[0].content, 'University assistant')

    def test_qwen_retry_uses_tools_without_claude(self):
        model = Mock()
        expected = AIMessage(content='', tool_calls=[{'id': 'r', 'name': 'read_requirements', 'args': {}}])
        model.invoke.side_effect = [AIMessage(content='Old incorrect answer'), expected]
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'model_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen') as qwen, patch.object(model_router, 'claude') as claude:
            qwen.return_value.bind_tools.return_value = model
            reply = model_router.invoke([SystemMessage(content='Use tools'), HumanMessage(content='Requirements?')],
                                        [read_requirements], require_tools=True)
        self.assertEqual(reply.response_metadata['routing_provider'], 'qwen')
        self.assertEqual(model.invoke.call_count, 2)
        claude.assert_not_called()

    def test_legacy_prefers_qwen_and_falls_back_on_failure(self):
        fallback = Mock()
        wrapper = LimitedMessages(fallback)
        with patch.object(legacy_qwen, 'create', return_value='local'):
            self.assertEqual(wrapper.create(messages=[]), 'local')
        fallback.create.assert_not_called()
        with patch.object(legacy_qwen, 'create', side_effect=ValueError('offline')), \
             patch('pure_multi_agent.capacity.model_slot', return_value=nullcontext()):
            wrapper.create(messages=[])
        fallback.create.assert_called_once_with(messages=[])

    def test_legacy_transport_preserves_tool_schema_and_response(self):
        response = Mock()
        response.json.return_value = {'message': {'content': '', 'tool_calls': [
            {'function': {'name': 'read_requirements', 'arguments': {}}}]}}
        with patch.object(legacy_qwen, 'model_slot', return_value=nullcontext()), \
             patch.object(legacy_qwen.httpx, 'post', return_value=response) as post:
            result = legacy_qwen.create(messages=[{'role': 'user', 'content': 'Read requirements'}],
                tools=[{'name': 'read_requirements', 'input_schema': {'type': 'object', 'properties': {}}}])
        self.assertEqual(post.call_args.kwargs['json']['model'], 'qwen3:1.7b')
        self.assertEqual(result.content[0].name, 'read_requirements')
        self.assertEqual(result.stop_reason, 'tool_use')
