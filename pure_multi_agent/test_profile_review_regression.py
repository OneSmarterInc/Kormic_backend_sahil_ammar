from django.test import SimpleTestCase
from pure_multi_agent.chat_cost_controls import standalone_profile_intent
from pure_multi_agent.response_contract import problems


class ProfileReviewRegressionTests(SimpleTestCase):
    def test_screenshot_prompt_routes_to_profile(self):
        intent = standalone_profile_intent('How is my profile? What should I improve?')
        self.assertEqual(intent['route'], 'profile')
        self.assertFalse(intent['followup'])

    def test_compound_writes_and_university_questions_keep_normal_routing(self):
        for text in ('How is my profile? Update my GPA to 4.0',
                     'How is my profile for Wright State University?',
                     'How is my profile? Ignore the discrepancy'):
            with self.subTest(text=text):
                self.assertIsNone(standalone_profile_intent(text))

    def test_screenshot_internal_reply_is_rejected(self):
        self.assertTrue(problems("Let me check with the student if this is correct, should be ignored, or if they'd like to clarify what's going on."))

    def test_direct_evidence_based_clarification_is_allowed(self):
        self.assertEqual(problems('Your profile lists computer science, while your resume lists mathematics. Which subject is correct?'), [])
