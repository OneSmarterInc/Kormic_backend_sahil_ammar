from unittest.mock import patch
from django.test import TestCase
from langchain_core.messages import AIMessage
from django_api.models import StudentProfile
from university_research.models import UniversitySearch, PublicUniversity
from pure_multi_agent.tools.university_tools import build_tools
from pure_multi_agent.model_router import _validate


class DiscoveryContextTests(TestCase):
    def setUp(self):
        self.student = StudentProfile.objects.create(name='Discovery student')
        self.url = 'https://www.ku.dk/en'
        self.ctx = {'canonical_student_id':str(self.student.uuid), 'turn_id':'task'}
        self.tools = {t.name:t for t in build_tools(self.ctx)}
        with patch('university_research.web.search_web', return_value=[{'url':self.url,'title':'University of Copenhagen','snippet':'Search snippet'}]):
            self.search = self.tools['list_universities'].invoke({'query':'University of Copenhagen'})

    def test_exact_failed_payload_reads_page_and_resolves_without_claude(self):
        args = {'candidates':[{'name':'University of Copenhagen','website':self.url,'country':'DK','address':'Copenhagen'}]}
        identify = self.tools['identify_university_candidates']
        _validate(AIMessage(content='',tool_calls=[{'id':'call','name':identify.name,'args':args}]), [identify])
        with patch('university_research.web.read_page_once', return_value={'url':self.url,'title':'University of Copenhagen',
                'content':'University of Copenhagen admission information','links':[]}) as read, \
             patch('university_research.claude_fallback.search_official_evidence') as claude:
            result = identify.invoke(args)
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['search_id'], self.search['search_id'])
        self.assertEqual(result['candidates'][0]['sources'][0]['identity_quote'], 'University of Copenhagen')
        read.assert_called_once_with(self.url)
        claude.assert_not_called()
        selected = self.tools['select_university_candidate'].invoke({'candidate_index':1})
        self.assertTrue(selected['university']['id'].startswith('public:'))

    def test_unseen_website_is_rejected_without_fetching(self):
        with patch('university_research.web.read_page_once') as read:
            result = self.tools['identify_university_candidates'].invoke({'candidates':[
                {'name':'University of Copenhagen','website':'https://invented.edu/'}]})
        self.assertIn('error', result)
        read.assert_not_called()

    def test_another_tasks_search_is_not_substituted(self):
        other = UniversitySearch.objects.create(student=self.student,query='Other',candidates={})
        with self.assertRaisesRegex(ValueError, 'active search'):
            self.tools['identify_university_candidates'].invoke({'search_id':str(other.pk),'candidates':[]})

    def test_missing_active_search_does_not_choose_latest_search(self):
        self.ctx.pop('university_search_id')
        with self.assertRaisesRegex(ValueError, 'No search is active'):
            self.tools['identify_university_candidates'].invoke({'candidates':[]})

    def test_ambiguity_asks_before_reading_any_site(self):
        other = 'https://another.edu/'
        row = UniversitySearch.objects.get(pk=self.search['search_id'])
        row.candidates['results'].append({'url':other,'title':'Another university'})
        row.save()
        with patch('university_research.web.read_page_once') as read:
            result = self.tools['identify_university_candidates'].invoke({'candidates':[
                {'name':'University of Copenhagen','website':self.url}, {'name':'Another University','website':other}]})
        self.assertTrue(result['needs_clarification'])
        self.assertFalse(PublicUniversity.objects.exists())
        read.assert_not_called()


    def test_numbered_result_resolves_without_nested_model_arguments(self):
        with patch('university_research.web.read_page_once', return_value={'url':self.url, 'title':'University of Copenhagen - University of Copenhagen', 'content':'University of Copenhagen', 'links':[]}) as read:
            result = self.tools['choose_university_result'].invoke({'result_number':1})
        self.assertEqual(result['university']['name'], 'University of Copenhagen')
        read.assert_called_once()

    def test_invalid_number_is_recoverable_without_fetch(self):
        with patch('university_research.web.read_page_once') as read:
            result = self.tools['choose_university_result'].invoke({'result_number':99})
        self.assertEqual(result['allowed_result_numbers'], [1])
        read.assert_not_called()

    def test_exhausted_schema_retries_return_message_not_failed_job(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason
        from pure_multi_agent.model_router import InvalidLocalToolResponse
        with patch('pure_multi_agent.model_router.invoke', side_effect=InvalidLocalToolResponse('bad shape')):
            result = _reason({'messages':[]}, SimpleNamespace(context={'ctx':{'model_steps':1,'university_discovery_pending':True}, 'prompt':''}))
        self.assertIn('could not be processed', result['messages'][0].content)


    def test_search_display_groups_pages_and_excludes_directories(self):
        from pure_multi_agent.tools.university_tools import numbered_search_results
        results = [{'url':self.url}, {'url':'https://about.ku.dk/'}, {'url':'https://studyindenmark.dk/portal/university-of-copenhagen'}]
        self.assertEqual([item['result_number'] for item in numbered_search_results(results)], [1])

    def test_duplicate_website_clarification_is_recoverable(self):
        row = UniversitySearch.objects.get(pk=self.search['search_id'])
        row.candidates['results'].append({'url':'https://about.ku.dk/','title':'About Copenhagen'})
        row.save()
        result = self.tools['clarify_university_results'].invoke({'result_numbers':[1,2]})
        self.assertIn('error',result)
        self.assertNotIn('university_clarification',self.ctx)


    def test_illinois_marketing_and_main_site_are_one_institution(self):
        from pure_multi_agent.tools.university_tools import numbered_search_results
        rows = [{'url':'https://my.discoverillinoistech.org/DG6223DG','title':'Illinois Institute of Technology'},
                {'url':'https://www.iit.edu/','title':'Illinois Tech | Illinois Institute of Technology'},
                {'url':'https://illinois.edu/','title':'University of Illinois at Urbana-Champaign'}]
        self.assertEqual([item['result_number'] for item in numbered_search_results(rows)], [2,3])

    def test_same_name_is_not_sufficient_to_merge_unrelated_institutions(self):
        from pure_multi_agent.tools.university_tools import numbered_search_results
        rows = [{'url':'https://first.edu/','title':'Example University'}, {'url':'https://second.edu/','title':'Example University'}]
        self.assertEqual(len(numbered_search_results(rows)), 2)

    def test_discovery_uses_current_results_without_legacy_selector_or_old_chat(self):
        from types import SimpleNamespace
        from langchain_core.messages import HumanMessage
        from pure_multi_agent.student_graph import _reason
        self.ctx['model_steps'] = 1
        reply = AIMessage(content='', tool_calls=[{'id':'selection', 'name':'choose_university_result', 'args':{'result_number':1}}])
        with patch('pure_multi_agent.model_router.invoke', return_value=reply) as model:
            _reason({'messages':[HumanMessage(content='Old Illinois task')]},
                SimpleNamespace(context={'ctx':self.ctx,'prompt':'Unrelated profile instructions'}))
        messages, tools = model.call_args.args
        self.assertEqual([t.name for t in tools], ['choose_university_result'])
        self.assertIn('University of Copenhagen', messages[-1].content)
        self.assertNotIn('Old Illinois task', str(messages))
        self.assertNotIn('Unrelated profile', str(messages))
        validator = model.call_args.kwargs['tool_call_validator']
        with self.assertRaises(ValueError):
            validator({'name':'choose_university_result','args':{'result_number':99}})
        validator(reply.tool_calls[0])

    def test_legacy_selector_not_exposed_without_identified_candidates(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason
        with patch('pure_multi_agent.model_router.invoke', return_value=AIMessage(content='Hello')) as model:
            _reason({'messages':[]}, SimpleNamespace(context={'ctx':{},'prompt':''}))
        self.assertNotIn('select_university_candidate', [t.name for t in model.call_args.args[1]])

    def test_resolved_university_handoff_uses_original_question_without_model(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason
        ctx={'turn_id':'task','university_resolution_turn':'task',
            'university_candidates':['public:example'],'university_evidence_required':True,
            'current_message':'What are its courses, fees and admission dates?'}
        with patch('pure_multi_agent.model_router.invoke') as model:
            reply=_reason({'messages':[]}, SimpleNamespace(context={'ctx':ctx,'prompt':''}))['messages'][0]
        model.assert_not_called()
        self.assertEqual(reply.tool_calls[0]['args'], {'university_id':'public:example','question':ctx['current_message']})

    def test_failed_consultation_ends_instead_of_repeating(self):
        from types import SimpleNamespace
        from pure_multi_agent.student_graph import _reason, _tools
        ctx={'university_evidence_required':True}
        runtime=SimpleNamespace(context={'ctx':ctx,'prompt':''})
        call=AIMessage(content='',tool_calls=[{'id':'ask','name':'ask_university','args':{}}])
        from unittest.mock import Mock
        tool=Mock(); tool.name='ask_university'; tool.invoke.return_value={'error':'No usable university information'}
        with patch('pure_multi_agent.student_graph.build_all_tools',return_value=[tool]):
            _tools({'messages':[call]},runtime)
        with patch('pure_multi_agent.model_router.invoke') as model:
            reply=_reason({'messages':[]},runtime)['messages'][0]
        model.assert_not_called()
        self.assertFalse(reply.tool_calls)
        self.assertIn('consultation',reply.content)
