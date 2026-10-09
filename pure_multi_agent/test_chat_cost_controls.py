import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from langchain_core.messages import AIMessage, HumanMessage

from pure_multi_agent.answer_context import compact_evidence
from pure_multi_agent.chat_cost_controls import explicit_comparison, compact_json
from pure_multi_agent.turn_policy import classify


class ChatCostTests(SimpleTestCase):
    def test_profile_routing_requires_a_complete_unambiguous_request(self):
        from pure_multi_agent.chat_cost_controls import standalone_profile_intent
        for text in ('Review my saved profile', 'How can I improve my academic profile?',
                     'Please assess my profile in detail', 'Identify strengths and gaps in my profile'):
            ctx = {'canonical_student_id': 'student', 'current_message': text}
            with patch('pure_multi_agent.model_router.invoke') as model:
                self.assertEqual(classify(ctx, [HumanMessage(content=text)])['route'], 'profile')
            model.assert_not_called()
        for text in ('Review my profile for Wright State University', 'Review my profile and save changes',
                     'Review my uploaded resume', 'How can I improve it?', 'Review his profile',
                     'Review my profile using the previous assumptions'):
            self.assertIsNone(standalone_profile_intent(text))
        ctx = {'canonical_student_id': 'student', 'current_message': 'Review my profile',
               'chat_attachments': [{'id': 1}]}
        self.assertEqual(classify(ctx, [])['route'], 'document')

    def test_complete_concept_questions_skip_only_classification(self):
        for text in ('What is a SOP?', 'Please explain a semester in detail.', 'What is a CV?'):
            ctx = {'canonical_student_id': 'student', 'current_message': text}
            with patch('pure_multi_agent.model_router.invoke') as model:
                self.assertEqual(classify(ctx, [HumanMessage(content=text)])['route'], 'general')
            model.assert_not_called()
        for text in ('What is a SOP for Ohio State University?', 'Explain a CV and save my skills.',
                     'Explain my resume', 'What is a semester there?'):
            ctx = {'canonical_student_id': 'student', 'current_message': text}
            with patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(content='{"route":"general"}')) as model:
                classify(ctx, [HumanMessage(content=text)])
            model.assert_called_once()

    def test_greetings_never_swallow_attachments_or_compound_requests(self):
        from pure_multi_agent.reference_answers import direct_reply
        for text in ('Hello!', 'Thanks a lot.'):
            self.assertTrue(direct_reply({'canonical_student_id': 'student', 'current_message': text}))
        for text in ('Hello, compare University A and University B', 'Thanks, save my profile'):
            self.assertIsNone(direct_reply({'canonical_student_id': 'student', 'current_message': text}))
        self.assertIsNone(direct_reply({'canonical_student_id': 'student', 'current_message': 'Hello!',
                                       'chat_attachments': [{'id': 1}]}))

    def test_document_shortcut_covers_only_standalone_reviews(self):
        from pure_multi_agent.chat_cost_controls import standalone_document_review
        for text in ('Summarize this PDF', 'Please review my uploaded resume.', 'Analyze the document in detail'):
            self.assertTrue(standalone_document_review(text))
        for text in ('Review my resume and update my profile', 'Save this document', 'Review this PDF for University A'):
            self.assertFalse(standalone_document_review(text))

    def test_explicit_comparison_skips_classifier_and_keeps_whole_question(self):
        from pure_multi_agent.student_graph import _reason_impl
        text = 'Compare Wright State University and Ohio State University for MS tuition, scholarships and deadlines.'
        ctx = {'canonical_student_id': 'student', 'current_message': text,
               'profile_action_checked': True, 'clarification_checked': True}
        with patch('pure_multi_agent.reference_answers.direct_reply', return_value=None), \
             patch('pure_multi_agent.reference_answers.missing_resume_reply', return_value=None), \
             patch('pure_multi_agent.reference_answers.planning_reply', return_value=None), \
             patch('pure_multi_agent.reference_answers.reference_reply', return_value=None), \
             patch('pure_multi_agent.model_router.invoke') as model:
            result = _reason_impl({'messages': [HumanMessage(content=text)]},
                                 SimpleNamespace(context={'ctx': ctx, 'prompt': ''}))
        model.assert_not_called()
        call = result['messages'][0].tool_calls[0]
        self.assertEqual(call['name'], 'compare_named_universities')
        self.assertEqual(call['args'], {'names': ['Wright State University', 'Ohio State University'], 'question': text})

    def test_ambiguous_mixed_and_noninstitution_comparisons_keep_model_routing(self):
        for text in ('Compare MIT and Stanford for fees.',
                     'Compare University A and University B and save my preferences.',
                     'Compare University A and University B using my resume.',
                     'Compare Washington and Lee University and Ohio State University.',
                     'Compare University A and University B for fees at University C.',
                     'Compare University A and University A.',
                     'Compare tuition and scholarships.',
                     'Compare universities in Germany and Canada.',
                     'Compare University A and University B, not University C.'):
            with self.subTest(text=text):
                self.assertIsNone(explicit_comparison(text))
                ctx = {'canonical_student_id': 'student', 'current_message': text}
                with patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(content='{"route":"general"}')) as model:
                    classify(ctx, [HumanMessage(content=text)])
                model.assert_called_once()

    def test_attachments_preserve_document_route(self):
        ctx = {'canonical_student_id': 'student', 'current_message': 'Compare University A and University B.',
               'chat_attachments': [{'id': 'document'}]}
        self.assertEqual(classify(ctx, [])['route'], 'document')

    def test_classifier_schema_is_supplied_once_and_recent_context_is_unchanged(self):
        text = 'Which one has better funding?'
        ctx = {'canonical_student_id': 'student', 'current_message': text}
        with patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(content='{"route":"general"}')) as model:
            classify(ctx, [HumanMessage(content='University A'), AIMessage(content='Their funding differs.'), HumanMessage(content=text)])
        messages = model.call_args.args[0]
        self.assertNotIn('"properties"', messages[0].content)
        self.assertIn('properties', model.call_args.kwargs['json_schema'])
        self.assertEqual(json.loads(messages[1].content)[-1]['text'], text)

    def test_exact_duplicates_do_not_crowd_out_other_facts_or_verified_corrections(self):
        fact = {'id': 1, 'topic': 'Fees', 'content': 'USD 12000', 'source_url': 'https://one.edu', 'year': 2026}
        corrected = {**fact, 'id': 2, 'content': 'USD 13000', 'human_verified': True}
        different_source = {**fact, 'source_url': 'https://two.edu'}
        different_year = {**fact, 'year': 2027}
        data = {'facts': [fact] * 7 + [corrected, different_source, different_year]}
        original = copy.deepcopy(data)
        result = compact_evidence(data, 'fees')
        self.assertEqual(result['facts'], [fact, corrected, different_source, different_year])
        self.assertEqual(data, original)
        self.assertEqual(json.loads(compact_json(result)), result)

    @patch('pure_multi_agent.change_proposals.effective_profile', return_value=({}, {}))
    @patch('agents.student_context.university_context', return_value={'name': 'Student'})
    @patch('agent_queries.services.answered_evidence', return_value=[])
    @patch('agent_queries.services.message')
    @patch('agent_queries.services.history', return_value=[])
    @patch('agent_queries.services.conversation_for', return_value=SimpleNamespace(pk='conversation'))
    def test_each_consultation_reads_current_evidence_and_uses_one_answer_call(self, *mocks):
        from pure_multi_agent.registered_adviser import _consult
        row = SimpleNamespace(uuid='university', name='Example University', agent_name='University adviser')
        # A complete tool response beyond the old character cutoff must remain valid JSON.
        data = {'profile': {'description': 'Context ' * 5000}, 'facts': [
            {'content': 'USD 12000', 'source_url': 'https://example.edu/fees', 'human_verified': True}]}
        revised = {**data, 'facts': [{**data['facts'][0], 'content': 'USD 13000'}]}
        with patch('pure_multi_agent.tools.university_tools._university_evidence', side_effect=[data, revised]) as retrieve, \
             patch('pure_multi_agent.registered_adviser.invoke', return_value=AIMessage(content='See the current university fee details.')) as model:
            for turn, expected in enumerate(('USD 12000', 'USD 13000')):
                ctx = {'canonical_student_id': 'student', 'student_profile': {}, 'turn_id': str(turn)}
                _consult(ctx, row, 'What are the fees?')
                tools = [message for message in model.call_args.args[0] if message.type == 'tool']
                payload = json.loads(tools[0].content)
                self.assertEqual(payload['facts'][0]['content'], expected)
                self.assertEqual(payload['facts'][0]['source_url'], 'https://example.edu/fees')
                self.assertTrue(payload['facts'][0]['human_verified'])
                self.assertEqual(payload['profile']['description'], data['profile']['description'])
            self.assertEqual(model.call_count, 2)
            self.assertEqual(retrieve.call_count, 2)
