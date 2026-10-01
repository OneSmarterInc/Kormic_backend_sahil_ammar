from django.test import TestCase, SimpleTestCase, override_settings
from django_api.models import StudentProfile
from django_api.services import profile_row_to_dict
from pure_multi_agent.profile_actions import handle, money_amount
from pure_multi_agent.response_contract import problems
from pure_multi_agent.answer_context import partial_answer


@override_settings(UNIVERSITY_VECTOR_SEARCH=False, AGENT_DISTRIBUTED_LIMITS=False)
class ProfileReceiptTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name='Audit', country='India', budget=3000000,
            budget_text='INR 30 lakh total', preferences={'preferred_locations':['Germany']})

    def context(self, text, turn='one'):
        self.student.refresh_from_db()
        profile = profile_row_to_dict(self.student)
        return dict(canonical_student_id=str(self.student.uuid), student_profile=profile,
                    profile_baseline=dict(profile), current_message=text, turn_id=turn)

    def test_budget_and_destination_require_confirmation_then_write_exact_amount(self):
        reply = handle(self.context('Change my destination to Ireland and my total budget to INR 25 lakh.'))
        self.assertIn('not been saved', reply)
        self.student.refresh_from_db()
        self.assertEqual(self.student.budget, 3000000)
        reply = handle(self.context('Yes, confirm those changes: Ireland and 25 lakh INR total budget.', 'two'))
        self.assertIn('Saved to your profile', reply)
        self.student.refresh_from_db()
        self.assertEqual(self.student.budget, 2500000)
        self.assertEqual(self.student.preferences['preferred_locations'], ['Ireland'])
        self.assertEqual(self.student.country, 'India')
        self.assertIn('2,500,000', handle(self.context('What budget and destination are saved?', 'three')))

    def test_revised_confirmation_does_not_save(self):
        handle(self.context('Change my destination to Ireland and my budget to INR 25 lakh.'))
        reply = handle(self.context('Yes, but change my budget to INR 20 lakh.', 'two'))
        self.assertNotIn('Saved to your profile', reply)
        self.student.refresh_from_db()
        self.assertEqual(self.student.budget, 3000000)

    def test_confirmation_can_repeat_an_already_saved_destination(self):
        handle(self.context('My preferred destination is Germany and my total budget is 25 lakh INR. Please update my preferences.'))
        reply = handle(self.context('Yes, confirm those changes: Germany and 25 lakh INR total budget.', 'two'))
        self.assertIn('Saved to your profile', reply)
        self.student.refresh_from_db()
        self.assertEqual(self.student.budget, 2500000)

    def test_hypothetical_is_not_a_write(self):
        self.assertIsNone(handle(self.context('If I change my budget to INR 25 lakh, what can I afford?')))
        self.assertEqual(self.student.budget, 3000000)

    def test_yes_with_negative_or_different_values_is_not_consent(self):
        handle(self.context('Change my destination to Ireland and my budget to INR 25 lakh.'))
        for text in ("Yes, don't save it.", 'Yes, confirm those changes: France and INR 20 lakh.'):
            handle(self.context(text, 'two'))
            self.student.refresh_from_db()
            self.assertEqual(self.student.budget, 3000000)


class OutputBoundaryTests(SimpleTestCase):
    def test_pronoun_is_never_searched_as_an_institution(self):
        from unittest.mock import patch
        from langchain_core.messages import AIMessage, HumanMessage
        from pure_multi_agent.turn_policy import classify
        ctx = {'canonical_student_id':'test', 'current_message':'What scholarship and housing details are saved for that university?'}
        with patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(content=
                '{"route":"document","institutions":["that university"],"followup":false}')):
            intent = classify(ctx, [HumanMessage(content=ctx['current_message'])])
        self.assertEqual(intent['institutions'], [])
        self.assertTrue(intent['followup'])
        self.assertEqual(intent['route'], 'university')

    def test_repeated_tool_is_not_executed_twice(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from langchain_core.messages import AIMessage
        from pure_multi_agent.student_graph import _tools
        tool = Mock(name='test tool')
        tool.name = 'read_requirements'
        tool.invoke.return_value = {'requirements':'Saved requirements'}
        state = {'messages':[AIMessage(content='', tool_calls=[{'id':'one', 'name':tool.name, 'args':{}}])]}
        ctx = {}
        with patch('pure_multi_agent.student_graph.build_all_tools', return_value=[tool]), \
             patch('pure_multi_agent.job_recovery.boundary'), patch('pure_multi_agent.activity.tool_activity'):
            for _ in range(3):
                _tools(state, SimpleNamespace(context={'ctx':ctx}))
        tool.invoke.assert_called_once()
        self.assertIn('completed_evidence_answer', ctx)

    def test_units(self):
        self.assertEqual(money_amount('INR 25 lakh'), 2500000)
        self.assertEqual(money_amount('USD 1.5 million'), 1500000)

    def test_applicant_values_are_not_university_facts(self):
        from pure_multi_agent.answer_checks import violations
        profile = {'budget':3000000, 'gpa':8}
        self.assertTrue(violations('Tuition is 3000000.', {}, profile))
        self.assertTrue(violations('The minimum GPA is 8.', {}, profile))
        self.assertFalse(violations('Your budget is 3000000.', {}, profile))
        from pure_multi_agent.response_contract import admission_claims
        self.assertTrue(admission_claims('A minimum GPA of 8.0 is typically required.', []))
        self.assertTrue(admission_claims('The total cost is estimated at INR 30 lakh.', []))
        self.assertTrue(admission_claims('Your three-year BCom degree is eligible for admission abroad.', []))
        self.assertFalse(admission_claims('Plan eight weeks of test preparation.', []))

    def test_rejects_internal_output_and_false_receipts(self):
        for text in ('<tool>review_student_profile</tool>', 'I have saved your preferences.', 'The fee is {fee}.'):
            self.assertTrue(problems(text))
        self.assertFalse(problems('Here is a suggested plan for your applications.'))
        self.assertFalse(problems('I have saved your preferences.', saved=True))

    def test_contribution_is_not_tuition(self):
        from pure_multi_agent.answer_checks import violations
        data = {'courses':[{'tuition':6000, 'semester_contribution':97}]}
        self.assertTrue(violations('Tuition is 97.', data))
        self.assertFalse(violations('Tuition is 6000. The semester contribution is 97.', data))

    def test_failed_draft_is_not_salvaged_into_broken_fragments(self):
        answer = partial_answer('Deadline?', {}, 'Apply by 25 October.\n\nSubmit by this date.\n\n<tool>foo</tool>')
        self.assertNotIn('this date', answer)
        self.assertNotIn('<tool>', answer)

    def test_valid_draft_is_preserved(self):
        draft = 'You can start by reviewing the programme prerequisites.'
        self.assertEqual(partial_answer('Where should I start?', {}, draft), draft)

    def test_test_preparation_is_not_test_comparison(self):
        from pure_multi_agent.reference_answers import reference_reply
        self.assertIsNone(reference_reply({'current_message':'Give me an eight week IELTS preparation plan'},
                                         {'reference_topic':'english_tests'}))
