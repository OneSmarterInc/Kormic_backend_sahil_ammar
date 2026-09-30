from django.test import SimpleTestCase
from pure_multi_agent.university_grounding import grounded_reply


class UniversityDeliveryTests(SimpleTestCase):
    def test_no_source_dump_or_missing_field_template_replaces_agent_prose(self):
        ctx = {'current_message':'match my profile', 'university_answer_evidence':{
            'university':{'name':'Example'}, 'facts':[{'source_quote':'Skip to main content', 'source_url':'https://example.edu/'}]}}
        self.assertEqual(grounded_reply(ctx, 'Your academic background meets the listed requirements.'),
            'Your academic background meets the listed requirements.')

    def test_only_current_consultation_can_supply_a_completed_answer(self):
        ctx = {'turn_id':'new', 'university_answer_evidence':{'agent_answer':'Old college', 'answer_turn':'old'}}
        self.assertEqual(grounded_reply(ctx, 'Current response'), 'Current response')
        ctx['university_answer_evidence'].update(agent_answer='Current adviser response', answer_turn='new')
        self.assertEqual(grounded_reply(ctx, 'Other draft'), 'Current adviser response')

    def test_scraper_and_claude_text_cannot_bypass_the_university_agent(self):
        ctx = {'read_web_pages':{'https://example.edu/':{'provider_answer':{'text':'Raw provider response'}}}}
        self.assertEqual(grounded_reply(ctx, 'Saved-record adviser response'), 'Saved-record adviser response')
