from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, TestCase
from langchain_core.messages import AIMessage, HumanMessage
from pure_multi_agent import model_router
from pure_multi_agent.capacity import LimitedMessages
from pure_multi_agent.claude_policy import prepare_sdk


class ClaudeRoutingTests(SimpleTestCase):
    def test_legacy_local_only_flag_cannot_dispatch_qwen(self):
        model = Mock(model='claude-test')
        model.invoke.return_value = AIMessage(content='Claude answer')
        with patch.object(model_router, 'qwen') as local, patch.object(model_router, 'claude', return_value=model), \
             patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'model_slot', return_value=nullcontext()), \
             patch('pure_multi_agent.capacity.model_slot', return_value=nullcontext()):
            reply = model_router.invoke([HumanMessage(content='Hello')], local_only=True)
        local.assert_not_called()
        self.assertEqual(reply.response_metadata['routing_provider'], 'claude')

    def test_invalid_claude_json_is_repaired_once_and_validated(self):
        model = Mock(model='claude-test')
        model.invoke.side_effect = [AIMessage(content='{"score":"bad"}'), AIMessage(content='{"score":2}')]
        with patch.object(model_router, 'claude', return_value=model), \
             patch.object(model_router, 'provider_blocked', return_value=False), \
             patch.object(model_router, 'model_slot', return_value=nullcontext()), \
             patch('pure_multi_agent.capacity.model_slot', return_value=nullcontext()):
            reply = model_router.invoke([HumanMessage(content='Score')], json_schema={'type':'object', 'properties': {'score': {'type': 'integer'}}})
        self.assertEqual(reply.content, '{"score":2}')
        self.assertEqual(model.invoke.call_count, 2)

    def test_sdk_never_uses_legacy_local_adapter(self):
        sdk = Mock()
        sdk.create.return_value = SimpleNamespace(id='test', usage=SimpleNamespace(model_dump=lambda: {'input_tokens':10,'output_tokens':2}))
        with patch('pure_multi_agent.legacy_qwen.create') as local, patch('pure_multi_agent.capacity.model_slot', return_value=nullcontext()), patch('pure_multi_agent.claude_policy.emit'):
            LimitedMessages(sdk).create(model='old-model', max_tokens=9000, messages=[{'role':'user','content':'Hi'}])
        local.assert_not_called()
        self.assertTrue(sdk.create.call_args.kwargs['model'].startswith('claude-'))
        self.assertEqual(sdk.create.call_args.kwargs['max_tokens'], 4000)

    def test_input_budget_rejects_excess_without_silently_cutting_it(self):
        from pure_multi_agent.qwen_context import ContextBudgetExceeded
        with self.assertRaises(ContextBudgetExceeded):
            prepare_sdk({'messages':[{'role':'user','content':'word ' * 100000}], 'max_tokens':100})

    def test_scraped_fact_extraction_never_calls_a_model(self):
        from knowledge.scraper import extract_facts_from_page
        with patch('knowledge.scraper._get_anthropic_client', side_effect=AssertionError('Scraping must be free')):
            facts = extract_facts_from_page('https://example.edu/fees', 'Tuition costs $600 per semester.', 'Example')
        self.assertIn('$600 per semester', facts[0]['content'])

    def test_github_analysis_skips_ollama(self):
        from github_profiles.inference import Inference
        sdk = Mock()
        sdk.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(type='tool_use', name='structured_response', input={'summary':'ok'})])
        with patch('github_profiles.inference.httpx.post') as local, patch('agents.github_agent._get_anthropic_client', return_value=sdk):
            result = Inference().chat([{'role':'user','content':'Analyze'}], {'type':'object'})
        local.assert_not_called()
        self.assertEqual(result['provider'], 'claude')

    def test_research_page_selection_does_not_call_a_model(self):
        from university_research.agent import graph_for
        with patch('university_research.agent.invoke', side_effect=AssertionError('No paid crawl planner')):
            result = graph_for('https://example.edu', 'Example').invoke({'messages':[], 'pages':{}, 'draft':{}, 'errors':0})
        self.assertEqual(result['messages'][-1].tool_calls[0]['name'], 'read_official_page')

    def test_malformed_source_links_do_not_stop_a_full_crawl(self):
        from url_discovery.url_normalizer import normalize_url
        self.assertEqual(normalize_url('https://example.edu:bad-port/file'), '')
        self.assertEqual(normalize_url('http://[bad-ipv6'), '')
        self.assertEqual(normalize_url('https://example.edu:443/fees#top'), 'https://example.edu/fees')


class AlgorithmicCatalogueTests(TestCase):
    def test_cached_website_extraction_preserves_evidence_without_inference(self):
        from university_research.claude_fallback import extract_catalogue_locally
        page = {'url':'https://example.edu/fees','title':'Fees','content':'Tuition is $600 per semester. ' * 400, 'structured_entities':[]}
        with patch('pure_multi_agent.model_router.invoke', side_effect=AssertionError('No website inference')):
            first = extract_catalogue_locally(page['url'], 'fees', page)
            second = extract_catalogue_locally(page['url'], 'different question', page)
        self.assertEqual(first, second)
        self.assertEqual(''.join(item['content'] for item in first['catalogue']['facts']), page['content'])
