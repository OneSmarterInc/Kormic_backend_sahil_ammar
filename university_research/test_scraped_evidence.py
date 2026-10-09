from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from universities.models import University
from university_research.catalogue import save_catalogue
from university_research.claude_fallback import search_official_evidence
from university_research.models import PublicUniversity


class SourceFallbackTests(SimpleTestCase):
    def test_empty_extraction_retains_actual_source_without_memory_fallback(self):
        source = {'url': 'https://example.edu/aid', 'title': 'Scholarships',
                  'content': 'Scholarship applicants must submit an application.'}
        with patch('university_research.claude_fallback.extract_catalogue_locally',
                   return_value={**source, 'catalogue': {}}), \
             patch('university_research.new_university.research') as memory:
            result = search_official_evidence('https://example.edu/', source_page=source)
        memory.assert_not_called()
        self.assertEqual(result['catalogue']['facts'][0]['content'], source['content'])
        self.assertEqual(result['url'], source['url'])
        self.assertTrue(result['grounded_in_source'])

    def test_unavailable_source_fails_without_inventing_information(self):
        with patch('university_research.web.search_official_site', return_value=[]), \
             patch('university_research.web.read_page_once', side_effect=ValueError('unavailable')), \
             patch('university_research.new_university.research') as memory:
            with self.assertRaisesRegex(ValueError, 'No official source document'):
                search_official_evidence('https://example.edu/')
        memory.assert_not_called()


class LinkedSourceTests(TestCase):
    def test_linked_university_saves_source_without_changing_officer_profile(self):
        institution = University.objects.create(name='Example', website_url='https://example.edu/',
                                                description='Officer description')
        row = PublicUniversity.objects.create(identity_key='linked-source', name='Example',
                  website=institution.website_url, registered_university=institution)
        page = {'url': 'https://example.edu/aid', 'content': 'Scholarship applications are open.',
                'evidence_provider': 'scraper_excerpt', 'catalogue': {'facts': [
                    {'topic': 'Scholarships', 'content': 'Scholarship applications are open.'}]}}
        save_catalogue(row, page)
        self.assertEqual(row.facts.get().page.content, page['content'])
        institution.refresh_from_db()
        self.assertEqual(institution.description, 'Officer description')
        for provider in ('claude_direct',):
            with self.assertRaisesRegex(ValueError, 'Model-memory'):
                save_catalogue(row, {**page, 'evidence_provider': provider})
