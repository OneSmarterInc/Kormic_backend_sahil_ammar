from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from university_research.models import PublicUniversity
from university_research.catalogue import save_catalogue


def page_fixture():
    return {'url':'https://example.edu/', 'title':'Example University', 'content':'University information',
        'evidence_provider':'claude_direct', 'provider_identity':{'country':'IN'},
        'catalogue':{'description':'A research university.',
            'courses':[{'name':'BSc Physics', 'tuition':'INR 100000 per year', 'seats':'60', 'academic_year':'2026-27'}],
            'facts':[{'topic':'Scholarships','content':'Merit scholarships are available.'}],
            'intakes':[{'term':'Autumn','year':2026,'deadline':'July 2026'}]}}


class CatalogueFlowTests(TestCase):
    def setUp(self):
        self.row = PublicUniversity.objects.create(identity_key='catalogue-test', name='Example University', website='https://example.edu/')

    def test_shared_records_are_saved_without_creating_an_enrolled_account(self):
        from university_research import services
        from django_api.models import UniversityKnowledgeEntry
        canonical = save_catalogue(self.row, page_fixture())
        self.row.refresh_from_db()
        self.assertEqual(canonical.record_origin, 'researched')
        self.assertEqual(canonical.country, 'IN')
        self.assertEqual(self.row.registered_university, canonical)
        self.assertFalse(services.registered().filter(pk=canonical.pk).exists())
        self.assertEqual(self.row.courses.get().seats, '60')
        self.assertEqual(self.row.courses.get().duration, 'N/A')
        self.assertEqual(self.row.pages.get().provider, 'claude_direct')
        self.assertTrue(UniversityKnowledgeEntry.objects.filter(university_id=str(canonical.uuid),source_type='claude_direct').exists())
        save_catalogue(self.row, page_fixture())
        self.assertEqual(self.row.courses.count(), 1)
        self.assertEqual(self.row.facts.count(), 1)

    @patch('knowledge.vectors.enabled', return_value=False)
    def test_scrape_failure_saves_before_common_agent_reads_and_reuses_cache(self, _):
        from pure_multi_agent.public_adviser import consult
        def adviser(ctx, canonical, question, public_row=None):
            self.assertTrue(public_row.coverage['catalogue_saved'])
            self.assertEqual(public_row.courses.get().tuition, 'INR 100000 per year')
            self.assertEqual(canonical.description, 'A research university.')
            return {'answer':'Here are the programme details.'}
        ctx = {'canonical_student_id':'test', 'turn_id':'turn'}
        with patch('university_research.web.read_page_once', side_effect=ValueError('HTTP 403')) as scrape, \
             patch('university_research.claude_fallback.search_official_evidence', return_value=page_fixture()) as fallback, \
             patch('pure_multi_agent.registered_adviser._consult', side_effect=adviser) as agent:
            consult(ctx, self.row, 'courses')
            self.row.refresh_from_db()
            consult(ctx, self.row, 'courses')
        scrape.assert_called_once()
        fallback.assert_called_once()
        self.assertEqual(agent.call_count, 2)
        self.assertNotIn(str(self.row.pk), ctx['research_after_reply'])

    def test_scraped_catalogue_is_extracted_and_committed_before_consultation(self):
        from pure_multi_agent.public_adviser import consult
        source = {'url':self.row.website, 'content':'BSc Physics. 60 seats.', 'links':[]}
        extracted = {**page_fixture(), 'evidence_provider':'scraper_extracted'}
        with patch('university_research.web.read_page_once', return_value=source) as scrape, \
             patch('university_research.claude_fallback.search_official_evidence', return_value=extracted) as extract, \
             patch('pure_multi_agent.registered_adviser._consult', return_value={'answer':'Details'}) as agent:
            consult({}, self.row, 'courses')
        scrape.assert_called_once()
        self.assertEqual(extract.call_args.kwargs['source_page'], source)
        self.assertEqual(self.row.pages.get().provider, 'scraper_extracted')
        agent.assert_called_once()

    def test_existing_local_catalogue_is_reused_without_scraping(self):
        from django.utils import timezone
        from university_research.models import UniversityPage, UniversityFact
        from pure_multi_agent.public_adviser import consult
        self.row.fetched_at = timezone.now()
        self.row.save()
        page = UniversityPage.objects.create(university=self.row, url=self.row.website,
            content='Scholarships are available.', content_hash='local', fetched_at=timezone.now())
        UniversityFact.objects.create(university=self.row, page=page, topic='Scholarships',
            content=page.content, source_quote=page.content, fetched_at=timezone.now())
        with patch('university_research.web.read_page_once') as scrape, \
             patch('university_research.claude_fallback.search_official_evidence') as fallback, \
             patch('pure_multi_agent.registered_adviser._consult', return_value={'answer':'Saved scholarships'}) as agent:
            consult({}, self.row, 'scholarships')
        scrape.assert_not_called()
        fallback.assert_not_called()
        agent.assert_called_once()

    def test_registered_university_keeps_its_own_agent_and_records(self):
        from django.contrib.auth.models import User
        from accounts.models import Account
        from universities.models import University
        from pure_multi_agent.public_adviser import consult
        institution = University.objects.create(name=self.row.name, description='Officer managed')
        Account.objects.create(user=User.objects.create_user('officer'), role='university', university=institution)
        self.row.registered_university = institution
        self.row.save()
        with patch('pure_multi_agent.registered_adviser.consult', return_value={'answer':'Officer records'}) as agent, \
             patch('university_research.web.read_page_once') as scrape:
            self.assertEqual(consult({}, self.row, 'requirements')['answer'], 'Officer records')
        agent.assert_called_once_with({}, institution, 'requirements')
        scrape.assert_not_called()
        save_catalogue(self.row, page_fixture())
        institution.refresh_from_db()
        self.assertEqual(institution.description, 'Officer managed')

    def test_ambiguous_registered_names_require_a_choice_before_consulting(self):
        from django.contrib.auth.models import User
        from accounts.models import Account
        from universities.models import University
        from pure_multi_agent.tools.university_tools import build_tools
        for city in ('Boston', 'Austin'):
            institution = University.objects.create(name='Shared University', location=city)
            Account.objects.create(user=User.objects.create_user(city), role='university', university=institution)
        ctx = {'turn_id':'choose'}
        tools = {tool.name:tool for tool in build_tools(ctx)}
        found = tools['list_universities'].invoke({'query':'Shared University'})
        self.assertTrue(found['needs_clarification'])
        with patch('pure_multi_agent.registered_adviser.consult') as agent:
            result = tools['ask_university'].invoke({'university_id':found['candidates'][0]['id'],'question':'fees'})
        self.assertIn('identify the campus', result['error'])
        agent.assert_not_called()


class AdviserDeliveryTests(SimpleTestCase):
    def test_agent_answer_wins_over_stale_or_unrelated_scrapes(self):
        from pure_multi_agent.university_grounding import grounded_reply
        answer = '**Your fit**\n\nYour background meets the stated prerequisites. The university has not provided its seat count.'
        ctx = {'turn_id':'current', 'university_answer_evidence':{'agent_answer':answer,'answer_turn':'current'},
            'read_web_pages':{'https://other.edu/':{'provider_answer':{'text':'Wrong institution'}}}}
        self.assertEqual(grounded_reply(ctx, ''), answer)
        ctx['turn_id'] = 'next'
        self.assertEqual(grounded_reply(ctx, 'New answer'), 'New answer')

    def test_generic_formatter_is_removed(self):
        from pure_multi_agent.university_grounding import grounded_reply
        ctx = {'university_answer_evidence':{'facts':[], 'university':{'name':'Example'}}}
        self.assertEqual(grounded_reply(ctx, 'A natural answer.'), 'A natural answer.')
