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

    def test_numeric_fees_and_null_seats_save_without_discarding_answer(self):
        page=page_fixture()
        page['catalogue']['courses'][0].update(tuition=64000,seats=None,academic_year=2026)
        page['provider_answer']={'text':'The programme tuition is 64000; seat availability is not specified.'}
        save_catalogue(self.row,page)
        course=self.row.courses.get()
        self.assertEqual(course.tuition,'64000')
        self.assertEqual(course.seats,'N/A')
        self.row.refresh_from_db()
        self.assertEqual(self.row.coverage['provider_answer'],page['provider_answer'])

    def test_transient_database_error_retries_only_save(self):
        from django.db import OperationalError
        from university_research.models import UniversityPage
        original=UniversityPage.objects.update_or_create
        calls=[]
        def write(*args,**kwargs):
            calls.append(1)
            if len(calls)==1:
                raise OperationalError('temporary database failure')
            return original(*args,**kwargs)
        with patch.object(UniversityPage.objects,'update_or_create',side_effect=write), patch('university_research.claude_fallback.Anthropic') as client:
            save_catalogue(self.row,page_fixture())
        self.assertEqual(len(calls),2)
        self.assertEqual(self.row.courses.count(),1)
        client.assert_not_called()

    def test_empty_extraction_falls_back_once_saves_then_answers(self):
        from pure_multi_agent.public_adviser import consult
        source = {'url':self.row.website,'content':'Campus news and events','links':[]}
        def adviser(ctx, row, question, public_row=None):
            self.assertEqual(public_row.courses.get().name, 'BSc Physics')
            return {'answer':'Physics programme details'}
        with patch('university_research.web.read_page_once', return_value=source) as scrape, \
             patch('university_research.claude_fallback.search_official_evidence', side_effect=[
                 {**source,'catalogue':{'coverage_notes':'Only news found'}},page_fixture()]) as research, \
             patch('pure_multi_agent.registered_adviser._consult', side_effect=adviser):
            answer=consult({},self.row,'courses, fees and admission timeline')
        self.assertEqual(answer['answer'],'Physics programme details')
        scrape.assert_called_once()
        self.assertEqual(research.call_count,2)
        self.assertIn('source_page',research.call_args_list[0].kwargs)
        self.assertNotIn('source_page',research.call_args_list[1].kwargs)

    def test_irrelevant_extraction_uses_fallback(self):
        from pure_multi_agent.public_adviser import useful_catalogue
        self.assertFalse(useful_catalogue({'catalogue':{'description':'Campus news and alumni events'}},'courses and fees'))
        self.assertTrue(useful_catalogue(page_fixture(),'courses and fees'))

    def test_empty_fallback_does_not_save_or_consult(self):
        from pure_multi_agent.public_adviser import consult
        with patch('university_research.web.read_page_once', side_effect=ValueError('blocked')), \
             patch('university_research.claude_fallback.search_official_evidence', return_value={
                 'provider_answer':{'text':'Unavailable'},'catalogue':{}}) as research, \
             patch('pure_multi_agent.registered_adviser._consult') as adviser:
            with self.assertRaisesRegex(ValueError,'No usable'):
                consult({},self.row,'courses')
        research.assert_called_once()
        adviser.assert_not_called()
        self.assertFalse(self.row.pages.exists())

    def test_shared_records_are_saved_without_creating_an_enrolled_account(self):
        from university_research import services
        from django_api.models import UniversityKnowledgeEntry
        canonical = save_catalogue(self.row, page_fixture())
        self.row.refresh_from_db()
        from universities.models import University
        self.assertFalse(University.objects.exists())
        self.assertEqual(canonical.country, 'IN')
        self.assertIsNone(self.row.registered_university_id)
        self.assertEqual(self.row.courses.get().seats, '60')
        self.assertEqual(self.row.courses.get().duration, 'N/A')
        self.assertEqual(self.row.pages.get().provider, 'claude_direct')
        self.assertFalse(UniversityKnowledgeEntry.objects.exists())
        self.assertEqual(self.row.coverage['catalogue_profile']['description'], 'A research university.')
        save_catalogue(self.row, page_fixture())
        self.assertEqual(self.row.courses.count(), 1)
        self.assertEqual(self.row.facts.count(), 1)

    @patch('knowledge.vectors.enabled', return_value=False)
    def test_scrape_failure_saves_before_common_agent_reads_and_reuses_cache(self, _):
        from pure_multi_agent.public_adviser import consult
        def adviser(ctx, canonical, question, public_row=None):
            self.assertTrue(public_row.coverage['catalogue_saved'])
            self.assertEqual(public_row.courses.get().tuition, 'INR 100000 per year')
            self.assertEqual(public_row.coverage['catalogue_profile']['description'], 'A research university.')
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
        self.row.coverage={'topic_checks':{'scholarships':timezone.now().isoformat()}}
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


class SeparationTests(TestCase):
    def test_legacy_profile_moves_without_losing_research_or_private_history(self):
        from universities.models import University
        from django_api.models import StudentProfile
        from agent_queries.models import AgentConversation, AgentConversationMessage
        from university_research.models import CommonAgentMessage
        from university_research.separation import separate_profile
        profile = University.objects.create(name='Legacy', record_origin='researched', description='Saved description')
        row = PublicUniversity.objects.create(identity_key='legacy', name='Legacy', website='https://example.edu/', registered_university=profile)
        student = StudentProfile.objects.create(name='Student')
        conversation = AgentConversation.objects.create(student=student, university=profile)
        AgentConversationMessage.objects.create(conversation=conversation, actor='university_agent', kind='reply', content='Original answer')
        self.assertTrue(separate_profile(profile.pk))
        row.refresh_from_db()
        self.assertIsNone(row.registered_university_id)
        self.assertEqual(row.coverage['catalogue_profile']['description'], 'Saved description')
        self.assertEqual(CommonAgentMessage.objects.get(student=student, university=row).content, 'Original answer')
        self.assertFalse(University.objects.exists())

    def test_enrolled_profile_is_never_removed(self):
        from universities.models import University
        from university_research.separation import separate_profile
        profile = University.objects.create(name='Enrolled')
        self.assertFalse(separate_profile(profile.pk))
        self.assertTrue(University.objects.filter(pk=profile.pk).exists())

    @patch('knowledge.vectors.enabled', return_value=False)
    def test_common_consultation_uses_database_without_creating_profile_or_personal_agent(self, _):
        from universities.models import University
        from django_api.models import StudentProfile, AgentIdentity
        from agent_queries.models import AgentConversation
        from university_research.models import CommonAgentMessage
        from pure_multi_agent.public_adviser import consult
        from langchain_core.messages import AIMessage
        row = PublicUniversity.objects.create(identity_key='common', name='Example University', website='https://example.edu/')
        save_catalogue(row, page_fixture())
        row.refresh_from_db()
        student = StudentProfile.objects.create(name='Alex')
        ctx = {'canonical_student_id':str(student.uuid), 'student_profile':{}, 'turn_id':'turn'}
        def reply(messages, tools, **kwargs):
            self.assertTrue(kwargs['local_only'])
            if messages[-1].type == 'human':
                return AIMessage(content='', tool_calls=[{'id':'read','name':'retrieve_official_information','args':{'query':'courses'}}])
            self.assertIn('INR 100000', messages[-1].content)
            return AIMessage(content='BSc Physics costs INR 100000 per year.')
        with patch('pure_multi_agent.registered_adviser.invoke', side_effect=reply), patch('pure_multi_agent.change_proposals.effective_profile', return_value=({}, [])):
            result = consult(ctx, row, 'courses')
        self.assertEqual(result['agent_name'], 'Common University Agent')
        self.assertFalse(University.objects.exists())
        self.assertFalse(AgentIdentity.objects.filter(owner_type='university').exists())
        self.assertFalse(AgentConversation.objects.exists())
        self.assertEqual(CommonAgentMessage.objects.filter(student=student, university=row).count(), 3)
