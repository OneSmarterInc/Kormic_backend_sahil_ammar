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


@override_settings(UNIVERSITY_VECTOR_SEARCH=False, AGENT_DISTRIBUTED_LIMITS=False)
class ResearchTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name='Student', gpa=3.6)
        self.user = User.objects.create_user(username='student', email='student@example.test')
        Account.objects.create(user=self.user, role='student', student_profile=self.student)
        TOTPDevice.objects.create(user=self.user, confirmed_at=timezone.now())
        self.ctx = {'canonical_student_id': str(self.student.uuid), 'student_profile': {'name': 'Student', 'gpa': 3.6}, 'current_message': 'Boston'}
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
        self.ctx['read_web_pages'] = {'https://example.edu/': {'title': 'Example University', 'content': 'Welcome to Example University'}}
        result = self.tools()['identify_university_candidates'].invoke({'search_id': str(search.pk), 'candidates': [candidate]})
        self.assertEqual(result['count'], 1)

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
