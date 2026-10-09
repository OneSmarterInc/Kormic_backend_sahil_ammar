import json
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import nullcontext

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from pure_multi_agent.officer_context import prepare, fit_history, DEFERRED, CATALOG, PAGE_CHARS
from pure_multi_agent.officer_graph import POLICY, _reason
from pure_multi_agent.qwen_context import select_context, ContextBudgetExceeded
from pure_multi_agent.tools.officer_tools import build_tools


class OfficerContextTests(SimpleTestCase):
    def test_repair_reclaims_old_history_and_keeps_real_question_and_feedback(self):
        from pure_multi_agent import model_router
        question = HumanMessage(content='Change the CGPA requirement to 3.7')
        history = [HumanMessage(content='Earlier discussion'),
                   AIMessage(content='Earlier context'), question]
        _, tools = prepare({}, [], build_tools({}))
        # Choose old history that fits initially but cannot fit a repair as well.
        while True:
            try:
                select_context([SystemMessage(content=POLICY + CATALOG), *history],
                               tools, profile='evidence')
            except ContextBudgetExceeded:
                history[1] = AIMessage(content=history[1].content[:-2000])
                break
            history[1] = AIMessage(content=history[1].content + 'x' * 1000)

        dispatched = []
        invalid = AIMessage(content='', tool_calls=[{'id': 'bad', 'name': 'unknown', 'args': {}}])
        valid = AIMessage(content='', tool_calls=[{'id': 'read', 'name': 'read_university_record',
                                                  'args': {'section': 'requirements'}}])
        def prepare_request(messages, *, output_tokens=None):
            return [messages[0], *fit_history(messages[0].content, messages[1:], tools,
                protected_message=question, output_tokens=output_tokens)]
        def respond(model, messages, *args):
            dispatched.append(messages)
            return invalid if len(dispatched) == 1 else valid
        with patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'qwen_slot', return_value=nullcontext()), \
             patch.object(model_router, 'qwen'), \
             patch.object(model_router, 'needs_expansion', return_value=False), \
             patch.object(model_router, '_local_repair_instruction', return_value='Correct the rejected call ' * 250), \
             patch.object(model_router, 'measured_invoke', side_effect=respond):
            result = model_router.invoke([SystemMessage(content=POLICY + CATALOG), *history], tools,
                profile='evidence', local_only=True, message_preparer=prepare_request)
        self.assertEqual(result.tool_calls[0]['name'], 'read_university_record')
        self.assertEqual(len(dispatched), 2)
        self.assertTrue(any(message is history[1] for message in dispatched[0]))
        self.assertTrue(any(message is question for message in dispatched[1]))
        self.assertIn('Correct the rejected call', dispatched[1][-1].content)
        self.assertFalse(any(message is history[1] for message in dispatched[1]))
        self.assertEqual(len(history), 3)

    def test_repair_instruction_cannot_displace_the_current_request(self):
        question = HumanMessage(content='CGPA request ' * 2500)
        correction = HumanMessage(content='Correction feedback ' * 1000)
        with self.assertRaises(ContextBudgetExceeded):
            fit_history(POLICY, [question, correction], [], protected_message=question)

    def test_long_history_fits_by_removing_whole_old_turns_only(self):
        history = [HumanMessage(content='Old question ' * 3000),
                   AIMessage(content='', tool_calls=[{'name': 'read_university_record',
                       'args': {'section': 'requirements'}, 'id': 'old-call'}]),
                   ToolMessage(content='Old evidence', tool_call_id='old-call'),
                   AIMessage(content='Old answer ' * 3000),
                   HumanMessage(content='What are the admission requirements?')]
        messages, tools = prepare({}, history, build_tools({}))
        fitted = fit_history(POLICY + CATALOG, messages, tools)
        self.assertEqual([m.type for m in fitted], ['system', 'human'])
        self.assertIn('older conversation turns', fitted[0].content)
        self.assertIs(fitted[-1], history[-1])
        self.assertEqual(len(history), 5)
        self.assertEqual(history[2].content, 'Old evidence')
        select_context([SystemMessage(content=POLICY + CATALOG), *fitted], tools,
                       profile='evidence')

    def test_current_turn_is_never_cut_to_force_a_fit(self):
        history = [HumanMessage(content='Current question ' * 10000)]
        with self.assertRaises(ContextBudgetExceeded):
            fit_history(POLICY, history, [])
        self.assertEqual(len(history[0].content), len('Current question ' * 10000))

    def test_read_only_question_fits_without_full_edit_schemas(self):
        ctx = {}
        history = [HumanMessage(content='What are our admission requirements?')]
        messages, tools = prepare(ctx, history, build_tools(ctx))
        names = {item.name for item in tools}
        self.assertTrue({'read_university_record', 'read_portal_tab', 'interested_student_detail',
                         'resolve_university_change', 'university_change_status'}.issubset(names))
        self.assertFalse(names & DEFERRED.keys())
        budget = select_context([SystemMessage(content=POLICY + CATALOG), *messages],
                                tools, profile='evidence', max_context=16384)
        self.assertLess(budget.input_estimate + budget.num_predict, 16384)

    def test_all_edit_schemas_remain_available_one_at_a_time_without_mutations(self):
        ctx = {}
        for name in DEFERRED:
            _, tools = prepare(ctx, [], build_tools(ctx))
            loader = next(item for item in tools if item.name == 'enable_officer_tool')
            with patch('pure_multi_agent.change_proposals.propose_university') as write:
                self.assertEqual(loader.invoke({'name': name})['enabled'], name)
                write.assert_not_called()
            _, tools = prepare(ctx, [], build_tools(ctx))
            self.assertEqual({item.name for item in tools} & DEFERRED.keys(), {name})
        with self.assertRaises(ValueError):
            loader.invoke({'name': 'arbitrary_database_update'})

    def test_large_unicode_evidence_is_exactly_retrievable_and_conversation_scoped(self):
        content = json.dumps({'requirements': '学費 é admission ' * 3000}, ensure_ascii=False)
        original = ToolMessage(content=content, tool_call_id='requirements', name='read_university_record')
        projected, tools = prepare({}, [original], [])
        preview = json.loads(projected[0].content)
        self.assertEqual(preview['content'], content[:PAGE_CHARS])
        self.assertEqual(projected[0].tool_call_id, original.tool_call_id)
        self.assertEqual(original.content, content)
        reader = next(item for item in tools if item.name == 'read_officer_evidence')
        chunks, offset = [], 0
        while offset is not None:
            page = reader.invoke({'evidence_id': preview['evidence_id'], 'offset': offset})
            chunks.append(page['content'])
            offset = page['next_offset']
        self.assertEqual(''.join(chunks), content)
        _, other = prepare({}, [], [])
        with self.assertRaises(ValueError):
            next(item for item in other if item.name == 'read_officer_evidence').invoke(
                {'evidence_id': preview['evidence_id']})

    def test_previous_pages_do_not_accumulate_and_checkpoint_rebuild_preserves_evidence(self):
        source = ToolMessage(content='Evidence ' * 2000, tool_call_id='source')
        projected, tools = prepare({}, [source], [])
        reference = json.loads(projected[0].content)['evidence_id']
        reader = next(item for item in tools if item.name == 'read_officer_evidence')
        history = [source]
        for offset in (0, 4000, 8000):
            history.append(ToolMessage(content=json.dumps(reader.invoke(
                {'evidence_id': reference, 'offset': offset})),
                name='read_officer_evidence', tool_call_id=str(offset)))
        projected, rebuilt = prepare({}, history, [])
        self.assertNotIn('content', json.loads(projected[1].content))
        self.assertEqual(json.loads(projected[-1].content)['content'], source.content[8000:12000])
        self.assertEqual(next(item for item in rebuilt if item.name == 'read_officer_evidence').invoke(
            {'evidence_id': reference})['content'], source.content[:4000])

    def test_reason_does_not_eagerly_fetch_large_profile_and_logs_budget_reason(self):
        university = SimpleNamespace(uuid='u', name='Example', agent_name='Ember', website_url='',
                                     tone_descriptors=[], communication_style_notes='', never_do_notes='')
        with patch('pure_multi_agent.officer_graph.changes.officer_university', return_value=university), \
             patch('pure_multi_agent.officer_graph.changes.conversation_state', return_value={}), \
             patch('pure_multi_agent.model_router.invoke', side_effect=ContextBudgetExceeded('Estimated 19000 tokens')):
            with self.assertLogs('pure_multi_agent.officer_graph', level='WARNING') as logs:
                result = _reason({'messages': [HumanMessage(content='Requirements?')]}, SimpleNamespace(context={}))
        self.assertIn('Estimated 19000', logs.output[0])
        self.assertIn('source material', result['messages'][0].content)
