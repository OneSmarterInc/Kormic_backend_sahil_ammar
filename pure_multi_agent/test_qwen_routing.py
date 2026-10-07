from contextlib import nullcontext
import os
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from django.conf import settings
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from pure_multi_agent.capacity import LimitedMessages
from pure_multi_agent import model_router, legacy_qwen
from kormic_backend.ollama_config import qwen_keep_alive


@tool
def read_requirements() -> dict:
    """Read saved requirements."""
    return {'gpa': 3.5}


class QwenRoutingTests(SimpleTestCase):
    def test_qwen_keep_alive_defaults_to_three_minutes(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(qwen_keep_alive(), '3m')

    def test_qwen_keep_alive_is_sent_by_both_student_transports(self):
        with patch.dict(os.environ, {'KORMIC_QWEN_KEEP_ALIVE': '2m'}):
            model_router.qwen.cache_clear()
            try:
                with patch.object(model_router, 'ChatOllama') as client:
                    model_router.qwen()
                self.assertEqual(client.call_args.kwargs['keep_alive'], '2m')
                self.assertEqual(client.call_args.kwargs['num_ctx'], 16384)
                self.assertEqual(client.call_args.kwargs['num_predict'], 2400)
                timeout = client.call_args.kwargs['client_kwargs']['timeout']
                self.assertEqual(timeout.read, 900)
                self.assertEqual(timeout.connect, 2)
            finally:
                model_router.qwen.cache_clear()

            response = Mock()
            response.json.return_value = {'message': {'content': 'Local answer'}}
            with patch.object(legacy_qwen, 'model_slot', return_value=nullcontext()), \
                 patch.object(legacy_qwen.httpx, 'post', return_value=response) as post:
                legacy_qwen.create(messages=[{'role': 'user', 'content': 'Hello'}])
            self.assertEqual(post.call_args.kwargs['json']['keep_alive'], '2m')
            self.assertEqual(post.call_args.kwargs['json']['options']['num_ctx'], 8192)
            self.assertEqual(post.call_args.kwargs['timeout'].read, 900)
            self.assertEqual(post.call_args.kwargs['timeout'].connect, 2)

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

    def test_invalid_requirement_schema_is_repaired_locally_before_execution(self):
        from pure_multi_agent.tools.officer_tools import build_tools
        tool = next(t for t in build_tools({}) if t.name == 'propose_admission_requirement')
        invalid = AIMessage(content='', tool_calls=[{'id': 'bad', 'name': tool.name, 'args': {
            'operation': 'update', 'index': 0, 'requirement': {'minimum_cgpa': 3.7, 'maximum_cgpa': 4}}}])
        valid = AIMessage(content='', tool_calls=[{'id': 'good', 'name': tool.name, 'args': {
            'operation': 'add', 'requirement': {'criterion': 'Minimum CGPA', 'detail': 'A CGPA of 3.7 to 4.0 is required.',
                'category': 'gpa', 'applies_to': 'All applicants', 'minimum': 3.7, 'maximum': 4, 'scale_maximum': 4}}}])
        invalid.response_metadata['prompt_eval_count'] = 3000
        valid.response_metadata['prompt_eval_count'] = 3000
        model = Mock()
        model.invoke.side_effect = [invalid, valid]
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen') as qwen, patch.object(model_router, 'claude') as claude:
            qwen.return_value.bind_tools.return_value = model
            result = model_router.invoke([HumanMessage(content='All applicants require 3.7 to 4.0 on a 4.0 scale')], [tool])
        self.assertEqual(result.tool_calls[0]['args']['requirement']['minimum'], 3.7)
        self.assertEqual(result.response_metadata['routing_provider'], 'qwen')
        correction = model.invoke.call_args.args[0][-1].content
        self.assertIn('minimum_cgpa', correction)
        self.assertIn('Relevant schemas', correction)
        self.assertIn('do not invent missing facts', correction)
        claude.assert_not_called()

    def test_consent_argument_can_be_repaired_without_executing_or_rewriting_it(self):
        from pure_multi_agent.tools.officer_tools import build_tools
        from pure_multi_agent.officer_graph import _validate_consent_call
        tool = next(t for t in build_tools({}) if t.name == 'resolve_university_change')
        consent = 'Yes, approve this exact change.'
        def call(quote):
            return AIMessage(content='', tool_calls=[{'id': 'r', 'name': tool.name,
                'args': {'proposal_id': 'example', 'decision': 'approve', 'confirmation_message': quote}}])
        model = Mock()
        model.invoke.side_effect = [call('I approved it'), call(consent)]
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen') as qwen, patch.object(model_router, 'claude') as claude:
            qwen.return_value.bind_tools.return_value = model
            result = model_router.invoke([HumanMessage(content=consent)], [tool],
                tool_call_validator=lambda call: _validate_consent_call(call, consent))
        self.assertEqual(result.tool_calls[0]['args']['confirmation_message'], consent)
        self.assertIn(consent, model.invoke.call_args.args[0][-1].content)
        claude.assert_not_called()

    def test_local_repair_is_bounded_and_then_uses_backup(self):
        invalid = AIMessage(content='', tool_calls=[{'id': 'bad', 'name': 'invented_tool', 'args': {}}])
        valid = AIMessage(content='', tool_calls=[{'id': 'good', 'name': 'read_requirements', 'args': {}}])
        local, backup = Mock(), Mock()
        local.invoke.return_value = invalid
        backup.invoke.return_value = valid
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'model_slot', return_value=nullcontext()), \
             patch('pure_multi_agent.capacity.model_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen') as qwen, patch.object(model_router, 'claude') as claude, \
             patch.object(model_router, 'block_provider') as block:
            qwen.return_value.bind_tools.return_value = local
            claude.return_value.bind_tools.return_value = backup
            result = model_router.invoke([HumanMessage(content='Read requirements')], [read_requirements])
        self.assertEqual(local.invoke.call_count, 2)
        self.assertEqual(backup.invoke.call_count, 1)
        self.assertEqual(result.response_metadata['routing_provider'], 'claude')
        block.assert_not_called()

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
        self.assertEqual(post.call_args.kwargs['json']['model'], settings.GITHUB_OLLAMA_MODEL)
        self.assertEqual(result.content[0].name, 'read_requirements')
        self.assertEqual(result.stop_reason, 'tool_use')


class LocalOnlyPolicyTests(SimpleTestCase):
    def test_invalid_calls_cannot_trigger_paid_fallback(self):
        local = Mock()
        local.invoke.return_value = AIMessage(content='', tool_calls=[{'id':'bad','name':'invented','args':{}}])
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen') as qwen, patch.object(model_router, 'claude') as claude:
            qwen.return_value.bind_tools.return_value = local
            with self.assertRaises(model_router.AIServiceUnavailable):
                model_router.invoke([HumanMessage(content='Read requirements')], [read_requirements], local_only=True)
        self.assertEqual(local.invoke.call_count, 2)
        claude.assert_not_called()

    def test_blocked_local_does_not_spend_on_claude(self):
        from github_profiles.scheduling import CapacityBusy
        with patch.object(model_router, 'provider_blocked', return_value=True), patch.object(model_router, 'claude') as claude:
            with self.assertRaises(CapacityBusy):
                model_router.invoke([HumanMessage(content='Hello')], local_only=True)
        claude.assert_not_called()

    def test_student_reply_is_delivered_unchanged_without_review(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason
        reply = AIMessage(content='**Courses**\n\nHere are the available programmes.')
        ctx = {'model_steps':1, 'tool_errors':5, 'university_resolution_turn':'turn'}
        with patch('pure_multi_agent.job_recovery.boundary'), \
             patch('pure_multi_agent.student_graph.build_all_tools', return_value=[read_requirements]), \
             patch.object(model_router, 'invoke', return_value=reply) as model, \
             patch('pure_multi_agent.completion.review_completion') as review:
            result = _reason({'messages':[HumanMessage(content='Courses?')]}, SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        self.assertIs(result['messages'][0], reply)
        model.assert_called_once()
        self.assertTrue(model.call_args.kwargs['local_only'])
        self.assertNotIn('force_claude', model.call_args.kwargs)
        review.assert_not_called()


    def test_university_question_does_not_offer_profile_verification(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason
        reply = AIMessage(content='University answer')
        tools = [SimpleNamespace(name='check_profile_verification'), read_requirements]
        with patch('pure_multi_agent.student_graph.build_all_tools', return_value=tools), \
             patch.object(model_router, 'invoke', return_value=reply) as model:
            _reason({'messages':[HumanMessage(content='Fees?')]}, SimpleNamespace(context={
                'ctx':{'model_steps':1,'current_message':'What are the fees at Example University?'}, 'prompt':''}))
        self.assertNotIn('check_profile_verification', [tool.name for tool in model.call_args.args[1]])
