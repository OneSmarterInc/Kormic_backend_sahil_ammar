from types import SimpleNamespace as Obj
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from university_research.claude_fallback import citation_pages, search_official_evidence


class ClaudeEvidenceTests(SimpleTestCase):
    def test_only_provider_citations_on_the_official_domain_are_accepted(self):
        def citation(url, quote):
            return Obj(type='web_search_result_location', url=url, cited_text=quote, title='Official source')
        response = Obj(content=[Obj(type='text', citations=[
            citation('https://www.ox.ac.uk/fees', 'Official fees excerpt'),
            citation('https://fake-ox.ac.uk/fees', 'Fake fee'),
            citation('https://www.ox.ac.uk.evil.test/fees', 'Fake fee')])])
        pages = citation_pages(response, 'ox.ac.uk')
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]['content'], 'Official fees excerpt')


class OfficialEvidenceTests(TestCase):
    def test_fetched_source_is_retained_without_model_inference(self):
        source = {'url': 'https://example.edu/fees', 'title': 'Fees',
                  'content': 'Tuition is $12000 per year.', 'links': []}
        with patch('university_research.web.search_official_site', return_value=[{'url': source['url']}]), \
             patch('university_research.web.read_page_once', return_value=source), \
             patch('pure_multi_agent.model_router.invoke') as model:
            result = search_official_evidence('https://example.edu/', 'fees')
        model.assert_not_called()
        self.assertTrue(result['grounded_in_source'])
        self.assertEqual(result['url'], source['url'])
        self.assertEqual(result['catalogue']['facts'][0]['content'], source['content'])
        self.assertEqual(result['evidence_provider'], 'scraper_extracted')

    def test_unavailable_sources_never_fall_back_to_model_memory(self):
        with patch('university_research.web.search_official_site', return_value=[]), \
             patch('university_research.web.read_page_once', side_effect=ValueError('Unavailable')), \
             patch('pure_multi_agent.model_router.invoke') as model:
            with self.assertRaisesRegex(ValueError, 'No official source document'):
                search_official_evidence('https://example.edu/')
        model.assert_not_called()

    def test_readable_source_needs_neither_network_nor_model(self):
        source = {'url': 'https://example.edu/physics', 'content': 'Physics programmes are available.'}
        with patch('university_research.web.search_official_site') as search, \
             patch('pure_multi_agent.model_router.invoke') as model:
            result = search_official_evidence('https://example.edu/', source_page=source)
        search.assert_not_called()
        model.assert_not_called()
        self.assertEqual(result['catalogue']['facts'][0]['content'], source['content'])
