import json
from contextlib import nullcontext
from unittest.mock import patch, Mock
from django.test import TestCase, SimpleTestCase
from django.db import DatabaseError
from langchain_core.messages import AIMessage, HumanMessage
from django_api.models import StudentProfile
from university_research.models import PublicUniversity
from university_research.catalogue import save_catalogue
from pure_multi_agent.answer_context import profile_context, compact_evidence, partial_answer


def page(url='https://example.edu/'):
    return {'url':url,'title':'Example University','content':'MS Computing tuition USD 12000 for 2026.',
        'evidence_provider':'scraper_extracted','catalogue':{'courses':[{
            'name':'MS Computing','tuition':'12000','currency':'USD','academic_year':'2026'}]}}


class FlowTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name='Flow test')
        self.row = PublicUniversity.objects.create(identity_key='flow-test',name='Example University',website='https://example.edu/')
        self.ctx = {'canonical_student_id':str(self.student.uuid),'student_profile':{},'turn_id':'turn',
                    'current_message':'What are the fees for MS Computing?'}

    def test_cached_university_uses_saved_records_without_network(self):
        from pure_multi_agent.public_adviser import consult
        save_catalogue(self.row,page()); self.row.refresh_from_db()
        with patch('university_research.web.search_official_site') as search, patch('university_research.web.read_page_once') as read, patch('pure_multi_agent.registered_adviser._consult',return_value={'answer':'Saved fees'}) as agent:
            self.assertEqual(consult(self.ctx,self.row,self.ctx['current_message'])['answer'],'Saved fees')
        search.assert_not_called(); read.assert_not_called(); agent.assert_called_once()

    def test_registered_match_always_wins(self):
        from pure_multi_agent.public_adviser import consult
        registered = Mock()
        with patch('university_research.services.registered') as directory, patch('pure_multi_agent.registered_adviser.consult',return_value={'answer':'Officer records'}) as agent, patch('university_research.web.read_page_once') as read:
            directory.return_value.filter.return_value.first.return_value = registered
            self.assertEqual(consult(self.ctx,self.row,'requirements')['answer'],'Officer records')
        agent.assert_called_once_with(self.ctx,registered,'requirements'); read.assert_not_called()

    def test_new_collection_failure_research_saved_before_common_agent(self):
        from pure_multi_agent.public_adviser import consult
        self.ctx['new_university_domains']=['example.edu']
        def answer(ctx,row,question,public_row=None):
            self.assertEqual(row.courses.get().tuition,'12000')
            return {'answer':'Saved fees'}
        with patch('university_research.web.search_official_site',return_value=[]), patch('university_research.web.read_page_once',side_effect=ValueError('HTTP 403')) as read, patch('university_research.new_university.research',return_value={**page(),'evidence_provider':'claude_research'}) as research, patch('pure_multi_agent.registered_adviser._consult',side_effect=answer):
            self.assertEqual(consult(self.ctx,self.row,self.ctx['current_message'])['answer'],'Saved fees')
        read.assert_called_once(); research.assert_called_once()
        self.assertEqual(self.row.pages.get().provider,'claude_research')

    def test_save_failure_never_reports_success_or_calls_adviser(self):
        from pure_multi_agent.public_adviser import consult
        self.ctx['read_web_pages']={self.row.website:page()}
        with patch('university_research.catalogue.save_catalogue',side_effect=DatabaseError('write failed')), patch('pure_multi_agent.registered_adviser._consult') as agent:
            with self.assertRaises(DatabaseError):
                consult(self.ctx,self.row,self.ctx['current_message'])
        agent.assert_not_called()

    def test_valid_answer_is_not_rewritten(self):
        from pure_multi_agent.registered_adviser import _consult
        save_catalogue(self.row,page()); self.row.refresh_from_db()
        answer='The saved tuition is USD 12,000 for 2026.'
        with patch('pure_multi_agent.registered_adviser.invoke',return_value=AIMessage(content=answer)) as model:
            result=_consult(self.ctx,self.row,self.ctx['current_message'],public_row=self.row)
        self.assertTrue(result['answer'].startswith(answer)); model.assert_called_once()
        self.assertTrue(model.call_args.kwargs['local_only'])

    def test_generation_failure_retains_saved_fees_and_is_not_scrape_error(self):
        from pure_multi_agent.registered_adviser import _consult
        from pure_multi_agent.model_router import InvalidLocalToolResponse
        save_catalogue(self.row,page()); self.row.refresh_from_db()
        with patch('pure_multi_agent.registered_adviser.invoke',side_effect=InvalidLocalToolResponse('empty correction')):
            result=_consult(self.ctx,self.row,self.ctx['current_message'],public_row=self.row)
        self.assertIn('12000',result['answer']); self.assertNotIn('could not be retrieved',result['answer'])

    def test_research_budget_and_raw_response_replay(self):
        from university_research.new_university import research
        self.ctx['new_university_domains']=['example.edu']
        raw=json.dumps({'identity':{'name':'Example University','website':self.row.website},'catalogue':page()['catalogue']})
        with patch('pure_multi_agent.model_router.invoke',return_value=AIMessage(content=raw)) as model:
            first=research(self.ctx,self.row.website,'fees')
            second=research(self.ctx,self.row.website,'fees')
            model.assert_called_once(); self.assertEqual(first,second)
            self.assertTrue(model.call_args.kwargs['single_attempt'])
            self.ctx['new_university_domains'].append('another.edu')
            with self.assertRaises(ValueError): research(self.ctx,'https://another.edu/','fees')
        self.assertEqual(first['evidence_provider'],'claude_research')

    def test_existing_university_cannot_use_research_fallback(self):
        from university_research.new_university import research
        with patch('pure_multi_agent.model_router.invoke') as model:
            with self.assertRaises(ValueError): research(self.ctx,self.row.website,'fees')
        model.assert_not_called()


class ContextTests(SimpleTestCase):
    def test_target_study_survives_followup_without_using_current_degree(self):
        from pure_multi_agent.answer_context import study_focus
        target=study_focus({},'My current degree is BTech. I want MS Computer Science for September 2027.')
        self.assertEqual(target['target_degree'],'MS')
        self.assertEqual(target['target_intake'],'September 2027')
        self.assertEqual(study_focus(target,'What should I do this week?'),target)

    def test_single_paid_attempt_does_not_retry_invalid_response(self):
        from pure_multi_agent import model_router as router
        with patch.object(router,'provider_blocked',return_value=False), patch.object(router,'model_slot',return_value=nullcontext()), patch('pure_multi_agent.capacity.model_slot',return_value=nullcontext()), patch.object(router,'claude') as paid:
            paid.return_value.invoke.return_value=AIMessage(content='')
            paid.return_value.model='test'
            with self.assertRaises(router.InvalidToolResponse):
                router.invoke([HumanMessage(content='research')],force_claude=True,single_attempt=True,research=True)
            paid.return_value.invoke.assert_called_once()

    def test_profile_excludes_raw_documents_and_unrelated_private_data(self):
        result=profile_context({'program':'BTech','gpa':8,'resume_text':'x'*50000,'notes':'private','skills':['Python']},'fees')
        self.assertEqual(result,{'program':'BTech'})

    def test_records_bounded_and_relevant(self):
        data={'courses':[{'name':f'History {n}'} for n in range(99)]+[{'name':'MS Computer Science'}]}
        result=compact_evidence(data,'MS Computer Science tuition')
        self.assertEqual(result['courses'][0]['name'],'MS Computer Science'); self.assertLessEqual(len(result['courses']),6)

    def test_supported_paragraph_survives_failed_answer(self):
        result=partial_answer('fees',{'facts':[{'content':'Tuition is USD 12000.'}]},'Tuition is USD 12000.\n\nSeats are 600.')
        self.assertIn('12000',result); self.assertNotIn('600',result)

    def test_general_planning_does_not_dispatch_identity_tools(self):
        from pure_multi_agent.turn_policy import classify,select_tools
        ctx={'canonical_student_id':'test','current_message':'What information do you need from me to suggest suitable universities?'}
        with patch('pure_multi_agent.model_router.invoke') as model:
            intent=classify(ctx,[])
        model.assert_not_called(); self.assertEqual(intent['route'],'general')
        self.assertEqual(select_tools([type('Tool',(),{'name':'list_universities'})()],intent,ctx),[])

    def test_student_scope_never_dispatches_claude_for_local_failure(self):
        from pure_multi_agent import model_router as router
        @router.student_model_policy
        def request(): return router.invoke([HumanMessage(content='hello')])
        with patch.object(router,'qwen_slot',return_value=nullcontext()), patch.object(router,'provider_blocked',return_value=False), patch.object(router,'block_provider'), patch.object(router,'qwen',side_effect=RuntimeError('offline')), patch.object(router,'claude') as paid:
            with self.assertRaises(router.AIServiceUnavailable): request()
        paid.assert_not_called()
