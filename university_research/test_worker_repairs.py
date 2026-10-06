from unittest.mock import patch
from datetime import timedelta
from django.test import TestCase, SimpleTestCase
from django.utils import timezone
from langchain_core.messages import AIMessage
from university_research.agent import build_tools
from pure_multi_agent.model_router import _validate


class SubmissionRepairTests(SimpleTestCase):
    def test_full_page_budget_exposes_only_submission_and_stays_local(self):
        from university_research.agent import graph_for
        from langchain_core.messages import HumanMessage
        pages={f'https://example.edu/{n}':{'content':'Page','links':[]} for n in range(8)}
        reply=AIMessage(content='',tool_calls=[{'id':'done','name':'submit_research','args':{'facts':[]}}])
        with patch('university_research.agent.invoke',return_value=reply) as model:
            graph_for('https://example.edu/','Example University').invoke({'messages':[HumanMessage(content='Research')],'pages':pages,'draft':{},'result':{},'errors':0})
        self.assertEqual([t.name for t in model.call_args.args[1]],['submit_research'])
        self.assertTrue(model.call_args.kwargs['local_only'])

    def test_missing_arrays_and_gaps_are_repaired_before_execution(self):
        url='https://example.edu/'
        result={}
        tools=build_tools({url:{'content':'A university in Hoboken.'}},result,url)
        reply=AIMessage(content='',tool_calls=[{'id':'submit','name':'submit_research','args':{
            'facts':[{'topic':'Location','content':'Located in Hoboken.','source_url':url,'source_quote':'A university in Hoboken.'}],
            'gaps':['Fees unavailable']}}])
        _validate(reply,tools)
        call=reply.tool_calls[0]
        self.assertEqual(call['args']['courses'],[])
        self.assertEqual(call['args']['intakes'],[])
        self.assertEqual(call['args']['coverage_notes'],'Fees unavailable')
        response=tools[1].invoke(call['args'])
        self.assertTrue(response['accepted'])
        self.assertEqual(result['coverage_notes'],'Fees unavailable')

    def test_table_row_evidence_is_accepted_with_summary_label(self):
        url='https://example.edu/admissions'
        result={}
        tools=build_tools({url:{'content':'Application dates', 'tables':[{
            'heading':'Admissions','rows':['Round | Deadline','Early Decision I | November 15']}]}},result,url)
        response=tools[1].invoke({'intakes':[{'course_name':'Undergraduate admissions','term':'Early Decision I',
            'deadline':'November 15','source_url':url,'source_quote':'Early Decision I | November 15'}]})
        self.assertTrue(response['accepted'])
        self.assertEqual(result['intakes'][0]['deadline'],'November 15')


class ResearchBudgetTests(TestCase):
    def setUp(self):
        import uuid
        from university_research.models import PublicUniversity,ResearchRun
        self.row=PublicUniversity.objects.create(identity_key='repair-budget',name='Example University',website='https://example.edu/')
        self.run=ResearchRun.objects.create(university=self.row,status='running',available_at=timezone.now(),
            lease_token=uuid.uuid4(),lease_expires_at=timezone.now()+timedelta(minutes=5))

    def test_paid_claim_survives_context_and_checkpoint_replacement(self):
        from university_research.research_budget import research_execution,claim
        from university_research.worker import release
        token=research_execution.set((self.run.pk,self.run.lease_token))
        try:
            claim({},'example.edu')
            with self.assertRaisesRegex(ValueError,'already been used'):
                claim({},'example.edu')
            release(self.run,status='queued',state={'pages':{}})
        finally:
            research_execution.reset(token)
        self.run.refresh_from_db()
        self.assertTrue(self.run.state['_claude_research']['attempted'])

    def test_useful_partial_draft_never_calls_claude(self):
        from university_research.worker import finish_research
        with patch('university_research.worker.publish') as publish, patch('university_research.claude_fallback.search_official_evidence') as claude:
            finish_research(self.run,{'draft':{'facts':[{'topic':'location'}]}})
        publish.assert_called_once()
        claude.assert_not_called()

    def test_empty_research_saves_fallback_before_index(self):
        from university_research.worker import finish_research
        from university_research.test_catalogue_flow import page_fixture
        with patch('university_research.claude_fallback.search_official_evidence',return_value={**page_fixture(), 'evidence_provider': 'claude_web_search'}) as claude:
            finish_research(self.run,{'pages':{},'draft':{}})
        claude.assert_called_once()
        self.assertEqual(self.row.courses.get().name,'BSc Physics')
        self.run.refresh_from_db()
        self.assertEqual(self.run.state['phase'],'index')
