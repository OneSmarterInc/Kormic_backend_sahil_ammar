from unittest.mock import patch
from django.test import SimpleTestCase
from pure_multi_agent.tools.university_tools import build_tools, resolution_error

class UniversityResolutionTests(SimpleTestCase):
    def identity_fixture(self):
        from pure_multi_agent.tools.university_tools import Candidate
        candidate = Candidate(name='California Institute of Technology', website='https://www.caltech.edu/',
            source_indices=list(range(9)), official_identity_quote='An inaccurate proposed identity quote')
        ctx = {'read_web_pages': {candidate.website: {'url':candidate.website,
            'title':'Home - www.caltech.edu', 'content':'Caltech Homepage. Research news.', 'links':[]}}}
        return candidate, ctx

    def test_identity_recovery_reads_official_about_page_not_search_snippet(self):
        from unittest.mock import Mock
        from pure_multi_agent.tools.university_tools import recover_identity
        candidate, ctx = self.identity_fixture()
        reader = Mock()
        reader.invoke.return_value = {'url':'https://www.caltech.edu/about',
            'title':'About Caltech', 'content':'California Institute of Technology (Caltech) is a research institute.'}
        with patch('university_research.claude_fallback.search_official_evidence') as fallback:
            identity = recover_identity(ctx, candidate, [
                {'url':'https://unrelated.edu/about', 'title':candidate.name},
                {'url':'https://www.caltech.edu/about', 'title':'About Caltech'}], reader)
        self.assertEqual(identity['url'], 'https://www.caltech.edu/about')
        reader.invoke.assert_called_once_with({'url':'https://www.caltech.edu/about'})
        fallback.assert_not_called()

    def test_successful_but_uninformative_scrape_reaches_claude_and_preserves_answer(self):
        from unittest.mock import Mock
        from pure_multi_agent.tools.university_tools import recover_identity
        candidate, ctx = self.identity_fixture()
        answer = {'text':'Courses, fees, seats and scholarships from Claude.', 'sources':['https://www.caltech.edu/about']}
        page = {'url':candidate.website, 'links':[], 'provider_answer':answer,
            'citation_pages':[{'url':'https://www.caltech.edu/about', 'content':candidate.name}]}
        with patch('university_research.claude_fallback.search_official_evidence', return_value=page) as fallback:
            identity = recover_identity(ctx, candidate, [], Mock())
            self.assertEqual(recover_identity(ctx, candidate, [], Mock()), identity)
        fallback.assert_called_once()
        self.assertEqual(ctx['read_web_pages'][candidate.website]['provider_answer'], answer)

    def test_identity_recovery_never_accepts_other_institution_or_repeats_fallback(self):
        from unittest.mock import Mock
        from pure_multi_agent.tools.university_tools import recover_identity
        candidate, ctx = self.identity_fixture()
        page = {'url':candidate.website, 'links':[], 'citation_pages':[
            {'url':'https://unrelated.edu/about','content':candidate.name}]}
        with patch('university_research.claude_fallback.search_official_evidence', return_value=page) as fallback:
            for _ in range(3):
                self.assertIsNone(recover_identity(ctx, candidate, [], Mock()))
        fallback.assert_called_once()

    def test_unresolved_turn_cannot_select_consultation_or_official_site_search(self):
        from types import SimpleNamespace
        from langchain_core.messages import HumanMessage, AIMessage
        from pure_multi_agent.student_graph import _reason
        ctx={'turn_id':'new','model_steps':1}
        available=[SimpleNamespace(name=name) for name in ['list_universities','ask_university','search_official_university_site']]
        reply=AIMessage(content='',tool_calls=[{'id':'directory','name':'list_universities','args':{'query':'New University'}}])
        with patch('pure_multi_agent.student_graph.build_all_tools',return_value=available), patch('pure_multi_agent.model_router.invoke',return_value=reply) as model:
            _reason({'messages':[HumanMessage(content='New University fees')]},SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        self.assertEqual([tool.name for tool in model.call_args.args[1]],['list_universities'])

    def test_old_scrapes_are_removed_but_current_tool_protocol_is_preserved(self):
        from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
        from pure_multi_agent.student_graph import turn_messages
        old_call=AIMessage(content='',tool_calls=[{'name':'read_page','args':{},'id':'old'}])
        current_call=AIMessage(content='',tool_calls=[{'name':'read_page','args':{},'id':'current'}])
        current_result=ToolMessage(content='current verified facts',tool_call_id='current')
        messages=[HumanMessage(content='Stanford'),old_call,ToolMessage(content='navigation '*10000,tool_call_id='old'),AIMessage(content='Previous answer'),HumanMessage(content='MIT'),current_call,current_result]
        result=turn_messages(messages)
        self.assertEqual(result[-3:],messages[-3:])
        self.assertNotIn('navigation',str(result))
        self.assertTrue(any(m.content=='Previous answer' for m in result))

    def test_previous_turn_selection_is_rejected(self):
        ctx={'turn_id':'new', 'university_resolution_turn':'old', 'university_candidates':['old-id']}
        self.assertIsNotNone(resolution_error(ctx,'old-id'))

    def test_unselected_university_is_rejected(self):
        ctx={'turn_id':'new', 'university_resolution_turn':'new', 'university_candidates':['iit']}
        self.assertIsNotNone(resolution_error(ctx,'old-id'))
        self.assertIsNone(resolution_error(ctx,'iit'))

    def test_stale_consultation_never_calls_other_university(self):
        ctx={'turn_id':'new','university_candidates':['old-id']}
        tools={t.name:t for t in build_tools(ctx)}
        with patch('pure_multi_agent.registered_adviser.consult') as consult:
            result=tools['ask_university'].invoke({'university_id':'old-id','question':'Can I get admission into IIT Bombay?'})
        consult.assert_not_called()
        self.assertIn('list_universities',result['instruction'])

    def test_registered_official_search_uses_web_not_consultation(self):
        from types import SimpleNamespace
        ctx={'turn_id':'new','university_resolution_turn':'new','university_candidates':['iit']}
        tools={t.name:t for t in build_tools(ctx)}
        row=SimpleNamespace(website_url='https://www.iitb.ac.in/')
        with patch('pure_multi_agent.tools.university_tools.services.registered') as registered, patch('university_research.web.search_official_site',return_value=[{'url':'https://acad.iitb.ac.in/admissions'}]) as search, patch('pure_multi_agent.tools.university_tools.services.add_reference'):
            registered.return_value.get.return_value=row
            result=tools['search_official_university_site'].invoke({'university_id':'iit','query':'admissions'})
        search.assert_called_once_with('https://www.iitb.ac.in/','admissions')
        self.assertEqual(result['official_domain'],'www.iitb.ac.in')

    def test_unresolved_lookup_forces_directory_tool_instead_of_final_answer(self):
        from types import SimpleNamespace
        from langchain_core.messages import AIMessage, HumanMessage
        from pure_multi_agent.student_graph import _reason
        ctx={'university_lookup_required':True,'model_steps':1}
        reply=AIMessage(content='',tool_calls=[{'id':'resolve','name':'list_universities','args':{'query':'IIT Bombay'}}])
        with patch('pure_multi_agent.job_recovery.boundary'), patch('pure_multi_agent.student_graph.build_all_tools',return_value=[SimpleNamespace(name='ask_university'),SimpleNamespace(name='list_universities')]), patch('pure_multi_agent.model_router.invoke',return_value=reply) as model:
            _reason({'messages':[HumanMessage(content='Find IIT Bombay courses')]},SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        self.assertEqual([tool.name for tool in model.call_args.args[1]],['list_universities'])
        self.assertTrue(model.call_args.kwargs['require_tools'])
        self.assertTrue(model.call_args.kwargs['local_only'])

    def test_directory_identity_cannot_skip_evidence_retrieval(self):
        from types import SimpleNamespace
        from langchain_core.messages import AIMessage, HumanMessage
        from pure_multi_agent.student_graph import _reason
        ctx={'turn_id':'turn','university_candidates':['public:example'],'university_evidence_required':True,'university_resolution_turn':'turn','model_steps':2}
        reply=AIMessage(content='',tool_calls=[{'id':'evidence','name':'ask_university','args':{'university_id':'public:example','question':'fees'}}])
        with patch('pure_multi_agent.job_recovery.boundary'), patch('pure_multi_agent.student_graph.build_all_tools',return_value=[SimpleNamespace(name='ask_university'),SimpleNamespace(name='list_universities')]), patch('pure_multi_agent.model_router.invoke',return_value=reply) as model:
            _reason({'messages':[HumanMessage(content='Find courses and fees')]},SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        self.assertEqual([tool.name for tool in model.call_args.args[1]],['ask_university'])
        self.assertTrue(model.call_args.kwargs['require_tools'])
        self.assertTrue(model.call_args.kwargs['local_only'])

    def test_college_turn_does_not_fetch_github_automatically(self):
        from types import SimpleNamespace
        from langchain_core.messages import AIMessage, HumanMessage
        from pure_multi_agent.student_graph import _reason
        ctx={'canonical_student_id':'synthetic', 'model_steps':0}
        reply=AIMessage(content='',tool_calls=[{'id':'resolve','name':'list_universities','args':{'query':'IIT Bombay'}}])
        with patch('pure_multi_agent.job_recovery.boundary'), patch('pure_multi_agent.change_proposals.conversation_state',return_value={}), patch('pure_multi_agent.student_graph.build_all_tools',return_value=[SimpleNamespace(name='list_universities')]), patch('pure_multi_agent.model_router.invoke',return_value=reply), patch('pure_multi_agent.tools.github_tools.github_evidence') as github:
            _reason({'messages':[HumanMessage(content='Find IIT Bombay courses')]},SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        github.assert_not_called()

    def test_blocked_discovery_stops_before_another_model_or_wrong_college_call(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason
        ctx={'university_discovery_blocked':{'url':'https://www.ox.ac.uk/','reason':'robots.txt returned HTTP 429'}}
        with patch('pure_multi_agent.job_recovery.boundary'), patch('pure_multi_agent.model_router.invoke') as model:
            result=_reason({'messages':[]},SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        model.assert_not_called()
        self.assertNotIn('N/A',result['messages'][0].content)
        self.assertIn('could not be accessed',result['messages'][0].content)

    def test_failed_page_is_not_fetched_repeatedly(self):
        ctx={'known_web_urls':{'https://example.edu/'},'university_discovery_pending':True}
        tool={t.name:t for t in build_tools(ctx)}['read_university_webpage']
        with patch('university_research.web.read_page',side_effect=ValueError('robots.txt returned HTTP 429')) as fetch, patch('university_research.claude_fallback.search_official_evidence',side_effect=ValueError('No verified citations')):
            with self.assertRaises(ValueError): tool.invoke({'url':'https://example.edu/'})
            result=tool.invoke({'url':'https://example.edu/'})
        self.assertEqual(fetch.call_count,1)
        self.assertIn('already failed',result['instruction'])

    def test_first_scrape_success_does_not_use_claude(self):
        ctx={'known_web_urls':{'https://example.edu/'}}
        tool={t.name:t for t in build_tools(ctx)}['read_university_webpage']
        page={'url':'https://example.edu/','content':'Official tuition information','links':[]}
        with patch('university_research.web.read_page',return_value=page) as fetch, patch('university_research.claude_fallback.search_official_evidence') as fallback:
            self.assertEqual(tool.invoke({'url':'https://example.edu/'}),page)
        self.assertEqual(fetch.call_count,1)
        fallback.assert_not_called()

    def test_registered_search_outage_does_not_call_claude(self):
        from types import SimpleNamespace
        ctx={'turn_id':'new','university_resolution_turn':'new','university_candidates':['iit'],'current_message':'fees and seats'}
        tools={t.name:t for t in build_tools(ctx)}
        page={'url':'https://www.iitb.ac.in/','content':'Verified source','links':[],'citation_pages':[{'url':'https://www.iitb.ac.in/fees','content':'Verified fees','links':[]}]}
        with patch('pure_multi_agent.tools.university_tools.services.registered') as registered, patch('pure_multi_agent.tools.university_tools.services.add_reference'), patch('university_research.web.search_official_site',side_effect=RuntimeError('No results found')), patch('university_research.web.read_page',side_effect=ValueError('unavailable')) as fetch, patch('university_research.claude_fallback.search_official_evidence',return_value=page) as fallback:
            registered.return_value.get.return_value=SimpleNamespace(website_url='https://www.iitb.ac.in/')
            with self.assertRaises(ValueError):
                tools['search_official_university_site'].invoke({'university_id':'iit','query':'fees'})
        self.assertEqual(fetch.call_count,1)
        fallback.assert_not_called()

    def test_completed_university_lookup_does_not_start_unrelated_actions(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason
        ctx={'turn_id':'turn','university_answer_evidence':{'agent_answer':'The university has not provided its fees.','answer_turn':'turn'},'current_message':'fees'}
        with patch('pure_multi_agent.job_recovery.boundary'), patch('pure_multi_agent.student_graph.build_all_tools',return_value=[]), patch('pure_multi_agent.model_router.invoke') as model:
            result=_reason({'messages':[]},SimpleNamespace(context={'ctx':ctx,'prompt':''}))
        model.assert_not_called()
        self.assertEqual(result['messages'][0].content,'The university has not provided its fees.')
