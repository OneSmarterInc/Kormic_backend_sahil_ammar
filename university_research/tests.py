import uuid
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from rest_framework.test import APIClient

from accounts.models import Account, TOTPDevice, GitHubOAuthConnection
from django_api.models import StudentProfile, GitHubProfileSnapshot, GitHubSyncRun
from .models import PublicUniversity, ResearchRun, UniversityPage, UniversityFact, UniversitySearch, AdvisingArtifact
from . import services


def public(name='Example University', **extra):
    return PublicUniversity.objects.create(identity_key=uuid.uuid4().hex, name=name, website='https://example.edu/', **extra)


class RouterTests(SimpleTestCase):
    def setUp(self):
        for name, replacement in [('model_slot', lambda *args: nullcontext()), ('provider_blocked', lambda p: False), ('block_provider', lambda *args: None)]:
            p = patch('pure_multi_agent.model_router.' + name, replacement)
            p.start(); self.addCleanup(p.stop)
        p = patch('pure_multi_agent.capacity.model_slot', lambda *args: nullcontext())
        p.start(); self.addCleanup(p.stop)

    @patch('pure_multi_agent.model_router.claude')
    @patch('pure_multi_agent.model_router.qwen')
    def test_qwen_first_and_claude_only_on_failure(self, qwen, claude):
        from pure_multi_agent.model_router import invoke
        qwen.return_value.invoke.return_value = AIMessage(content='Qwen answer')
        self.assertEqual(invoke([HumanMessage(content='Hello')]).content, 'Qwen answer')
        claude.assert_not_called()
        qwen.return_value.invoke.side_effect = httpx.ConnectError('offline')
        claude.return_value.invoke.return_value = AIMessage(content='Claude answer')
        self.assertEqual(invoke([HumanMessage(content='Hello')]).response_metadata['routing_provider'], 'claude')

    @patch('pure_multi_agent.model_router.claude')
    @patch('pure_multi_agent.model_router.qwen')
    def test_invalid_tool_schema_falls_back(self, qwen, claude):
        from pure_multi_agent.model_router import invoke
        @tool
        def read_record(record_id: int) -> dict:
            """Read a record."""
            return {}
        qwen.return_value.bind_tools.return_value.invoke.return_value = AIMessage(content='', tool_calls=[{'id': 'a', 'name': 'read_record', 'args': {'record_id': 'invalid'}}])
        claude.return_value.bind_tools.return_value.invoke.return_value = AIMessage(content='Recovered')
        self.assertEqual(invoke([HumanMessage(content='Hi')], [read_record]).content, 'Recovered')

    @patch('pure_multi_agent.model_router.claude')
    @patch('pure_multi_agent.model_router.qwen')
    def test_busy_provider_does_not_burst_paid_fallback(self, qwen, claude):
        from github_profiles.scheduling import CapacityBusy
        from pure_multi_agent.model_router import invoke
        qwen.return_value.invoke.side_effect = CapacityBusy()
        with self.assertRaises(CapacityBusy):
            invoke([HumanMessage(content='Hi')])
        claude.assert_not_called()

    def test_source_boundary_rejects_intermediaries_and_local_urls(self):
        from .web import require_institution_site, canonical_url
        for url in ['https://en.wikipedia.org/wiki/Test', 'https://www.shiksha.com/test']:
            with self.assertRaises(ValueError):
                require_institution_site(url)
        for url in ['http://127.0.0.1/admin', 'http://localhost/', 'http://example.edu:11434', 'https://user:password@example.edu']:
            with self.assertRaises(ValueError):
                canonical_url(url)

    def test_quote_matching_normalizes_typography_but_not_wording(self):
        from .agent import normalize
        self.assertEqual(normalize('One of the world’s leading centers.'), normalize("One of the world's leading centers."))
        self.assertNotEqual(normalize('Tuition is $25,000.'), normalize('Tuition is $20,000.'))

    @patch('pure_multi_agent.model_router.claude')
    def test_claude_bad_arguments_are_returned_to_graph_for_safe_repair(self, claude):
        from pure_multi_agent.model_router import invoke
        @tool
        def read_record(record_id: int) -> dict:
            """Read a record."""
            return {'id': record_id}
        claude.return_value.bind_tools.return_value.invoke.return_value = AIMessage(content='', tool_calls=[
            {'id': 'repair', 'name': 'read_record', 'args': {'record_id': 'invalid'}}])
        response = invoke([HumanMessage(content='Read a record')], [read_record], force_claude=True)
        self.assertEqual(response.tool_calls[0]['id'], 'repair')
        # The actual tool still rejects it before any business logic executes.
        with self.assertRaises(ValueError):
            read_record.invoke(response.tool_calls[0]['args'])

    @patch('pure_multi_agent.model_router.claude')
    @patch('pure_multi_agent.model_router.qwen')
    def test_required_workflow_action_falls_back_when_qwen_only_replies(self, qwen, claude):
        from pure_multi_agent.model_router import invoke
        @tool
        def prepare_change() -> dict:
            """Prepare a change for confirmation."""
            return {}
        qwen.return_value.bind_tools.return_value.invoke.return_value = AIMessage(content='Please confirm')
        claude.return_value.bind_tools.return_value.invoke.return_value = AIMessage(content='', tool_calls=[{'id': 'draft', 'name': 'prepare_change', 'args': {}}])
        result = invoke([HumanMessage(content='Prepare changes')], [prepare_change], require_tools=True)
        self.assertEqual(result.tool_calls[0]['name'], 'prepare_change')
        claude.return_value.bind_tools.assert_called_once_with([prepare_change], tool_choice='any')


@override_settings(UNIVERSITY_VECTOR_SEARCH=False, AGENT_DISTRIBUTED_LIMITS=False)
class ResearchTests(TestCase):
    def test_submit_research_accepts_summarized_program_names(self):
        from university_research.agent import build_tools
        url='https://example.edu/programs'
        quotes=["Learn more about the diverse degree programs at UChicago, which offer students flexibility through full-time, part-time, executive, and online formats:",
            "For those looking to continue their academic journey or pivot their careers, UChicago's 60-plus programs offer opportunities to conduct research in cutting-edge labs and facilities."]
        result={}
        submit={t.name:t for t in build_tools({url:{'url':url,'content':'\n'.join(quotes)}},result,'https://example.edu/')}['submit_research']
        response=submit.invoke({'facts':[],'courses':[{'name':name,'source_url':url,'source_quote':quote}
            for name,quote in zip(["Master's Programs",'Doctoral Programs'],quotes)],'intakes':[],'coverage_notes':'Degree categories; individual course details not supplied.'})
        self.assertTrue(response['accepted'])
        self.assertEqual([c['name'] for c in result['courses']],["Master's Programs",'Doctoral Programs'])

    def test_unlisted_university_saves_catalogue_before_agent_reply(self):
        from django_api.models import AgentJob
        from university_research.services import publish_delivered_evidence
        from pure_multi_agent.university_grounding import grounded_reply
        website = 'https://new-example.edu/'
        name = 'New Example University'
        self.ctx['current_message'] = 'New Example University tuition fees'
        cited = {'url':website+'fees','title':'2026 Undergraduate tuition','content':'New Example University undergraduate tuition for 2026 is USD 12000 per year.','links':[],'evidence_provider':'claude_web_search'}
        page = {'url':website,'title':'2026 Undergraduate tuition','content':cited['content'],'links':[],'citation_pages':[cited],'evidence_provider':'claude_web_search'}
        provider_answer={'text':cited['content'],'sources':[cited['url']]}
        page['catalogue']={'description':name,'facts':[{'topic':'Tuition','content':cited['content']}]}
        page['provider_answer']=provider_answer
        cited['provider_answer']=provider_answer
        with patch('university_research.web.search_web',return_value=[{'url':website,'title':name,'snippet':name}]):
            found = self.tools()['list_universities'].invoke({'query':name})
        self.assertEqual(found['status'],'web_results_need_resolution')
        with patch('university_research.web.read_page',side_effect=ValueError('Official page unavailable: HTTP 403')) as scrape, patch('university_research.claude_fallback.search_official_evidence',return_value=page) as fallback:
            self.tools()['read_university_webpage'].invoke({'url':website})
        self.assertEqual(scrape.call_count,1)
        fallback.assert_called_once()
        identity = self.tools()['identify_university_candidates'].invoke({'search_id':found['search_id'],'candidates':[{'name':name,'website':website,'source_indices':[0],'official_identity_quote':'This paraphrased quote is not in the source'}]})
        self.assertEqual(identity['count'],1)
        self.assertEqual(identity['candidates'][0]['sources'][0]['identity_quote'],cited['content'])
        with patch('university_research.web.validate_public_base_url'):
            selected = self.tools()['select_university_candidate'].invoke({'search_id':found['search_id'],'candidate_index':1})
        uid = selected['university']['id']
        with self.adviser_reply('Tuition is USD 12000 per year.'):
            self.tools()['ask_university'].invoke({'university_id':uid,'question':'tuition fees'})
        answer = grounded_reply(self.ctx,'')
        self.assertIn('USD 12000',answer)
        row = PublicUniversity.objects.get(pk=uid.split(':',1)[1])
        self.assertTrue(row.pages.exists())
        pending={'pages':list(self.ctx['university_cache_after_reply'].values()),'universities':[str(row.pk)],'missing':[],'checked':['fees']}
        job=AgentJob.objects.create(owner_key='synthetic:new-university',idempotency_key='delivery',kind='student',status='completed',payload={'university_cache_pending':pending})
        self.assertTrue(publish_delivered_evidence(job.pk))
        self.assertTrue(row.pages.exists())
        with patch('university_research.web.search_web') as search:
            cached = self.tools()['list_universities'].invoke({'query':name})
        self.assertEqual(cached['source'],'researched')
        search.assert_not_called()

    def adviser_reply(self, answer):
        return patch('pure_multi_agent.registered_adviser.invoke', side_effect=[
            AIMessage(content='', tool_calls=[{'id':'read-catalogue','name':'retrieve_official_information','args':{'query':'courses fees'}}]),
            AIMessage(content=answer)])

    def setUp(self):
        # Worker cleanup must not close the TestCase transaction.
        cleanup = patch("university_research.worker.close_old_connections")
        cleanup.start(); self.addCleanup(cleanup.stop)
        completion = patch("pure_multi_agent.completion.review_completion", return_value={"complete": True, "next_action": ""})
        completion.start(); self.addCleanup(completion.stop)
        self.student = StudentProfile.objects.create(name='Student', gpa=3.6)
        self.user = User.objects.create_user(username='student', email='student@example.test')
        Account.objects.create(user=self.user, role='student', student_profile=self.student)
        TOTPDevice.objects.create(user=self.user, confirmed_at=timezone.now())
        self.ctx = {'canonical_student_id': str(self.student.uuid), 'student_profile': {'name': 'Student', 'gpa': 3.6}, 'current_message': 'Boston', 'turn_id': 'test-turn'}
        self.client = APIClient(); self.client.force_authenticate(self.user)

    def tools(self):
        from pure_multi_agent.tools import build_all_tools
        return {t.name: t for t in build_all_tools(self.ctx)}

    @patch('university_research.web.search_web')
    def test_registered_then_cache_then_web_and_badge(self, search):
        from universities.models import University
        uni = University.objects.create(name='Example University')
        cached = public(fetched_at=timezone.now())
        # An unclaimed DB record is not enrollment.
        result = self.tools()['list_universities'].invoke({'query': 'Example University'})
        self.assertEqual(result['source'], 'researched')
        self.assertFalse(result['candidates'][0]['listed'])
        officer = User.objects.create_user(username='officer')
        Account.objects.create(user=officer, role='university', university=uni)
        result = self.tools()['list_universities'].invoke({'query': 'Example University'})
        self.assertEqual(result['source'], 'registered')
        self.assertTrue(result['candidates'][0]['listed'])
        search.assert_not_called()
        search.return_value = [{'url': 'https://different.edu/', 'title': 'Different University', 'snippet': 'Official home'}]
        result = self.tools()['list_universities'].invoke({'query': 'Different University'})
        self.assertEqual(result['status'], 'web_results_need_resolution')
        search.assert_called_once()

    def test_ambiguity_requires_real_student_clarification(self):
        candidates = [{'name': 'Example University', 'website': 'https://example.edu/', 'address': place, 'country': 'US', 'sources': []} for place in ['Boston', 'Austin']]
        search = UniversitySearch.objects.create(student=self.student, query='Example', candidates={'universities': candidates})
        select = self.tools()['select_university_candidate']
        result = select.invoke({'search_id': str(search.pk), 'candidate_index': 2, 'confirmation_detail': 'Austin'})
        self.assertIn('error', result)
        self.assertFalse(PublicUniversity.objects.exists())
        with patch('university_research.web.canonical_url', lambda u: u):
            result = select.invoke({'search_id': str(search.pk), 'candidate_index': 1, 'confirmation_detail': 'Boston'})
        self.assertEqual(result['university']['address'], 'Boston')
        # Deferred until the answer is generated.
        self.assertFalse(ResearchRun.objects.exists())

    def test_discovery_cannot_claim_official_status_from_snippets(self):
        search = UniversitySearch.objects.create(student=self.student, query='Example', candidates={'results': [{'url': 'https://example.edu/', 'title': 'Example University'}]})
        candidate = {'name': 'Example University', 'website': 'https://example.edu/', 'source_indices': [0], 'official_identity_quote': 'Welcome to Example University'}
        result = self.tools()['identify_university_candidates'].invoke({'search_id': str(search.pk), 'candidates': [candidate]})
        self.assertIn('error', result)
        self.ctx['read_web_pages'] = {'https://example.edu/': {'url': 'https://example.edu/', 'title': 'Example University', 'content': 'Welcome to Example University'}}
        result = self.tools()['identify_university_candidates'].invoke({'search_id': str(search.pk), 'candidates': [candidate]})
        self.assertEqual(result['count'], 1)

    @patch('university_research.claude_fallback.search_official_evidence')
    @patch('university_research.web.read_page_once')
    def test_ambiguous_discovery_asks_before_scraping_calling_claude_or_saving(self, scrape, claude):
        choices = [{'name':'Example University','website':'https://example.edu/','address':'Boston','source_indices':[0],'official_identity_quote':'Example University Boston'},
            {'name':'Example University','website':'https://other.edu/','address':'London','source_indices':[1],'official_identity_quote':'Example University London'}]
        search=UniversitySearch.objects.create(student=self.student,query='Example',candidates={'results':[{'url':c['website']} for c in choices]})
        result=self.tools()['identify_university_candidates'].invoke({'search_id':str(search.pk),'candidates':choices})
        self.assertTrue(result['needs_clarification'])
        self.assertEqual(len(self.ctx['university_clarification']),2)
        self.assertIn('error',self.tools()['select_university_candidate'].invoke({'search_id':str(search.pk),'candidate_index':1}))
        scrape.assert_not_called(); claude.assert_not_called()
        self.assertFalse(PublicUniversity.objects.exists())

    @patch('university_research.claude_fallback.search_official_evidence')
    @patch('university_research.web.read_page',side_effect=ValueError('HTTP 403'))
    def test_direct_fallback_one_call_full_answer_and_no_duplicate_research(self, scrape, claude):
        website='https://example.edu/'
        answer={'text':'Courses: MSc. Fees: N/A. Seats: N/A. Scholarships: need-based.','sources':[website],'provider':'claude_direct'}
        claude.return_value={'url':website,'title':'Example University','content':answer['text'],'links':[],
            'provider_answer':answer,'evidence_provider':'claude_direct','catalogue':{'description':'Example University','courses':[{'name':'MSc Physics','tuition':'USD 10000 per year'}]}}
        search=UniversitySearch.objects.create(student=self.student,query='Example University',candidates={'results':[{'url':website}]})
        self.ctx.update(known_web_urls={website},turn_id='direct',current_message='courses fees seats scholarships')
        tools=self.tools()
        tools['read_university_webpage'].invoke({'url':website})
        tools['identify_university_candidates'].invoke({'search_id':str(search.pk),'candidates':[{
            'name':'Example University','website':website,'source_indices':[0],'official_identity_quote':'Example University'}]})
        with patch('university_research.web.canonical_url',lambda url:url):
            selected=tools['select_university_candidate'].invoke({'search_id':str(search.pk),'candidate_index':1})
        with self.adviser_reply('Physics is offered. Fees are USD 10000 per year.'):
            evidence=tools['ask_university'].invoke({'university_id':selected['university']['id'],'question':'courses fees seats scholarships'})
        self.assertIn('Physics',evidence['answer'])
        self.assertFalse(self.ctx['research_after_reply'])
        self.assertFalse(self.ctx['university_cache_after_reply'])
        self.assertTrue(PublicUniversity.objects.get().pages.exists())
        self.assertFalse(self.ctx['university_source_search_required'])
        scrape.assert_called_once(); claude.assert_called_once()

    @patch('university_research.claude_fallback.search_official_evidence')
    def test_successful_heading_only_scrape_delivers_claude_answer_and_terminates(self, claude):
        from pure_multi_agent.student_graph import _reason
        from types import SimpleNamespace
        row=public(name='Example University')
        uid='public:'+str(row.pk)
        answer={'text':'**Example University**\n\nCourses: Physics. Fees: USD 10000 per year. Seats: N/A.',
            'sources':[row.website],'provider':'claude_direct'}
        claude.return_value={'url':row.website,'title':row.name,'content':answer['text'],'links':[],
            'provider_answer':answer,'evidence_provider':'claude_direct','catalogue':{'description':'Example University','courses':[{'name':'MSc Physics','tuition':'USD 10000 per year'}]}}
        self.ctx.update(turn_id='headings',university_resolution_turn='headings',university_candidates=[uid],
            current_message='courses fees seats',read_web_pages={row.website:{'url':row.website,'title':row.name,'content':'Tuition and Fees 2026-27','links':[]}},
            university_cache_after_reply={row.website:{'university_id':str(row.pk),'page':{'url':row.website,'title':row.name,'content':'Tuition and Fees 2026-27'}}})
        with self.adviser_reply('Physics is offered. The university has not provided its seat count.'):
            self.tools()['ask_university'].invoke({'university_id':uid,'question':'courses fees seats'})
        with patch('pure_multi_agent.student_graph.build_all_tools',return_value=[]), patch('pure_multi_agent.change_proposals.conversation_state',return_value={}), patch('pure_multi_agent.model_router.invoke') as model:
            response=_reason({'messages':[]},SimpleNamespace(context={'ctx':self.ctx,'prompt':''}))
        self.assertEqual(response['messages'][0].content,'Physics is offered. The university has not provided its seat count.')
        self.assertFalse(self.ctx['research_after_reply'])
        self.assertFalse(self.ctx['university_source_search_required'])
        self.assertFalse(self.ctx['university_cache_after_reply'])
        self.assertEqual(row.courses.get().name,'MSc Physics')
        model.assert_not_called(); claude.assert_called_once()

    def test_fetched_official_title_can_verify_identity_without_site_navigation(self):
        search = UniversitySearch.objects.create(student=self.student, query='MIT', candidates={'results':[{'url':'https://www.mit.edu/','title':'Search hint'}]})
        self.ctx['read_web_pages'] = {'https://www.mit.edu/': {'url':'https://www.mit.edu/','title':'MIT - Massachusetts Institute of Technology','content':'Research highlights'}}
        candidate = {'name':'Massachusetts Institute of Technology','website':'https://www.mit.edu/','source_indices':[0],'official_identity_quote':'MIT - Massachusetts Institute of Technology'}
        result = self.tools()['identify_university_candidates'].invoke({'search_id':str(search.pk),'candidates':[candidate]})
        self.assertEqual(result['count'],1)

    @patch('university_research.claude_fallback.search_official_evidence')
    @patch('university_research.web.read_page_once')
    def test_caltech_homepage_recovers_through_about_page_with_nine_references(self, read, fallback):
        website = 'https://www.caltech.edu/'
        about = website + 'about'
        results = [{'url':website,'title':'Home'}, {'url':about,'title':'About Caltech'}]
        results.extend({'url':website+'news/'+str(i),'title':'News'} for i in range(7))
        search = UniversitySearch.objects.create(student=self.student, query='Caltech University', candidates={'results':results})
        self.ctx['read_web_pages'] = {website:{'url':website,'title':'Home - www.caltech.edu',
            'content':'Caltech Homepage. Research news.', 'links':[{'url':about,'label':'About'}]}}
        read.return_value = {'url':about,'title':'About Caltech',
            'content':'California Institute of Technology (Caltech)', 'links':[]}
        result = self.tools()['identify_university_candidates'].invoke({'search_id':str(search.pk), 'candidates':[{
            'name':'California Institute of Technology','website':website,'source_indices':list(range(9)),
            'official_identity_quote':'Caltech is a world-renowned science and engineering institute.'}]})
        self.assertEqual(result['count'],1)
        self.assertEqual(result['candidates'][0]['sources'][0]['url'],about)
        self.assertNotIn('university_discovery_blocked',self.ctx)
        read.assert_called_once_with(about)
        fallback.assert_not_called()

    @patch('university_research.claude_fallback.search_official_evidence', side_effect=ValueError('No identity evidence'))
    def test_invalid_identity_attempts_are_bounded(self, fallback):
        search = UniversitySearch.objects.create(student=self.student, query='MIT', candidates={'results':[{'url':'https://www.mit.edu/','title':'Search hint'}]})
        self.ctx['read_web_pages'] = {'https://www.mit.edu/': {'url':'https://www.mit.edu/','title':'Unrelated College','content':'Research highlights'}}
        candidate = {'name':'Massachusetts Institute of Technology','website':'https://www.mit.edu/','source_indices':[0],'official_identity_quote':'Invented institution identity'}
        for _ in range(3):
            result = self.tools()['identify_university_candidates'].invoke({'search_id':str(search.pk),'candidates':[candidate]})
        self.assertEqual(result['attempt'],3)
        self.assertIn('university_discovery_blocked',self.ctx)
        fallback.assert_called_once()

    def test_search_and_saved_advice_are_owner_scoped(self):
        other = StudentProfile.objects.create(name='Other')
        search = UniversitySearch.objects.create(student=other, query='private', candidates={})
        with self.assertRaises(UniversitySearch.DoesNotExist):
            self.tools()['identify_university_candidates'].invoke({'search_id': str(search.pk), 'candidates': []})
        item = AdvisingArtifact.objects.create(student=other, kind='roadmap', title='Private', content={'secret': 'private'})
        result = self.tools()['save_advising_artifact'].invoke({'kind': 'roadmap', 'title': 'Changed', 'content': {}, 'artifact_id': str(item.pk)})
        self.assertIn('error', result)
        self.assertFalse(self.tools()['get_saved_advice'].invoke({})['items'])

    def test_processing_blocks_partial_github_findings(self):
        from pure_multi_agent.tools.github_tools import github_evidence
        connection = GitHubOAuthConnection.objects.create(user=self.user, github_user_id=123, github_username='student')
        snap = GitHubProfileSnapshot.objects.create(student=self.student, connection=connection, github_user_id=123, summary='Partial misleading finding')
        GitHubSyncRun.objects.create(profile=snap)
        result = github_evidence(str(self.student.uuid))
        self.assertEqual(result['status'], 'processing')
        self.assertNotIn('summary', result)
        self.assertNotIn('Partial misleading', str(result))

    def test_freshness_policy_is_admin_only_and_applies_to_records(self):
        row = public(fetched_at=timezone.now()-timedelta(days=31))
        self.assertTrue(services.reference(row)['stale'])
        self.assertEqual(self.client.patch('/api/superuser/update-information/', {'refresh_days': 90}, format='json').status_code, 403)
        self.user.account.role = 'superuser'; self.user.account.save()
        self.assertEqual(self.client.patch('/api/superuser/update-information/', {'refresh_days': 90}, format='json').status_code, 200)
        self.assertFalse(services.reference(row)['stale'])
        self.assertEqual(self.client.patch('/api/superuser/update-information/', {'refresh_days': 0}, format='json').status_code, 400)

    def test_registered_website_research_keeps_officer_records_and_freshness(self):
        from universities.models import University
        uni = University.objects.create(name='Enrolled University', website_url='https://example.edu/', description='Officer managed')
        officer = User.objects.create_user(username='registered-officer')
        Account.objects.create(user=officer, role='university', university=uni)
        row = services.public_for_registered(uni)
        self.assertEqual(row.pk, services.public_for_registered(uni).pk)
        row.fetched_at = timezone.now() - timedelta(days=31)
        row.save()
        run = services.queue_research(row)
        ref = services.reference(uni)
        self.assertEqual(ref['research_id'], 'public:' + str(row.pk))
        self.assertTrue(ref['stale'])
        self.assertTrue(ref['processing'])
        self.assertEqual(run.university.registered_university_id, uni.pk)
        uni.refresh_from_db()
        self.assertEqual(uni.description, 'Officer managed')

    def test_queue_deduplicates_and_old_lease_cannot_publish(self):
        from .worker import claim, release
        row = public()
        first = services.queue_research(row, self.user)
        self.assertEqual(first.pk, services.queue_research(row, self.user).pk)
        owned = claim()
        ResearchRun.objects.filter(pk=first.pk).update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        newer = claim()
        self.assertNotEqual(owned.lease_token, newer.lease_token)
        self.assertFalse(release(owned, status='completed'))

    @patch('university_research.agent.read_page')
    @patch('university_research.agent.invoke')
    def test_real_research_graph_resumes_and_persists_structured_sources(self, model, read):
        from .worker import work_once
        row = public()
        run = services.queue_research(row, self.user)
        quote = 'MSc Computing tuition is USD 12000. Fall 2027 deadline is March 1, 2027.'
        read.return_value = {'url': row.website, 'title': 'Courses', 'content': quote, 'links': []}
        def decide(messages, tools, **kwargs):
            if not any(m.type == 'tool' for m in messages):
                return AIMessage(content='', tool_calls=[{'id': 'read', 'name': 'read_official_page', 'args': {'url': row.website}}])
            return AIMessage(content='', tool_calls=[{'id': 'submit', 'name': 'submit_research', 'args': {
                'facts': [{'topic': 'Tuition', 'content': 'MSc Computing costs USD 12000.', 'source_url': row.website, 'source_quote': quote}],
                'courses': [{'name': 'MSc Computing', 'tuition': 'USD 12000', 'source_url': row.website, 'source_quote': quote}],
                'intakes': [{'course_name': 'MSc Computing', 'term': 'Fall', 'year': 2027, 'deadline': 'March 1, 2027', 'source_url': row.website, 'source_quote': quote}],
                'coverage_notes': 'One official program page; not a complete catalog.'}}])
        model.side_effect = decide
        for _ in range(5):
            work_once()
        run.refresh_from_db(); row.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(row.courses.get().name, 'MSc Computing')
        self.assertEqual(row.intakes.get().year, 2027)
        self.assertEqual(row.facts.count(), 3)
        self.assertEqual(row.coverage['embedding_status'], 'lexical_development_mode')
        self.assertEqual(row.pages.get().url, row.website)
        self.assertEqual(model.call_count, 2)


    def test_live_official_read_is_saved_without_private_question(self):
        row=public()
        page={'url':row.website+'housing','title':'Housing','content':'University hostels provide shared rooms and a dining hall.'}
        services.save_live_page(row,page,'My private student number 123456; tell me about hostels')
        self.assertEqual(row.pages.count(),1)
        fact=row.facts.get()
        self.assertNotIn('123456',fact.content)
        self.assertIn(fact.source_quote,page['content'])
        answer=services.retrieve(row,'hostels')
        self.assertEqual(answer['facts'][0]['source_url'],page['url'])

    def test_saved_alias_and_refresh_policy_drive_reuse(self):
        from .models import InformationPolicy
        row = public(fetched_at=timezone.now()-timedelta(days=31))
        row.name = 'Indian Institute of Technology Bombay'
        row.discovery_sources = [{'search_name': 'IIT Bombay', 'url': row.website}]
        row.save()
        self.assertEqual(services.search_public('IIT Bombay').get().pk, row.pk)
        services.add_reference(self.ctx, row)
        self.assertIn(str(row.pk), self.ctx['research_after_reply'])
        self.ctx['research_after_reply'] = set()
        InformationPolicy.objects.update_or_create(pk=1, defaults={'refresh_days':90})
        services.add_reference(self.ctx, row)
        self.assertEqual(self.ctx['research_after_reply'], set())

    def test_invalid_source_quote_is_rejected(self):
        from .agent import build_tools
        pages = {'https://example.edu/': {'content': 'Only MSc Physics is offered.'}}
        submit = build_tools(pages, {}, 'https://example.edu/')[1]
        result = submit.invoke({'facts': [], 'courses': [{'name': 'MSc Computing', 'source_url': 'https://example.edu/', 'source_quote': 'Only MSc Physics is offered.'}], 'intakes': [], 'coverage_notes': ''})
        self.assertIn('error', result)

    def test_valid_records_survive_rejected_batch_and_can_finish_without_bad_records(self):
        from .agent import build_tools
        pages = {'https://example.edu/': {'content': 'Only MSc Physics is offered. Tuition is USD 12000.'}}
        result, draft = {}, {}
        submit = build_tools(pages, result, 'https://example.edu/', draft)[1]
        response = submit.invoke({'facts': [{'topic': 'Program', 'content': 'MSc Physics is offered.',
            'source_url': 'https://example.edu/', 'source_quote': 'Only MSc Physics is offered.'}],
            'courses': [{'name': 'MSc Computing', 'tuition': 'USD 9000',
                'source_url': 'https://example.edu/', 'source_quote': 'Only MSc Physics is offered. Tuition is USD 12000.'}],
            'intakes': [], 'coverage_notes': 'One page'})
        self.assertEqual(response['issue_count'], 2)
        self.assertEqual(response['retained']['facts'], 1)
        self.assertFalse(result)
        self.assertFalse(draft['courses'])
        submit.invoke({'facts': [], 'courses': [], 'intakes': [], 'coverage_notes': 'Only the documented program fact is confirmed.'})
        self.assertEqual(len(result['facts']), 1)
        self.assertFalse(result['courses'])

    def test_processing_limit_publishes_only_previously_validated_records(self):
        from .worker import work_once
        row = public()
        run = services.queue_research(row)
        run.steps = 24
        run.state = {'pages': {row.website: {'title': 'Programs', 'content': 'MSc Physics is offered.'}},
            'draft': {'facts': [{'topic': 'Program', 'content': 'MSc Physics is offered.', 'source_url': row.website,
                'source_quote': 'MSc Physics is offered.'}], 'courses': [], 'intakes': [], 'coverage_notes': 'One page'}, 'messages': []}
        run.save()
        work_once(); work_once()
        run.refresh_from_db(); row.refresh_from_db()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(row.facts.count(), 1)
        self.assertIn('Processing limit reached', row.coverage['notes'])

    @patch('pure_multi_agent.model_router.invoke')
    def test_student_graph_uses_tools_and_model_writes_final_answer(self, model):
        from pure_multi_agent.student_graph import build_student_agent
        model.side_effect = [AIMessage(content='', tool_calls=[{'id': 'review', 'name': 'review_student_profile', 'args': {'focus': 'resume'}}]), AIMessage(content='Agent-composed resume advice')]
        ctx = {**self.ctx, 'memory': {}}
        result = build_student_agent(ctx, 'Use evidence.', InMemorySaver()).invoke({'messages': [HumanMessage(content='Improve my resume')]}, {'configurable': {'thread_id': str(self.student.uuid)}})
        self.assertEqual(result['messages'][-1].content, 'Agent-composed resume advice')
        self.assertTrue(any(m.type == 'tool' for m in result['messages']))
        self.assertEqual(model.call_count, 2)

    def test_chat_persistence_preserves_concurrent_github_update(self):
        from pure_multi_agent.runtime import _load_context, _persist_context
        ctx = _load_context(str(self.student.uuid))
        ctx['student_profile']['budget'] = 24000
        StudentProfile.objects.filter(pk=self.student.pk).update(github_assessment={'summary': 'New completed sync'})
        _persist_context(str(self.student.uuid), ctx)
        self.student.refresh_from_db()
        self.assertEqual(self.student.github_assessment['summary'], 'New completed sync')
        self.assertEqual(self.student.budget, 24000)

    @patch('pure_multi_agent.model_router.invoke')
    def test_busy_chat_resumes_without_replaying_saved_plan(self, model):
        from pure_multi_agent.runtime import run_turn
        from pure_multi_agent.capacity import ResumeTurnLater
        from github_profiles.scheduling import CapacityBusy
        saver = InMemorySaver()
        model.side_effect = [AIMessage(content='', tool_calls=[{'id': 'save', 'name': 'save_advising_artifact', 'args': {'kind': 'roadmap', 'title': 'Application plan', 'content': {'steps': ['Build a project']}}}]), CapacityBusy(), AIMessage(content='Your plan is saved.')]
        with patch('pure_multi_agent.runtime._checkpointer', saver):
            with self.assertRaises(ResumeTurnLater) as interrupted:
                run_turn(str(self.student.uuid), 'Create my plan', raise_errors=True)
            self.assertEqual(AdvisingArtifact.objects.filter(student=self.student).count(), 1)
            result = run_turn(str(self.student.uuid), 'Create my plan', raise_errors=True, resume_state=interrupted.exception.state)
        self.assertEqual(result[1], 'Your plan is saved.')
        self.assertEqual(AdvisingArtifact.objects.filter(student=self.student).count(), 1)
        checkpoint = saver.get({'configurable': {'thread_id': str(self.student.uuid)}})
        self.assertEqual(sum(m.type == 'human' for m in checkpoint['channel_values']['messages']), 1)

    def test_one_hundred_research_jobs_rotate_fairly(self):
        from .worker import claim, release
        rows = [public(name=f'University {i}') for i in range(100)]
        jobs = [services.queue_research(row, self.user) for row in rows]
        # Give queue entries distinct times; Windows clock resolution can tie.
        for i, job in enumerate(jobs):
            ResearchRun.objects.filter(pk=job.pk).update(available_at=timezone.now()-timedelta(minutes=2)+timedelta(milliseconds=i))
        active = [claim() for _ in range(4)]
        self.assertEqual(len({run.pk for run in active}), 4)
        release(active[0], status='queued', available_at=timezone.now())
        self.assertEqual(claim().pk, jobs[4].pk)

    @patch('university_research.web.search_web')
    def test_official_search_rejects_off_domain_results(self, search):
        from .web import search_official_site
        search.return_value = [{'url': 'https://example.edu/programs'}, {'url': 'https://blog.example.com/university'}, {'url': 'https://example.edu.evil.test/fees'}]
        with patch('university_research.web.canonical_url', lambda u: u):
            found = search_official_site('https://example.edu/', 'fees')
        self.assertEqual(found, [{'url': 'https://example.edu/programs'}])
